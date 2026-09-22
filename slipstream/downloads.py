"""Lease-scoped downloads / uploads as session artifacts (Browserbase-style).

Downloads land under ``{artifacts_root}/leases/{lease_id}/downloads/``
(outside Space user-data-dir and outside the credential vault). Agents see
artifact metadata (id, filename, bytes, sha256, created_at) and optional
lease-relative paths — never absolute host secret paths by default.

Chrome download behavior is configured via CDP ``Browser.setDownloadBehavior``
when not mock. Mock mode records the call for tests.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import time
import uuid
from dataclasses import dataclass, field
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
_SAFE_ARTIFACT_RE = re.compile(r"^(dl|up)_[A-Za-z0-9]{8,32}$")
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
_ERR_PATH_ESCAPE = "path escape refused"
_ERR_SYMLINK = "symlink escape refused"
_LABEL_ARTIFACT_DIR = "artifact dir"
_FLAG_O_NOFOLLOW = "O_NOFOLLOW"  # used in messages / feature probes

KIND_DOWNLOADS = "downloads"
_HAS_O_NOFOLLOW = hasattr(os, "O_NOFOLLOW")
_HAS_O_DIRECTORY = hasattr(os, "O_DIRECTORY")
KIND_UPLOADS = "uploads"
_ARTIFACT_KINDS = frozenset({KIND_DOWNLOADS, KIND_UPLOADS})


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
    # scrub_text strips secret-shaped substrings from operator labels
    cleaned = scrub_text(name)
    if cleaned != name:
        raise DownloadValidationError("filename refused by secret denylist")
    return name


def artifact_id_for_filename(filename: str, *, kind: str = KIND_DOWNLOADS) -> str:
    """Stable id from basename (dl_/up_ + sha256 prefix)."""
    prefix = "dl" if kind == KIND_DOWNLOADS else "up"
    digest = hashlib.sha256(filename.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def lease_artifacts_dir(artifacts_root: Path, lease_id: str, kind: str) -> Path:
    """Return ``{artifacts_root}/leases/{lease_id}/{downloads|uploads}/``.

    ADV-DL-002: resolve *only* ``artifacts_root``; join lease/kind without
    resolving so a directory symlink at ``kind`` cannot redirect before checks.
    """
    safe = validate_id_component("lease_id", lease_id, pattern=_SAFE_LEASE_RE)
    if kind not in _ARTIFACT_KINDS:
        raise DownloadValidationError("invalid artifact kind")
    root = _resolve_policy_path(artifacts_root)
    return root / "leases" / safe / kind


def _refuse_symlink_path(path: Path, *, label: str = "path") -> None:
    """lstat and refuse S_ISLNK (ADV-DL-002)."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as e:
        raise DownloadValidationError(f"{label} lstat failed") from e
    if stat.S_ISLNK(st.st_mode):
        raise DownloadValidationError(_ERR_SYMLINK)


def _open_dir_nofollow(path: Path) -> int:
    """Open directory nofollow; refuse symlink (ADV-DL-002)."""
    _refuse_symlink_path(path, label=_LABEL_ARTIFACT_DIR)
    flags = os.O_RDONLY
    if _HAS_O_DIRECTORY:
        flags |= os.O_DIRECTORY
    if _HAS_O_NOFOLLOW:
        flags |= os.O_NOFOLLOW
    try:
        return os.open(  # skylos: ignore[SKY-D215] O_DIRECTORY|O_NOFOLLOW kind dir
            os.fspath(path), flags
        )
    except OSError as e:
        raise DownloadValidationError(f"{_LABEL_ARTIFACT_DIR} open failed") from e


def ensure_lease_artifact_dirs(artifacts_root: Path, lease_id: str) -> dict[str, Path]:
    """Create lease downloads + uploads dirs (mode 0o700); refuse kind symlinks."""
    out: dict[str, Path] = {}
    for kind in (KIND_DOWNLOADS, KIND_UPLOADS):
        path = lease_artifacts_dir(artifacts_root, lease_id, kind)
        _refuse_symlink_path(path, label=kind)
        # Create parents without following a kind symlink (checked above).
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _refuse_symlink_path(path, label=kind)
        if not path.exists():
            path.mkdir(mode=0o700)
        _refuse_symlink_path(path, label=kind)
        # Confirm it opens as a real directory (not a raced symlink).
        fd = _open_dir_nofollow(path)
        os.close(fd)
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
        out[kind] = path
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
# Safe open / list
# ---------------------------------------------------------------------------


def _path_contained(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _open_reg_nofollow(path: Path, *, root: Path) -> tuple[int, os.stat_result]:
    """Open regular file nofollow; require containment under root."""
    # root is the kind dir (not resolved through a symlink — caller validated).
    root_r = root if root.is_absolute() else _resolve_policy_path(root)
    # Prefer realpath of a verified non-symlink directory for containment.
    try:
        if not root.is_symlink():
            root_r = _resolve_policy_path(root)
    except OSError:
        root_r = Path(root)
    flags = os.O_RDONLY
    if _HAS_O_NOFOLLOW:
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(  # skylos: ignore[SKY-D215] O_NOFOLLOW leaf; fstat REG + containment
            os.fspath(path), flags
        )
    except OSError as e:
        raise ArtifactNotFoundError("artifact open failed") from e
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            os.close(fd)
            raise DownloadValidationError("artifact must be a regular file")
        try:
            opened = Path(os.readlink(f"/proc/self/fd/{fd}"))
        except OSError:
            opened = Path(path).absolute()
        if not _path_contained(opened, root_r):
            os.close(fd)
            raise DownloadValidationError(_ERR_PATH_ESCAPE)
        return fd, st
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _prepare_kind_dir(dir_path: Path) -> Path:
    """Validate kind dir is a real directory (not symlink); return unreolved path.

    ADV-DL-002: never ``Path.resolve()`` the kind dir (follows dir symlink).
    """
    _refuse_symlink_path(dir_path, label=_LABEL_ARTIFACT_DIR)
    if not dir_path.exists():
        raise DownloadValidationError(f"{_LABEL_ARTIFACT_DIR} missing")
    fd = _open_dir_nofollow(dir_path)
    os.close(fd)
    return dir_path


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
        "kind": "download" if kind == KIND_DOWNLOADS else "upload",
    }
    if include_rel_path:
        # Lease-relative only — never absolute host paths.
        out["path"] = f"{kind}/{filename}"
    return out


def content_disposition_attachment(filename: str) -> str:
    """ADV-DL-005: safe Content-Disposition — no CR/LF/quotes; ASCII fallback + RFC5987."""
    name = Path(filename).name
    # Strip header-breaking / quoting characters.
    ascii_name = "".join(
        ch if (32 <= ord(ch) < 127 and ch not in '"\\;') else "_" for ch in name
    )
    if not ascii_name or ascii_name in (".", ".."):
        ascii_name = "download"
    starred = quote(name, safe="")
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{starred}'



def _list_one_artifact(
    root: Path,
    name: str,
    *,
    kind: str,
    include_rel_path: bool,
) -> dict[str, Any] | None:
    """Return public meta for one regular file, or None to skip."""
    if is_denied_filename(name) or name.startswith("."):
        return None
    candidate = root / name
    if candidate.is_symlink():
        return None
    try:
        if not candidate.is_file():
            return None
        fd, st = _open_reg_nofollow(candidate, root=root)
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


def list_artifacts(
    dir_path: Path,
    *,
    kind: str,
    include_rel_path: bool = False,
) -> list[dict[str, Any]]:
    """List regular files under a lease artifact dir (no absolute paths)."""
    try:
        root = _prepare_kind_dir(dir_path)
    except DownloadValidationError:
        if dir_path.is_symlink() or (
            dir_path.exists() and stat.S_ISLNK(os.lstat(dir_path).st_mode)
        ):
            raise
        return []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    items: list[dict[str, Any]] = []
    for name in names:
        meta = _list_one_artifact(
            root, name, kind=kind, include_rel_path=include_rel_path
        )
        if meta is not None:
            items.append(meta)
    return items


def read_artifact(
    dir_path: Path,
    artifact_id: str,
    *,
    kind: str,
    include_rel_path: bool = False,
) -> tuple[dict[str, Any], bytes]:
    """Return (public meta, file bytes) for artifact_id under dir."""
    aid = validate_id_component("artifact_id", artifact_id, pattern=_SAFE_ARTIFACT_RE)
    root = _prepare_kind_dir(dir_path)
    for name in os.listdir(root):
        if is_denied_filename(name) or name.startswith("."):
            continue
        if artifact_id_for_filename(name, kind=kind) != aid:
            continue
        candidate = root / name
        if candidate.is_symlink():
            raise DownloadValidationError(_ERR_SYMLINK)
        fd, st = _open_reg_nofollow(candidate, root=root)
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


def _unique_upload_name(root: Path, name: str) -> str:
    """If ``name`` exists, return stem-xxxxxxxx.suffix (ADV-DL-004)."""
    candidate = root / name
    if not candidate.exists() and not candidate.is_symlink():
        return name
    stem = Path(name).stem
    suf = Path(name).suffix
    for _ in range(8):
        alt = f"{stem}-{uuid.uuid4().hex[:8]}{suf}"
        alt_path = root / alt
        if not alt_path.exists() and not alt_path.is_symlink():
            return alt
    raise DownloadValidationError("upload name collision")



def _excl_create_write(dest: Path, payload: bytes) -> None:
    """O_EXCL|O_NOFOLLOW create+write under an already-validated kind dir."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if _HAS_O_NOFOLLOW:
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(  # skylos: ignore[SKY-D215] O_EXCL|O_NOFOLLOW under kind dir
            os.fspath(dest), flags, 0o600
        )
    except FileExistsError as e:
        raise DownloadValidationError("upload exists") from e
    except OSError as e:
        raise DownloadValidationError(_ERR_SYMLINK) from e
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass


def write_upload(
    dir_path: Path,
    *,
    filename: str,
    data: bytes,
    kind: str = KIND_UPLOADS,
) -> dict[str, Any]:
    """Write bytes into a lease artifact dir (nofollow|excl create).

    ADV-DL-002: refuse kind-dir symlink; do not resolve kind dir.
    ADV-DL-004: O_EXCL create; on collision use unique id filename.
    """
    if kind not in _ARTIFACT_KINDS:
        raise DownloadValidationError("invalid artifact kind")
    name = sanitize_upload_filename(filename)
    if not isinstance(data, (bytes, bytearray)):
        raise DownloadValidationError("content must be bytes")
    payload = bytes(data)
    if len(payload) > max_upload_bytes():
        raise DownloadValidationError("upload too large")
    _refuse_symlink_path(dir_path, label=_LABEL_ARTIFACT_DIR)
    dir_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not dir_path.exists():
        dir_path.mkdir(mode=0o700)
    root = _prepare_kind_dir(dir_path)
    name = _unique_upload_name(root, name)
    dest = root / name
    _refuse_symlink_path(dest, label="upload target")
    # Containment: dest must be direct child of unreolved kind dir.
    if dest.parent != root or dest.name != name:
        raise DownloadValidationError(_ERR_PATH_ESCAPE)
    _excl_create_write(dest, payload)
    return _public_meta(
        artifact_id=artifact_id_for_filename(name, kind=kind),
        filename=name,
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        created_at=time.time(),
        kind=kind,
        include_rel_path=False,
    )
