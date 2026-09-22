"""Browserbase-style user_metadata tags + q= filter for Spaces/leases.

Thin ops tagging for fleets. Nested JSON objects with string leaves only.
Serialized size ≤512 chars. Never store secrets (reuse alerts denylist).
"""

from __future__ import annotations

import json
import re
from typing import Any

from slipstream.alerts import AlertValidationError, reject_secret_fields

MAX_METADATA_CHARS = 512
MAX_DEPTH = 8
MAX_KEYS = 64

# Browserbase-ish: user_metadata['env']:'staging'
_BB_EQ_RE = re.compile(
    r"^user_metadata((?:\[['\"][^'\"]+['\"]\])+)\s*:\s*['\"](.*)['\"]\s*$"
)
_BB_PATH_SEG_RE = re.compile(r"\[['\"]([^'\"]+)['\"]\]")


class MetadataValidationError(ValueError):
    """Bad user_metadata payload (type, size, secrets, depth)."""


def _serialized_len(obj: Any) -> int:
    return len(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))


def _reject_bad_leaf(obj: Any) -> None:
    if isinstance(obj, str):
        return
    if isinstance(obj, list):
        raise MetadataValidationError("user_metadata arrays are not supported")
    if obj is None or isinstance(obj, (bool, int, float)):
        raise MetadataValidationError(
            "user_metadata values must be strings (convert numbers/bools to strings)"
        )
    raise MetadataValidationError(
        f"user_metadata values must be strings or nested objects, got {type(obj).__name__}"
    )


def _walk_validate(obj: Any, *, depth: int, key_count: list[int]) -> None:
    if depth > MAX_DEPTH:
        raise MetadataValidationError(
            f"user_metadata nesting exceeds max depth {MAX_DEPTH}"
        )
    if not isinstance(obj, dict):
        _reject_bad_leaf(obj)
        return
    for k, v in obj.items():
        if not isinstance(k, str) or not k:
            raise MetadataValidationError("user_metadata keys must be non-empty strings")
        key_count[0] += 1
        if key_count[0] > MAX_KEYS:
            raise MetadataValidationError(f"user_metadata exceeds max keys {MAX_KEYS}")
        _walk_validate(v, depth=depth + 1, key_count=key_count)


def validate_user_metadata(raw: Any) -> dict[str, Any]:
    """Validate and return a deep copy of user_metadata."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise MetadataValidationError("user_metadata must be a JSON object")
    try:
        reject_secret_fields(raw)
    except AlertValidationError as e:
        raise MetadataValidationError(str(e)) from e
    key_count = [0]
    _walk_validate(raw, depth=0, key_count=key_count)
    size = _serialized_len(raw)
    if size > MAX_METADATA_CHARS:
        raise MetadataValidationError(
            f"user_metadata serialized length {size} exceeds {MAX_METADATA_CHARS}"
        )
    return json.loads(json.dumps(raw))


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge override onto base; nested dicts merge, leaf strings override."""
    out: dict[str, Any] = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def effective_metadata(
    space_meta: dict[str, Any] | None,
    lease_override: dict[str, Any] | None,
) -> dict[str, Any]:
    """Space tags inherited by lease; lease override wins on conflict.

    ADV-META-002 / ADV-META-002-GAP: after Space∪lease deep_merge, re-run full
    ``validate_user_metadata`` (size, key-count, depth, secrets) — fail closed.
    """
    merged = deep_merge(space_meta or {}, lease_override or {})
    try:
        return validate_user_metadata(merged)
    except MetadataValidationError as e:
        # Clarify merge context when size/keys blow the cap.
        msg = str(e)
        if "exceeds" in msg and "after Space" not in msg:
            raise MetadataValidationError(
                f"effective user_metadata invalid after Space∪lease merge: {msg}"
            ) from e
        raise


def _lookup_path(meta: dict[str, Any], parts: list[str]) -> Any:
    cur: Any = meta
    for p in parts:
        if not isinstance(cur, dict) or p not in cur:
            return None
        cur = cur[p]
    return cur


def _leaf_strings(obj: Any) -> list[str]:
    out: list[str] = []
    if isinstance(obj, dict):
        for v in obj.values():
            out.extend(_leaf_strings(v))
    elif isinstance(obj, str):
        out.append(obj)
    return out


def _strip_value_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _parse_dotted_kv(token: str, sep: str) -> tuple[list[str], str] | None:
    left, right = token.split(sep, 1)
    left, right = left.strip(), _strip_value_quotes(right.strip())
    if not left or left == "user_metadata":
        return None
    if left.startswith("user_metadata."):
        left = left[len("user_metadata.") :]
    parts = [p for p in left.split(".") if p]
    if not parts:
        return None
    return parts, right


def _parse_kv_token(token: str) -> tuple[list[str], str] | None:
    """Parse key=value / key:value / dotted path. Returns (path_parts, value) or None."""
    token = token.strip()
    if not token:
        return None
    m = _BB_EQ_RE.match(token)
    if m:
        parts = _BB_PATH_SEG_RE.findall(m.group(1))
        if parts:
            return parts, m.group(2)
    for sep in ("=", ":"):
        if sep in token:
            parsed = _parse_dotted_kv(token, sep)
            if parsed is not None:
                return parsed
    return None


def metadata_matches(meta: dict[str, Any], q: str | None) -> bool:
    """Return True if meta matches query string q (space-separated AND)."""
    if not q or not q.strip():
        return True
    for tok in q.split():
        parsed = _parse_kv_token(tok)
        if parsed is not None:
            parts, value = parsed
            if _lookup_path(meta, parts) != value:
                return False
            continue
        if not any(tok in s for s in _leaf_strings(meta)):
            return False
    return True


def combine_q(q: str | None, tags: list[str] | None) -> str | None:
    """Merge CLI ``--q`` and repeated ``--tag key=value`` into one query string."""
    parts: list[str] = []
    if q and q.strip():
        parts.append(q.strip())
    for t in tags or []:
        t = t.strip()
        if not t:
            continue
        if "=" not in t and ":" not in t:
            raise MetadataValidationError(
                f"--tag must be key=value or key:value, got {t!r}"
            )
        parts.append(t)
    return " ".join(parts) if parts else None
