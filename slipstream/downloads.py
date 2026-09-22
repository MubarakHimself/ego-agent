"""Lease-scoped downloads / uploads as session artifacts (Browserbase-style).

Downloads land under ``{artifacts_root}/leases/{lease_id}/downloads/``
(outside Space user-data-dir and outside the credential vault). Agents see
artifact metadata (id, filename, bytes, sha256, created_at) and optional
lease-relative paths — never absolute host secret paths by default.

Chrome download behavior is configured via CDP ``Browser.setDownloadBehavior``
when not mock. Mock mode records the call for tests.
"""

from __future__ import annotations

from slipstream.artifact_walk import KindDirHandle, walk_lease_kind_dir

import hashlib
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

from slipstream.alerts import scrub_text


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DownloadError(Exception):
    """Base downloads failure."""


class DownloadValidationError(DownloadError):
    """Bad request / unsafe path / denylisted name."""


class ArtifactNotFoundError(DownloadError):
    """Unknown artifact id for this lease."""


class DownloadForbiddenError(DownloadError):
    """Lease ownership / agent_id mismatch."""


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


_SAFE_LEASE_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_SAFE_ARTIFACT_RE = re.compile(r"^(dl|up|ev)_[A-Za-z0-9]{8,32}$")
_SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9._+@%-][A-Za-z0-9._+@% -]{0,199}$")

# Filenames that look like secrets — refuse list/serve/upload.
_DENIED_FILENAME_SUBSTR = (
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "api_key",
    "private_key",
    "id_rsa",
    "id_ed25519",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    "credentials",
    "cookie",
    "storage_state",
)

DEFAULT_MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MiB thin drop
_BEHAVIOR_ALLOW = "allow"
_ERR_WALK_REQUIRED = "artifacts_root and lease_id required"
_ERR_SYMLINK = "symlink escape refused"
_LABEL_ARTIFACT_DIR = "artifact dir"

KIND_DOWNLOADS = "downloads"
_HAS_O_NOFOLLOW = hasattr(os, "O_NOFOLLOW")
KIND_UPLOADS = "uploads"
KIND_EVIDENCE = "evidence"
_LEASES_DIRNAME = "leases"
_ARTIFACT_KINDS = frozenset({KIND_DOWNLOADS, KIND_UPLOADS, KIND_EVIDENCE})


# ---------------------------------------------------------------------------
# Path / id helpers
# ---------------------------------------------------------------------------


def _resolve_policy_path(path: Path) -> Path:
    """Resolve path (Skylos PATH_SANITIZERS-recognized name)."""
    return Path(path).expanduser().resolve()


def validate_id_component(
    name: str,
    value: str,
    *,
    pattern: re.Pattern[str] | None = None,
) -> str:
    """Sanitize lease_id / artifact_id (no path separators)."""
    if pattern is None:
        pattern = _SAFE_LEASE_RE if name == "lease_id" else _SAFE_ARTIFACT_RE
    if not isinstance(value, str) or not value.strip():
        raise DownloadValidationError(f"{name} required")
    s = value.strip()
    if ".." in s or "/" in s or "\\" in s or chr(0) in s:
        raise DownloadValidationError(f"unsafe {name}")
    if not pattern.match(s):
        raise DownloadValidationError(f"unsafe {name} charset")
    return s


def is_denied_filename(filename: str) -> bool:
    """True when basename looks secret-bearing (denylist)."""
    name = Path(filename).name.lower()
    if not name or name in (".", "..") or name.startswith("."):
        return True
    collapsed = name.replace("-", "").replace("_", "")
    return any(
        bad in name or bad.replace("_", "").replace("-", "") in collapsed
        for bad in _DENIED_FILENAME_SUBSTR
    )


def sanitize_upload_filename(raw: Any) -> str:
    """Validate upload filename — basename only, no traversal, denylist."""
    if not isinstance(raw, str) or not raw.strip():
        raise DownloadValidationError("filename required")
    name = Path(raw.strip()).name
    if name != raw.strip().replace("\\", "/").split("/")[-1]:
        raise DownloadValidationError("filename must be a basename")
    if ".." in name or "/" in name or "\\" in name or chr(0) in name:
        raise DownloadValidationError("unsafe filename")
    if not _SAFE_FILENAME_RE.match(name):
        raise DownloadValidationError("filename charset refused")
    if is_denied_filename(name):
        raise DownloadValidationError("filename refused by secret denylist")
    cleaned = scrub_text(name)
    if cleaned != name:
        raise DownloadValidationError("filename refused by secret denylist")
    return name


def artifact_id_for_filename(filename: str, *, kind: str = KIND_DOWNLOADS) -> str:
    """Stable id from basename (dl_/up_/ev_ + sha256 prefix)."""
    if kind == KIND_EVIDENCE:
        prefix = "ev"
    elif kind == KIND_DOWNLOADS:
        prefix = "dl"
    else:
        prefix = "up"
    digest = hashlib.sha256(filename.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def lease_artifacts_dir(artifacts_root: Path, lease_id: str, kind: str) -> Path:
    """Return ``{artifacts_root}/leases/{lease_id}/{downloads|uploads}/``.

    Resolve *only* ``artifacts_root``; join ``leases`` / lease / kind without
    ``Path.resolve()`` so intermediate or kind symlinks cannot redirect the
    joined path before openat validation (ADV-DL-002/010/011).
    """
    safe = validate_id_component("lease_id", lease_id, pattern=_SAFE_LEASE_RE)
    if kind not in _ARTIFACT_KINDS:
        raise DownloadValidationError("invalid artifact kind")
    root = _resolve_policy_path(artifacts_root)
    return root / _LEASES_DIRNAME / safe / kind


def ensure_lease_artifact_dirs(artifacts_root: Path, lease_id: str) -> dict[str, Path]:
    """Create lease downloads + uploads dirs via nofollow walk (ADV-DL-010/011).

    Paths are derived from held kind_fd via /proc (ADV-DL-013/014) — not
    Path.resolve through unverified ancestors.
    """
    out: dict[str, Path] = {}
    for kind in (KIND_DOWNLOADS, KIND_UPLOADS, KIND_EVIDENCE):
        with walk_lease_kind_dir(
            artifacts_root, lease_id, kind, create=True
        ) as handle:
            out[kind] = handle.path_via_proc()
    return out


def ensure_artifacts_outside(
    artifacts_root: Path,
    *,
    spaces_root: Path,
    vault_root: Path,
) -> None:
    """Fail closed if artifacts sit inside spaces or vault (or equal)."""
    art = _resolve_policy_path(artifacts_root)
    spaces = _resolve_policy_path(spaces_root)
    vault = _resolve_policy_path(vault_root)
    for label, other in (("spaces_root", spaces), ("vault_root", vault)):
        if art == other:
            raise ValueError(
                f"artifacts_root must be outside {label} (got equal paths: {art})"
            )
        try:
            art.relative_to(other)
        except ValueError:
            continue
        raise ValueError(
            f"artifacts_root must be outside {label} "
            f"(artifacts={art} is under {label}={other})"
        )


def max_upload_bytes() -> int:
    raw = os.environ.get("SLIPSTREAM_MAX_UPLOAD_BYTES", "").strip()
    try:
        n = int(raw) if raw else DEFAULT_MAX_UPLOAD_BYTES
    except ValueError:
        n = DEFAULT_MAX_UPLOAD_BYTES
    return max(1024, min(n, 50 * 1024 * 1024))


# ---------------------------------------------------------------------------
# Safe open / list (openat via KindDirHandle — ADV-DL-013)
# ---------------------------------------------------------------------------


def _sha256_fd(fd: int, *, size: int) -> str:
    h = hashlib.sha256()
    remaining = size
    while remaining > 0:
        chunk = os.read(fd, min(65536, remaining))
        if not chunk:
            break
        h.update(chunk)
        remaining -= len(chunk)
    return h.hexdigest()


def _public_meta(
    *,
    artifact_id: str,
    filename: str,
    size: int,
    sha256: str,
    created_at: float,
    kind: str,
    include_rel_path: bool,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": artifact_id,
        "filename": filename,
        "bytes": size,
        "sha256": sha256,
        "created_at": created_at,
        "kind": ("download" if kind == KIND_DOWNLOADS else ("evidence" if kind == KIND_EVIDENCE else "upload")),
    }
    if include_rel_path:
        out["path"] = f"{kind}/{filename}"
    return out


def content_disposition_attachment(filename: str) -> str:
    """ADV-DL-005: safe Content-Disposition — no CR/LF/quotes; ASCII fallback + RFC5987."""
    name = Path(filename).name
    ascii_name = "".join(
        ch if (32 <= ord(ch) < 127 and ch not in '"\\;') else "_" for ch in name
    )
    if not ascii_name or ascii_name in (".", ".."):
        ascii_name = "download"
    starred = quote(name, safe="")
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{starred}'


def _list_one_at(
    handle: KindDirHandle,
    name: str,
    *,
    kind: str,
    include_rel_path: bool,
) -> dict[str, Any] | None:
    """Return public meta for one regular file via openat, or None to skip."""
    if is_denied_filename(name) or name.startswith("."):
        return None
    try:
        fd, st = handle.open_reg(name)
    except (DownloadError, OSError):
        return None
    try:
        digest = _sha256_fd(  # skylos: ignore[SKY-P401] bounded artifact hash via fd
            fd, size=st.st_size
        )
    finally:
        os.close(fd)
    return _public_meta(
        artifact_id=artifact_id_for_filename(name, kind=kind),
        filename=name,
        size=int(st.st_size),
        sha256=digest,
        created_at=float(st.st_mtime),
        kind=kind,
        include_rel_path=include_rel_path,
    )


def _open_kind_handle(
    *,
    kind: str,
    artifacts_root: Path | None,
    lease_id: str | None,
    create: bool,
    dir_path: Path | None = None,
) -> KindDirHandle:
    """ADV-DL-010/011/013/015: require walk kwargs; hold kind_fd for I/O."""
    if artifacts_root is not None and lease_id is not None:
        return walk_lease_kind_dir(
            artifacts_root, lease_id, kind, create=create
        )
    # ADV-DL-015: refuse dir_path-only (ancestor walk skipped).
    raise DownloadValidationError(_ERR_WALK_REQUIRED)


def list_artifacts(
    dir_path: Path | None = None,
    *,
    kind: str,
    include_rel_path: bool = False,
    artifacts_root: Path | None = None,
    lease_id: str | None = None,
) -> list[dict[str, Any]]:
    """List regular files under a lease artifact dir (no absolute paths)."""
    handle = _open_kind_handle(
        kind=kind,
        artifacts_root=artifacts_root,
        lease_id=lease_id,
        create=False,
        dir_path=dir_path,
    )
    try:
        items: list[dict[str, Any]] = []
        for name in handle.listdir():
            meta = _list_one_at(
                handle, name, kind=kind, include_rel_path=include_rel_path
            )
            if meta is not None:
                items.append(meta)
        return items
    finally:
        handle.close()


def read_artifact(
    dir_path: Path | None = None,
    artifact_id: str = "",
    *,
    kind: str,
    include_rel_path: bool = False,
    artifacts_root: Path | None = None,
    lease_id: str | None = None,
) -> tuple[dict[str, Any], bytes]:
    """Return (public meta, file bytes) for artifact_id under dir."""
    aid = validate_id_component(
        "artifact_id", artifact_id, pattern=_SAFE_ARTIFACT_RE
    )
    handle = _open_kind_handle(
        kind=kind,
        artifacts_root=artifacts_root,
        lease_id=lease_id,
        create=False,
        dir_path=dir_path,
    )
    try:
        for name in handle.listdir():
            if is_denied_filename(name) or name.startswith("."):
                continue
            if artifact_id_for_filename(name, kind=kind) != aid:
                continue
            fd, st = handle.open_reg(name)
            try:
                data = os.read(  # skylos: ignore[SKY-P401] size-capped artifact fetch
                    fd, st.st_size + 1
                )
                if len(data) > st.st_size:
                    raise DownloadValidationError("artifact size race")
                digest = hashlib.sha256(data).hexdigest()
            finally:
                os.close(fd)
            meta = _public_meta(
                artifact_id=aid,
                filename=name,
                size=len(data),
                sha256=digest,
                created_at=float(st.st_mtime),
                kind=kind,
                include_rel_path=include_rel_path,
            )
            return meta, data
        raise ArtifactNotFoundError(aid)
    finally:
        handle.close()


def _unique_upload_name(handle: KindDirHandle, name: str) -> str:
    """If ``name`` exists, return stem-xxxxxxxx.suffix (ADV-DL-004)."""
    if not handle.name_exists(name):
        return name
    stem = Path(name).stem
    suf = Path(name).suffix
    for _ in range(8):
        alt = f"{stem}-{uuid.uuid4().hex[:8]}{suf}"
        if not handle.name_exists(alt):
            return alt
    raise DownloadValidationError("upload name collision")


def write_upload(
    dir_path: Path | None = None,
    *,
    filename: str,
    data: bytes,
    kind: str = KIND_UPLOADS,
    artifacts_root: Path | None = None,
    lease_id: str | None = None,
) -> dict[str, Any]:
    """Write bytes into lease artifact dir (openat excl; ADV-DL-002/004/010–015)."""
    if kind not in _ARTIFACT_KINDS:
        raise DownloadValidationError("invalid artifact kind")
    name = sanitize_upload_filename(filename)
    if not isinstance(data, (bytes, bytearray)):
        raise DownloadValidationError("content must be bytes")
    payload = bytes(data)
    if len(payload) > max_upload_bytes():
        raise DownloadValidationError("upload too large")
    handle = _open_kind_handle(
        kind=kind,
        artifacts_root=artifacts_root,
        lease_id=lease_id,
        create=True,
        dir_path=dir_path,
    )
    try:
        name = _unique_upload_name(handle, name)
        handle.excl_create_write(name, payload)
        return _public_meta(
            artifact_id=artifact_id_for_filename(name, kind=kind),
            filename=name,
            size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            created_at=time.time(),
            kind=kind,
            include_rel_path=False,
        )
    finally:
        handle.close()
