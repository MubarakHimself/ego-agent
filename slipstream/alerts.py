"""Lease-scoped alerts: need_human (pause, keep warm) + task_done (once, release).

Ship-now minimum only. Payloads NEVER carry cookies/tokens/creds/secret paths.
Captain chat gets a one-liner + Watch/Take-over link fields; harness gets JSON.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any

# Nairobi EAT for human-facing timestamps in payloads (box-local convention).
_EAT = timezone(timedelta(hours=3), name="EAT")

EVENT_NEED_HUMAN = "need_human"
EVENT_TASK_DONE = "task_done"
SHIP_EVENTS = frozenset({EVENT_NEED_HUMAN, EVENT_TASK_DONE})

REASONS = frozenset({"captcha", "login", "ambiguous_ui", "stuck", "other"})

# Keys that must never appear in alert request/response bodies (case-insensitive).
# Prefer exact / token-boundary matches over bare substring so benign keys like
# secretary_note are not false-positive rejects.
_FORBIDDEN_KEY_RE = re.compile(
    r"(?i)^(cookies?|passwords?|passwd|tokens?|secrets?|credentials?|"
    r"auth(_?headers?)?|authorization|api[_-]?keys?|"
    r"cookie[_-]?paths?|secret[_-]?paths?|cdp[_-]?auth|"
    r"session[_-]?cookies?|set-cookie|"
    r"private[_-]?keys?|jwts?|bearers?|access[_-]?keys?)$"
)

# Exact key names (after lower + hyphen→underscore) that trip rejection.
_FORBIDDEN_FRAGMENTS = frozenset(
    {
        "cookie",
        "cookies",
        "password",
        "passwd",
        "token",
        "tokens",
        "secret",
        "secrets",
        "credential",
        "credentials",
        "authorization",
        "auth_header",
        "auth_headers",
        "api_key",
        "cookie_path",
        "secret_path",
        "cdp_auth",
        "session_cookie",
        "set_cookie",
        "private_key",
        "jwt",
        "bearer",
        "access_key",
    }
)

# Whole path segments (split on _ / - / .) that count as secret stems.
_FORBIDDEN_SEGMENTS = frozenset(
    {
        "cookie",
        "cookies",
        "password",
        "passwd",
        "passwords",
        "token",
        "tokens",
        "secret",
        "secrets",
        "credential",
        "credentials",
        "authorization",
        "jwt",
        "jwts",
        "bearer",
        "bearers",
    }
)

# Adjacent segment pairs that form secret compounds (token-boundary).
_FORBIDDEN_SEGMENT_PAIRS = frozenset(
    {
        ("private", "key"),
        ("access", "key"),
        ("api", "key"),
        ("auth", "header"),
        ("auth", "headers"),
        ("session", "cookie"),
        ("session", "cookies"),
        ("cookie", "path"),
        ("secret", "path"),
        ("cdp", "auth"),
        ("set", "cookie"),
    }
)

# Light scrub of free-text detail/summary values (trust boundary: callers must
# not put secrets in text; we still redact common password=/cookie=/token= leaks).
_SCRUB_VALUE_RE = re.compile(
    r"(?i)\b(password|cookie|token)\s*=\s*\S+"
)

DEFAULT_WATCH_TTL_S = 300


class AlertValidationError(ValueError):
    """Bad alert request (unknown event, forbidden fields, bad reason, …)."""


class AlertConflictError(Exception):
    """Alert not allowed in current lease state (e.g. already done)."""


def new_event_id() -> str:
    return str(uuid.uuid4())


def now_ts() -> str:
    """ISO-8601 timestamp in Africa/Nairobi (EAT / UTC+3)."""
    return datetime.now(_EAT).isoformat(timespec="seconds")


def _key_forbidden(key: str) -> bool:
    """True if key looks secret-bearing (exact / token-boundary; not bare substring)."""
    if _FORBIDDEN_KEY_RE.match(key):
        return True
    lowered = key.lower().replace("-", "_")
    if lowered in _FORBIDDEN_FRAGMENTS:
        return True
    segments = [s for s in re.split(r"[_.\-]", lowered) if s]
    if any(seg in _FORBIDDEN_SEGMENTS for seg in segments):
        return True
    for i in range(len(segments) - 1):
        if (segments[i], segments[i + 1]) in _FORBIDDEN_SEGMENT_PAIRS:
            return True
    return False


def scrub_text(value: str) -> str:
    """Redact password=/cookie=/token= patterns in free-text fields."""
    if not value:
        return value
    return _SCRUB_VALUE_RE.sub(
        lambda m: m.group(1) + "=[REDACTED]",
        value,
    )


def reject_secret_fields(obj: Any, *, path: str = "") -> None:
    """Raise AlertValidationError if any secret-like key appears in obj."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise AlertValidationError("alert keys must be strings")
            if _key_forbidden(k):
                where = f"{path}.{k}" if path else k
                raise AlertValidationError(
                    f"refused secret-like field: {where}"
                )
            reject_secret_fields(v, path=f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            reject_secret_fields(item, path=f"{path}[{i}]")


def default_watch_url(lease_id: str, *, base_url: str = "http://127.0.0.1:8755") -> str:
    """Local placeholder Watch URL until live pair-browse UI ships."""
    return f"{base_url.rstrip('/')}/v1/leases/{lease_id}/watch"


def default_takeover_url(lease_id: str, *, base_url: str = "http://127.0.0.1:8755") -> str:
    """Local placeholder Take-over URL (observe → interactive later)."""
    return f"{base_url.rstrip('/')}/v1/leases/{lease_id}/watch?mode=takeover"


def format_captain_one_liner(
    *,
    event: str,
    reason: str | None,
    detail: str | None,
    outcome: dict[str, Any] | None,
    watch_url: str,
    takeover_url: str,
    lease_id: str,
) -> str:
    """Short captain-chat line with Watch / Take-over link fields."""
    short = lease_id[:8] if lease_id else "?"
    if event == EVENT_NEED_HUMAN:
        why = reason or "other"
        extra = f" — {detail}" if detail else ""
        return (
            f"Need human ({why}) on lease {short}…{extra} — "
            f"[Watch]({watch_url}) · [Take-over]({takeover_url})"
        )
    # task_done
    ok = True if outcome is None else bool(outcome.get("ok", True))
    summary = ""
    if isinstance(outcome, dict) and outcome.get("summary"):
        summary = str(outcome["summary"])
    label = "ok" if ok else "failed"
    tail = f": {summary}" if summary else ""
    return f"Task done ({label}) on lease {short}…{tail} — lease released"


def normalize_outcome(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise AlertValidationError("outcome must be an object or null")
    reject_secret_fields(raw, path="outcome")
    ok = raw.get("ok", True)
    if not isinstance(ok, bool):
        raise AlertValidationError("outcome.ok must be a boolean")
    summary = raw.get("summary", "")
    if summary is None:
        summary = ""
    if not isinstance(summary, str):
        raise AlertValidationError("outcome.summary must be a string")
    if len(summary) > 500:
        raise AlertValidationError("outcome.summary too long (max 500)")
    return {"ok": ok, "summary": scrub_text(summary)}


def parse_alert_request(body: dict[str, Any]) -> dict[str, Any]:
    """Validate caller body; return normalized fields (no secrets)."""
    if not isinstance(body, dict):
        raise AlertValidationError("body must be a JSON object")
    reject_secret_fields(body)

    event = body.get("event")
    if event not in SHIP_EVENTS:
        raise AlertValidationError(
            f"event must be one of {sorted(SHIP_EVENTS)}; got {event!r}"
        )

    reason = body.get("reason")
    if event == EVENT_NEED_HUMAN:
        if reason is None:
            reason = "other"
        if reason not in REASONS:
            raise AlertValidationError(
                f"reason must be one of {sorted(REASONS)}; got {reason!r}"
            )
    else:
        # task_done: reason optional / ignored for ship-now
        if reason is not None and reason not in REASONS:
            raise AlertValidationError(
                f"reason must be one of {sorted(REASONS)}; got {reason!r}"
            )

    detail = body.get("detail")
    if detail is None:
        detail = ""
    if not isinstance(detail, str):
        raise AlertValidationError("detail must be a string")
    if len(detail) > 500:
        raise AlertValidationError("detail too long (max 500)")
    detail = scrub_text(detail)

    task_id = body.get("task_id")
    if task_id is not None and not isinstance(task_id, str):
        raise AlertValidationError("task_id must be a string")

    ttl_s = body.get("ttl_s", DEFAULT_WATCH_TTL_S)
    if isinstance(ttl_s, bool) or not isinstance(ttl_s, int) or ttl_s <= 0:
        raise AlertValidationError("ttl_s must be a positive integer")
    if ttl_s > 3600:
        ttl_s = 3600

    outcome = normalize_outcome(body.get("outcome"))
    if event == EVENT_NEED_HUMAN and outcome is not None:
        raise AlertValidationError("need_human must not include outcome")

    # Client must not override watch_url or status — server derives both.
    if "watch_url" in body:
        raise AlertValidationError("watch_url is server-derived; omit from request")
    if "status" in body:
        raise AlertValidationError("status is server-derived; omit from request")

    return {
        "event": event,
        "reason": reason,
        "detail": detail,
        "task_id": task_id,
        "ttl_s": ttl_s,
        "outcome": outcome,
    }


def build_alert_payload(
    *,
    lease_id: str,
    space_id: str,
    parsed: dict[str, Any],
    base_url: str = "http://127.0.0.1:8755",
    event_id: str | None = None,
    ts: str | None = None,
) -> dict[str, Any]:
    """Assemble the public alert payload (never includes secrets)."""
    event = parsed["event"]
    watch = default_watch_url(lease_id, base_url=base_url)
    if event == EVENT_NEED_HUMAN:
        status = "awaiting_human"
        outcome = None
    else:
        outcome = parsed.get("outcome") or {"ok": True, "summary": ""}
        status = "done" if outcome.get("ok") else "failed"

    payload: dict[str, Any] = {
        "event": event,
        "event_id": event_id or new_event_id(),
        "ts": ts or now_ts(),
        "lease_id": lease_id,
        "space_id": space_id,
        "task_id": parsed.get("task_id"),
        "reason": parsed.get("reason"),
        "detail": parsed.get("detail") or "",
        "status": status,
        "watch_url": watch,
        "ttl_s": parsed["ttl_s"],
        "outcome": outcome,
    }
    # Drop null task_id for cleaner harness JSON? Spec allows optional — keep key.
    return payload


def build_harness_envelope(
    payload: dict[str, Any],
    *,
    lease_kept: bool,
    lease_released: bool,
    agent_paused: bool,
    action: str,
    takeover_url: str,
    idempotent: bool = False,
) -> dict[str, Any]:
    """Harness-facing JSON so the caller pauses / awaits / continues."""
    captain = format_captain_one_liner(
        event=payload["event"],
        reason=payload.get("reason"),
        detail=payload.get("detail"),
        outcome=payload.get("outcome"),
        watch_url=payload["watch_url"],
        takeover_url=takeover_url,
        lease_id=payload["lease_id"],
    )
    return {
        "alert": payload,
        "harness": {
            "action": action,
            "agent_paused": agent_paused,
            "lease_kept": lease_kept,
            "lease_released": lease_released,
            "captain_message": captain,
            "watch_url": payload["watch_url"],
            "takeover_url": takeover_url,
            "idempotent": idempotent,
        },
    }

