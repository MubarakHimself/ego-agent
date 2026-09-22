"""CAPTCHA / solve-status chips (ui-peers §5.11) — thin MVP.

Client or detect stub reports solving lifecycle on the alerts/activity bus.
Unsolved (explicit fail or timeout) escalates to need_human (reason=captcha)
with Watch / Take-over via the existing alerts spine. No paid captcha SaaS.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

from slipstream.alerts import (
    AlertValidationError,
    reject_secret_fields,
    scrub_text,
)

EVENT_STARTED = "captcha_solving_started"
EVENT_FINISHED = "captcha_solving_finished"
EVENT_FAILED = "captcha_solving_failed"

STATE_IDLE = "idle"
STATE_SOLVING = "solving"
STATE_SOLVED = "solved"
STATE_FAILED = "failed"
STATE_ESCALATED = "escalated"

_EVENT_ALIASES = {
    "started": EVENT_STARTED,
    "solving_started": EVENT_STARTED,
    "captcha_solving_started": EVENT_STARTED,
    "finished": EVENT_FINISHED,
    "solving_finished": EVENT_FINISHED,
    "captcha_solving_finished": EVENT_FINISHED,
    "failed": EVENT_FAILED,
    "solving_failed": EVENT_FAILED,
    "captcha_solving_failed": EVENT_FAILED,
}

DEFAULT_CAPTCHA_TIMEOUT_S = 60
REASON_TIMEOUT = "timeout"  # feed/alert reason leaf — not a secret

_CHIP_SOLVING = (
    '<span class="chip captcha solving" title="CAPTCHA solve in progress">'
    "CAPTCHA solving…</span>"
)
_CHIP_FAIL = (
    '<span class="chip captcha fail" title="CAPTCHA unsolved — need human">'
    "CAPTCHA — need human</span>"
)
_BANNER_SOLVING = (
    '<p class="captcha-banner solving" role="status">'
    "CAPTCHA challenge detected — solve in progress (no paid solver).</p>"
)
_BANNER_FAIL = (
    '<p class="captcha-banner fail" role="alert">'
    "CAPTCHA unsolved — agent paused; use Watch / Take-over.</p>"
)


def captcha_timeout_seconds() -> int:
    """SLIPSTREAM_CAPTCHA_TIMEOUT — seconds before unsolved escalate (default 60)."""
    raw = os.environ.get("SLIPSTREAM_CAPTCHA_TIMEOUT", "").strip()
    try:
        n = int(raw) if raw else DEFAULT_CAPTCHA_TIMEOUT_S
    except ValueError:
        n = DEFAULT_CAPTCHA_TIMEOUT_S
    return max(5, min(n, 600))


@dataclass
class CaptchaState:
    """Per-lease captcha chip state (in-memory; cleared on release)."""

    state: str = STATE_IDLE
    last_event: str | None = None
    detail: str = ""
    provider: str | None = None
    started_at: float | None = None
    timeout_s: int = field(default_factory=captcha_timeout_seconds)
    escalated: bool = False

    def to_public(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "state": self.state,
            "event": self.last_event,
            "detail": self.detail,
            "timeout_s": self.timeout_s,
            "escalated": self.escalated,
        }
        if self.provider:
            out["provider"] = self.provider
        if self.started_at is not None:
            out["started_at"] = self.started_at
            if self.state == STATE_SOLVING:
                remaining = max(
                    0, int(self.timeout_s - (time.time() - self.started_at))
                )
                out["timeout_remaining_s"] = remaining
        return out

    def is_timed_out(self, now: float | None = None) -> bool:
        if self.state != STATE_SOLVING or self.started_at is None:
            return False
        now = time.time() if now is None else now
        return (now - self.started_at) >= float(self.timeout_s)

    def shows_fail_chip(self) -> bool:
        if self.state in (STATE_FAILED, STATE_ESCALATED):
            return True
        return self.state == STATE_SOLVING and self.is_timed_out()

    def shows_solving_chip(self) -> bool:
        return self.state == STATE_SOLVING and not self.is_timed_out()


def normalize_captcha_event(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise AlertValidationError(
            "event must be one of started|finished|failed "
            f"(or captcha_solving_*); got {raw!r}"
        )
    key = raw.strip().lower().replace("-", "_")
    event = _EVENT_ALIASES.get(key)
    if event is None:
        raise AlertValidationError(
            "event must be one of started|finished|failed "
            f"(or captcha_solving_*); got {raw!r}"
        )
    return event


def _parse_timeout_s(raw: Any) -> int:
    if raw is None:
        return captcha_timeout_seconds()
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise AlertValidationError("timeout_s must be a positive integer")
    return max(5, min(int(raw), 600))


def _parse_optional_str(raw: Any, *, field: str, max_len: int) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise AlertValidationError(f"{field} must be a string")
    cleaned = scrub_text(raw.strip())[:max_len]
    return cleaned or None


def parse_captcha_request(body: dict[str, Any]) -> dict[str, Any]:
    """Validate POST /captcha body; scrub secrets; return normalized fields."""
    if not isinstance(body, dict):
        raise AlertValidationError("body must be a JSON object")
    reject_secret_fields(body)

    event = normalize_captcha_event(body.get("event"))
    detail_raw = body.get("detail", "")
    if detail_raw is None:
        detail_raw = ""
    if not isinstance(detail_raw, str):
        raise AlertValidationError("detail must be a string")
    if len(detail_raw) > 500:
        raise AlertValidationError("detail too long (max 500)")
    detail = scrub_text(detail_raw)

    for forbidden in ("status", "state", "watch_url", "escalated"):
        if forbidden in body:
            raise AlertValidationError(
                f"{forbidden} is server-derived; omit from request"
            )

    return {
        "event": event,
        "detail": detail,
        "provider": _parse_optional_str(body.get("provider"), field="provider", max_len=64),
        "timeout_s": _parse_timeout_s(body.get("timeout_s")),
    }


def apply_captcha_event(
    state: CaptchaState,
    *,
    event: str,
    detail: str,
    provider: str | None,
    timeout_s: int,
    now: float | None = None,
) -> CaptchaState:
    """Mutate state for a client-reported event. Does not escalate."""
    now = time.time() if now is None else now
    state.last_event = event
    state.detail = detail
    if provider:
        state.provider = provider
    state.timeout_s = timeout_s
    if event == EVENT_STARTED:
        state.state = STATE_SOLVING
        state.started_at = now
        state.escalated = False
    elif event == EVENT_FINISHED:
        state.state = STATE_SOLVED
    else:
        state.state = STATE_FAILED
    return state


def feed_outcome_for_event(event: str) -> str:
    if event == EVENT_STARTED:
        return "pending"
    if event == EVENT_FAILED:
        return "err"
    return "ok"


def captcha_chip_html(state: CaptchaState | None) -> str:
    """High-salience Watch chip HTML (empty when idle/solved)."""
    if state is None or state.state in (STATE_IDLE, STATE_SOLVED):
        return ""
    if state.shows_solving_chip():
        return _CHIP_SOLVING
    return _CHIP_FAIL


def captcha_banner_html(state: CaptchaState | None) -> str:
    """Optional banner under meta when captcha is active."""
    if state is None or state.state in (STATE_IDLE, STATE_SOLVED):
        return ""
    if state.shows_solving_chip():
        return _BANNER_SOLVING
    return _BANNER_FAIL


def mark_escalated(state: CaptchaState) -> None:
    state.state = STATE_ESCALATED
    state.escalated = True
