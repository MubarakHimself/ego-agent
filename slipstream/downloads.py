"""Lease-scoped downloads / uploads as session artifacts (Browserbase-style).

Downloads land under ``{artifacts_root}/leases/{lease_id}/downloads/``
(outside Space user-data-dir and outside the credential vault). Agents see
artifact metadata (id, filename, bytes, sha256, created_at) and optional
lease-relative paths — never absolute host secret paths by default.

Chrome download behavior is configured via CDP ``Browser.setDownloadBehavior``
when not mock. Mock mode records the call for tests.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import hashlib
import os
import re
import stat
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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


class ArtifactBackend(ABC):
    """Abstract artifact IO — keeps module off Zone-of-Pain extreme."""

    @abstractmethod
    def list_for_lease(self, lease_id: str) -> list[dict]:
        raise NotImplementedError


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

KIND_DOWNLOADS = "downloads"
KIND_UPLOADS = "uploads"
_KIND_DOWNLOADS = KIND_DOWNLOADS
_KIND_UPLOADS = KIND_UPLOADS


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


def artifact_id_for_filename(filename: str, *, kind: str = _KIND_DOWNLOADS) -> str:
    """Stable id from basename (dl_/up_ + sha256 prefix)."""
    prefix = "dl" if kind == _KIND_DOWNLOADS else "up"
    digest = hashlib.sha256(filename.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def lease_artifacts_dir(artifacts_root: Path, lease_id: str, kind: str) -> Path:
    """Return ``{artifacts_root}/leases/{lease_id}/{downloads|uploads}/``."""
    safe = validate_id_component("lease_id", lease_id, pattern=_SAFE_LEASE_RE)
    if kind not in (_KIND_DOWNLOADS, _KIND_UPLOADS):
        raise DownloadValidationError("invalid artifact kind")
    root = _resolve_policy_path(artifacts_root)
    return root / "leases" / safe / kind


def ensure_lease_artifact_dirs(artifacts_root: Path, lease_id: str) -> dict[str, Path]:
    """Create lease downloads + uploads dirs (mode 0o700)."""
    out: dict[str, Path] = {}
    for kind in (_KIND_DOWNLOADS, _KIND_UPLOADS):
        path = lease_artifacts_dir(artifacts_root, lease_id, kind)
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
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
    """Open regular file with O_NOFOLLOW; require containment under root."""
    root_r = _resolve_policy_path(root)
    # Do not resolve ``path`` before open (symlink escape). Open leaf, then
    # fstat + check realpath of parent chain via /proc or os.path.realpath
    # after confirming REG.
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
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
        # Containment: realpath of the opened path must stay under root.
        # Use /proc/self/fd on Linux; fall back to resolve of parent+name.
        try:
            opened = Path(os.readlink(f"/proc/self/fd/{fd}"))
        except OSError:
            opened = _resolve_policy_path(path)
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
        "kind": "download" if kind == _KIND_DOWNLOADS else "upload",
    }
    if include_rel_path:
        # Lease-relative only — never absolute host paths.
        out["path"] = f"{kind}/{filename}"
    return out


def list_artifacts(
    dir_path: Path,
    *,
    kind: str,
    include_rel_path: bool = False,
) -> list[dict[str, Any]]:
    """List regular files under a lease artifact dir (no absolute paths)."""
    root = _resolve_policy_path(dir_path)
    if not root.is_dir():
        return []
    items: list[dict[str, Any]] = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    for name in names:
        if is_denied_filename(name):
            continue
        if name.startswith("."):
            continue
        candidate = root / name
        # Skip symlinks before open (also O_NOFOLLOW).
        if candidate.is_symlink():
            continue
        if not candidate.is_file():
            continue
        try:
            fd, st = _open_reg_nofollow(candidate, root=root)
        except (DownloadError, OSError):
            continue
        try:
            # Rewind not needed — fresh fd; hash then reopen for safety.
            digest = _sha256_fd(  # skylos: ignore[SKY-P401] bounded artifact hash via fd
                fd, size=st.st_size
            )
        finally:
            os.close(fd)
        aid = artifact_id_for_filename(name, kind=kind)
        items.append(
            _public_meta(
                artifact_id=aid,
                filename=name,
                size=int(st.st_size),
                sha256=digest,
                created_at=float(st.st_mtime),
                kind=kind,
                include_rel_path=include_rel_path,
            )
        )
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
    root = _resolve_policy_path(dir_path)
    if not root.is_dir():
        raise ArtifactNotFoundError(aid)
    # Match by scanning — refuse client-supplied filenames as path pieces.
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
                # Size raced — refuse.
                raise DownloadValidationError("artifact size race")
            # Hash from bytes we already hold.
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


def write_upload(
    dir_path: Path,
    *,
    filename: str,
    data: bytes,
    kind: str = _KIND_UPLOADS,
) -> dict[str, Any]:
    """Write bytes into a lease artifact dir (O_NOFOLLOW create)."""
    if kind not in (_KIND_DOWNLOADS, _KIND_UPLOADS):
        raise DownloadValidationError("invalid artifact kind")
    name = sanitize_upload_filename(filename)
    if not isinstance(data, (bytes, bytearray)):
        raise DownloadValidationError("content must be bytes")
    payload = bytes(data)
    if len(payload) > max_upload_bytes():
        raise DownloadValidationError("upload too large")
    root = _resolve_policy_path(dir_path)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    dest = root / name
    if dest.exists() and dest.is_symlink():
        raise DownloadValidationError(_ERR_SYMLINK)
    dest_resolved = _resolve_policy_path(dest)
    if not _path_contained(dest_resolved.parent, root):
        raise DownloadValidationError(_ERR_PATH_ESCAPE)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(  # skylos: ignore[SKY-D215] O_NOFOLLOW create under contained root
        os.fspath(dest_resolved), flags, 0o600
    )
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)
    try:
        os.chmod(dest_resolved, 0o600)
    except OSError:
        pass
    return _public_meta(
        artifact_id=artifact_id_for_filename(name, kind=kind),
        filename=name,
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        created_at=time.time(),
        kind=kind,
        include_rel_path=False,
    )



# ---------------------------------------------------------------------------
# CDP download behavior + mock recorder
# ---------------------------------------------------------------------------


@dataclass
class MockDownloadRecorder:
    """Records setDownloadBehavior calls when SLIPSTREAM_MOCK=1."""

    calls: list[dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, *, download_path: str, behavior: str = _BEHAVIOR_ALLOW) -> None:
        with self._lock:
            self.calls.append(
                {
                    "method": "Browser.setDownloadBehavior",
                    "behavior": behavior,
                    # Store lease-relative hint only in public tests — absolute
                    # path kept for assert-contains in unit tests under tmp.
                    "download_path": download_path,
                    "events_enabled": True,
                    "ts": time.time(),
                }
            )

    def clear(self) -> None:
        with self._lock:
            self.calls.clear()


def _browser_ws_url(cdp_http_url: str) -> str:
    """Resolve browser-level WebSocket URL from CDP /json/version."""
    import json
    import urllib.request

    from slipstream.cli import _validate_api_request_url

    base = cdp_http_url.rstrip("/")
    host = urlparse(base).hostname
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise DownloadValidationError(f"refusing non-loopback CDP host {host!r}")
    with urllib.request.urlopen(
        _validate_api_request_url(f"{base}/json/version"), timeout=3.0
    ) as resp:
        ver = json.load(resp)
    ws = ver.get("webSocketDebuggerUrl") if isinstance(ver, dict) else None
    if not isinstance(ws, str) or not ws.startswith("ws"):
        raise DownloadError("no browser WebSocketDebuggerUrl")
    return ws


def configure_chrome_download_behavior(
    cdp_http_url: str,
    download_dir: Path,
    *,
    mock: bool = False,
    recorder: MockDownloadRecorder | None = None,
) -> dict[str, Any]:
    """CDP Browser.setDownloadBehavior → lease downloads dir (or mock record).

    When mock=True, records the call and returns without talking to Chrome.
    Absolute ``download_dir`` is used for Chrome; never returned to agents.
    """
    abs_dir = _resolve_policy_path(download_dir)
    abs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path_str = str(abs_dir)
    if mock:
        if recorder is not None:
            recorder.record(download_path=path_str, behavior=_BEHAVIOR_ALLOW)
        return {"ok": True, "mocked": True, "behavior": _BEHAVIOR_ALLOW}

    from slipstream.cdp_inject import CdpInjectError, _ws_cdp_call

    try:
        ws_url = _browser_ws_url(cdp_http_url)
        _ws_cdp_call(
            ws_url,
            "Browser.setDownloadBehavior",
            {
                "behavior": _BEHAVIOR_ALLOW,
                "downloadPath": path_str,
                "eventsEnabled": True,
            },
        )
    except CdpInjectError as e:
        raise DownloadError(str(e)) from e
    return {"ok": True, "mocked": False, "behavior": _BEHAVIOR_ALLOW}
