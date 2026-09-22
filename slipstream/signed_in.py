"""Login-once signed-in badge helpers (metadata only — never cookies).

Space Chromium ``--user-data-dir`` already persists session cookies. This
module only validates the **badge** fields captains/agents may set after a
human Watch/Take-over login: ``signed_in`` + optional host label.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from slipstream.alerts import AlertValidationError, reject_secret_fields

MAX_HOST_CHARS = 128
_META_SIGNED_IN = "signed_in"
_META_SIGNED_IN_HOST = "signed_in_host"
_HOST_SAFE_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
_TRUE_SET = frozenset({"true", "1"})
_FALSE_SET = frozenset({"false", "0"})


class SignedInValidationError(ValueError):
    """Bad signed-in mark / host label (never secrets)."""


def _parse_signed_flag(flag: Any) -> bool:
    if isinstance(flag, bool):
        return flag
    if isinstance(flag, str):
        key = flag.strip().lower()
        if key in _TRUE_SET:
            return True
        if key in _FALSE_SET:
            return False
    raise SignedInValidationError("signed_in must be a boolean")


def _host_from_url(value: str) -> str:
    parsed = urlparse(value if "://" in value else f"https:{value}")
    if parsed.username or parsed.password:
        raise SignedInValidationError("host must not include credentials")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise SignedInValidationError("host URL missing hostname")
    return host


def _host_from_bare(value: str) -> str:
    host = value.split("/")[0].split("?")[0].strip().lower()
    if "@" in host:
        raise SignedInValidationError("host must not include credentials")
    return host


def normalize_signed_in_host(raw: Any) -> str:
    """Normalize optional host label (hostname or https URL → hostname)."""
    if not isinstance(raw, str):
        raise SignedInValidationError("host must be a string")
    value = raw.strip()
    if not value:
        raise SignedInValidationError("host must be non-empty when provided")
    if len(value) > MAX_HOST_CHARS:
        raise SignedInValidationError(f"host exceeds max length {MAX_HOST_CHARS}")
    host = (
        _host_from_url(value)
        if ("://" in value or value.startswith("//"))
        else _host_from_bare(value)
    )
    if not _HOST_SAFE_RE.match(host) or host in (".", "..") or host.startswith("."):
        raise SignedInValidationError("host is not a valid hostname label")
    return host


def validate_signed_in_body(raw: Any) -> tuple[bool, str | None]:
    """Parse POST body for mark/unmark. Returns ``(signed_in, host|None)``.

    Accepts ``{"signed_in": true|false|"true"|"false", "host"?: "…"}``.
    Refuses secret-like keys. Host is an optional label (hostname), not a
    cookie jar — never stored as credentials.
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise SignedInValidationError("body must be a JSON object")
    try:
        reject_secret_fields(raw)
    except AlertValidationError as e:
        raise SignedInValidationError(str(e)) from e
    if "signed_in" not in raw:
        raise SignedInValidationError("signed_in boolean required")
    signed = _parse_signed_flag(raw["signed_in"])
    host_raw = raw.get("host")
    if not signed or host_raw in (None, ""):
        return signed, None
    return True, normalize_signed_in_host(host_raw)


def metadata_signed_in_patch(*, signed_in: bool, host: str | None) -> dict[str, str]:
    """Keys to merge into Space user_metadata for q= filter (string leaves)."""
    if not signed_in:
        return {}
    out: dict[str, str] = {_META_SIGNED_IN: "true"}
    if host:
        out[_META_SIGNED_IN_HOST] = host
    return out


def strip_signed_in_metadata(meta: dict[str, Any]) -> dict[str, Any]:
    """Drop signed_in badge keys from a metadata copy (unmark)."""
    out = dict(meta)
    out.pop(_META_SIGNED_IN, None)
    out.pop(_META_SIGNED_IN_HOST, None)
    return out


def badge_from_registry(entry: dict[str, Any] | None) -> dict[str, Any]:
    """Public badge fields for Space/lease list (never cookies)."""
    if not entry or not entry.get("signed_in"):
        return {"signed_in": False}
    out: dict[str, Any] = {"signed_in": True}
    host = entry.get("host")
    if host:
        out["signed_in_host"] = host
    return out
