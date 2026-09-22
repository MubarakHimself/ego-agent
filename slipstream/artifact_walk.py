"""openat|O_NOFOLLOW lease artifact ancestry walk (ADV-DL-010/011)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

_HAS_O_NOFOLLOW = hasattr(os, "O_NOFOLLOW")
_HAS_O_DIRECTORY = hasattr(os, "O_DIRECTORY")
_LEASES_DIRNAME = "leases"


def _dir_flags_nofollow() -> int:
    flags = os.O_RDONLY
    if _HAS_O_DIRECTORY:
        flags |= os.O_DIRECTORY
    if _HAS_O_NOFOLLOW:
        flags |= os.O_NOFOLLOW
    return flags


def _close_fd_quiet(fd: int | None) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass


def _stat_component_at(dir_fd: int, name: str) -> os.stat_result | None:
    """fstatat AT_SYMLINK_NOFOLLOW — None if missing."""
    from slipstream.downloads import DownloadValidationError

    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise DownloadValidationError(f"{name} stat failed") from e


def _openat_dir_nofollow(dir_fd: int, name: str) -> int:
    from slipstream.downloads import DownloadValidationError, _ERR_SYMLINK

    try:
        return os.open(  # skylos: ignore[SKY-D215,SKY-D325] openat O_DIR|O_NOFOLLOW component
            name, _dir_flags_nofollow(), dir_fd=dir_fd
        )
    except OSError as e:
        raise DownloadValidationError(_ERR_SYMLINK) from e


def _require_component_name(name: str) -> str:
    from slipstream.downloads import DownloadValidationError

    if (
        not name
        or name in (".", "..")
        or "/" in name
        or chr(92) in name
        or chr(0) in name
    ):
        raise DownloadValidationError("unsafe path component")
    return name


def _require_dir_stat(st: os.stat_result, name: str) -> None:
    from slipstream.downloads import DownloadValidationError, _ERR_SYMLINK

    if stat.S_ISLNK(st.st_mode):
        raise DownloadValidationError(_ERR_SYMLINK)
    if not stat.S_ISDIR(st.st_mode):
        raise DownloadValidationError(f"{name} is not a directory")


def _mkdirat_nofollow(dir_fd: int, name: str) -> None:
    from slipstream.downloads import DownloadValidationError

    try:
        os.mkdir(  # skylos: ignore[SKY-D215] mkdirat under validated parent fd
            name, 0o700, dir_fd=dir_fd
        )
    except FileExistsError:
        pass
    except OSError as e:
        raise DownloadValidationError(f"{name} mkdir failed") from e


def _descend_component(dir_fd: int, name: str, *, create: bool) -> int:
    """Refuse symlink at ``name``; optionally mkdirat; return child dir fd."""
    from slipstream.downloads import DownloadValidationError, _LABEL_ARTIFACT_DIR

    name = _require_component_name(name)
    st = _stat_component_at(dir_fd, name)
    if st is None:
        if not create:
            raise DownloadValidationError(f"{_LABEL_ARTIFACT_DIR} missing")
        _mkdirat_nofollow(dir_fd, name)
        st = _stat_component_at(dir_fd, name)
        if st is None:
            raise DownloadValidationError(f"{name} mkdir vanished")
    _require_dir_stat(st, name)
    return _openat_dir_nofollow(dir_fd, name)


def walk_lease_kind_dir(
    artifacts_root: Path,
    lease_id: str,
    kind: str,
    *,
    create: bool = False,
) -> Path:
    """openat+O_NOFOLLOW walk: artifacts_root → leases → {{id}} → kind.

    ADV-DL-010/011: refuse any symlink on intermediate components.
    """
    from slipstream.downloads import (
        DownloadValidationError,
        _ARTIFACT_KINDS,
        _SAFE_LEASE_RE,
        _resolve_policy_path,
        validate_id_component,
    )

    safe = validate_id_component("lease_id", lease_id, pattern=_SAFE_LEASE_RE)
    if kind not in _ARTIFACT_KINDS:
        raise DownloadValidationError("invalid artifact kind")
    root = _resolve_policy_path(artifacts_root)
    joined = root / _LEASES_DIRNAME / safe / kind
    root_fd = leases_fd = lease_fd = kind_fd = None
    try:
        try:
            root_fd = os.open(  # skylos: ignore[SKY-D215,SKY-D325] artifacts_root O_DIRECTORY
                os.fspath(root),
                os.O_RDONLY | (os.O_DIRECTORY if _HAS_O_DIRECTORY else 0),
            )
        except OSError as e:
            raise DownloadValidationError("artifacts_root open failed") from e
        leases_fd = _descend_component(root_fd, _LEASES_DIRNAME, create=create)
        lease_fd = _descend_component(leases_fd, safe, create=create)
        kind_fd = _descend_component(lease_fd, kind, create=create)
        try:
            os.fchmod(kind_fd, 0o700)
        except OSError:
            pass
        return joined
    finally:
        _close_fd_quiet(kind_fd)
        _close_fd_quiet(lease_fd)
        _close_fd_quiet(leases_fd)
        _close_fd_quiet(root_fd)
