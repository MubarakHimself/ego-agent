"""Thin confirm-actions / permission ladder (server-enforced).

Gated categories: fill | eval | download | upload | nav_irreversible.
Soft browse (snapshot/click/scroll/wait) is free — not gated here.

Gated act → confirmation_required + sibling need_human on the same alerts bus
(kind=confirmation_required + confirm_id). Confirm|deny within ~60s; auto-deny
on expiry. Non-TTY interactive confirm → deny.

ADV-PL-001 (server-enforced on pool HTTP): confirm grants a **one-shot**
allowance for that category on the lease. Pool helpers for ``fill``
(credentials fill), ``eval`` (Runtime.evaluate / script), and
``nav_irreversible`` (all pool navigates) **refuse** without a prior confirm
for that category — no CDP side-effect. Unused grants TTL with the same
confirm window (``SLIPSTREAM_CONFIRM_TTL``). Deny / timeout stay fail-closed.
Domain allowlist on navigate remains enforced.

Raw CDP: lease responses omit ``cdp_http_url`` / ``cdp_ws_url`` by default
(Firstmate B). ``SLIPSTREAM_EXPOSE_RAW_CDP=1`` restores them; on that path
navigation/eval are agent-trust / honor-system (vault fill stays pool-only).

Defer: once/always/never policy matrix, Comet UI.
Pattern from agent-browser Security docs (no code theft).
"""

from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any

from slipstream.alerts import AlertValidationError, reject_secret_fields, scrub_text

# Nairobi EAT timestamps for expires_at (box-local convention).
_EAT = timezone(timedelta(hours=3), name="EAT")

CATEGORIES = frozenset({"fill", "eval", "download", "upload", "nav_irreversible"})
ENFORCED_CDP_CATEGORIES = frozenset({"fill", "eval", "nav_irreversible"})
MAX_EVAL_EXPRESSION_CHARS = 4000
KIND_CONFIRMATION_REQUIRED = "confirmation_required"
DEFAULT_CONFIRM_TTL_S = 60

STATUS_PENDING = "pending"
STATUS_CONFIRMED = "confirmed"
STATUS_DENIED = "denied"
STATUS_EXPIRED = "expired"


class ActionValidationError(ValueError):
    """Bad action request (unknown category, secrets, bad summary)."""


class ConfirmationNotFoundError(Exception):
    """Unknown confirm_id."""


class ConfirmationGoneError(Exception):
    """Confirmation already resolved or expired."""


class LadderGateError(Exception):
    """CDP path refused — no one-shot confirmation allowance for category."""

    def __init__(self, category: str):
        self.category = category
        super().__init__(f"confirmation required for category {category!r}")


def confirm_ttl_seconds() -> int:
    """TTL for pending confirmations (env SLIPSTREAM_CONFIRM_TTL, default 60)."""
    raw = os.environ.get("SLIPSTREAM_CONFIRM_TTL", "").strip()
    if not raw:
        return DEFAULT_CONFIRM_TTL_S
    try:
        n = int(raw)
    except ValueError:
        return DEFAULT_CONFIRM_TTL_S
    if n <= 0:
        return DEFAULT_CONFIRM_TTL_S
    return min(n, 3600)


def expose_raw_cdp() -> bool:
    """When True, lease/status wire JSON includes raw CDP tip (urls + ports + pid).

    Default False (Firstmate B / ADV-PL-001-BYPASS-RAW-CDP-PORT): agents drive
    via pool HTTP so consume_once is real. Public status/lease omit
    ``cdp_http_url`` / ``cdp_ws_url`` / ``cdp_port`` / ``cdp_base_port`` /
    ``chromium_pid`` so agents cannot reconstruct ``http://127.0.0.1:{port}``
    or derive CDP from public JSON. Escape hatch ``SLIPSTREAM_EXPOSE_RAW_CDP=1``
    restores them; ladder is honor-system for nav/eval on that path (vault fill
    stays pool-only).
    """
    return os.environ.get("SLIPSTREAM_EXPOSE_RAW_CDP", "").strip() == "1"


def mint_confirm_id() -> str:
    return "c_" + secrets.token_hex(8)


def _iso_eat(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=_EAT).isoformat(timespec="seconds")


def parse_action_request(body: dict[str, Any]) -> dict[str, Any]:
    """Validate gated-act body; return {category, summary}. Never keeps secrets."""
    if not isinstance(body, dict):
        raise ActionValidationError("body must be a JSON object")
    try:
        reject_secret_fields(body)
    except AlertValidationError as e:
        raise ActionValidationError(str(e)) from e

    category = body.get("category")
    if not isinstance(category, str) or category not in CATEGORIES:
        raise ActionValidationError(
            f"category must be one of {sorted(CATEGORIES)}; got {category!r}"
        )

    summary = body.get("summary", "")
    if summary is None:
        summary = ""
    if not isinstance(summary, str):
        raise ActionValidationError("summary must be a string")
    if len(summary) > 500:
        raise ActionValidationError("summary too long (max 500)")
    summary = scrub_text(summary.strip())
    if not summary:
        raise ActionValidationError("summary must be non-empty (no secrets)")

    # Client must not pass confirm_id / status / watch_url — server-derived.
    for forbidden in ("confirm_id", "status", "watch_url", "expires_at", "lease_id"):
        if forbidden in body:
            raise ActionValidationError(
                f"{forbidden} is server-derived; omit from request"
            )

    out: dict[str, Any] = {"category": category, "summary": summary}
    # Optional url for nav_irreversible (pool allowlist gate); scrub if present.
    url = body.get("url")
    if url is not None:
        if not isinstance(url, str):
            raise ActionValidationError("url must be a string")
        url = scrub_text(url.strip())
        if url:
            out["url"] = url
    return out


def parse_confirmation_action(body: dict[str, Any]) -> str:
    """Validate confirm|deny body; return normalized action string."""
    if not isinstance(body, dict):
        raise ActionValidationError("body must be a JSON object")
    try:
        reject_secret_fields(body)
    except AlertValidationError as e:
        raise ActionValidationError(str(e)) from e
    action = body.get("action")
    if not isinstance(action, str):
        raise ActionValidationError("action must be 'confirm' or 'deny'")
    norm = action.strip().lower()
    if norm not in ("confirm", "deny"):
        raise ActionValidationError("action must be 'confirm' or 'deny'")
    return norm


@dataclass
class PendingConfirmation:
    confirm_id: str
    lease_id: str
    category: str
    summary: str
    created_at: float
    expires_at: float
    status: str = STATUS_PENDING
    # Sibling need_human alert event_id (for correlation); never secrets.
    alert_event_id: str | None = None

    def alive(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return self.status == STATUS_PENDING and now < self.expires_at

    def to_public(self) -> dict[str, Any]:
        """Wire shape for confirmation_required / resolve responses (no secrets)."""
        return {
            "status": (
                KIND_CONFIRMATION_REQUIRED
                if self.status == STATUS_PENDING
                else self.status
            ),
            "confirm_id": self.confirm_id,
            "category": self.category,
            "summary": self.summary,
            "lease_id": self.lease_id,
            "expires_at": _iso_eat(self.expires_at),
        }


@dataclass
class ConfirmationStore:
    """In-memory pending confirmations + one-shot CDP allowances."""

    _by_id: dict[str, PendingConfirmation] = field(default_factory=dict)
    _by_lease: dict[str, list[str]] = field(default_factory=dict)
    # (lease_id, category) → remaining one-shot grants after confirm.
    _allow_counts: dict[tuple[str, str], int] = field(default_factory=dict)
    # (lease_id, category) → unix expiry for unused grants (confirm TTL).
    _allow_expires: dict[tuple[str, str], float] = field(default_factory=dict)

    def add(self, pending: PendingConfirmation) -> None:
        self._by_id[pending.confirm_id] = pending
        self._by_lease.setdefault(pending.lease_id, []).append(pending.confirm_id)

    def get(self, confirm_id: str) -> PendingConfirmation | None:
        return self._by_id.get(confirm_id)

    def lease_pending(self, lease_id: str, *, now: float | None = None) -> list[PendingConfirmation]:
        now = time.time() if now is None else now
        out: list[PendingConfirmation] = []
        for cid in list(self._by_lease.get(lease_id, [])):
            p = self._by_id.get(cid)
            if p is not None and p.alive(now):
                out.append(p)
        return out

    def _purge_expired_grants(self, now: float) -> None:
        """Drop unused allowances past confirm TTL (ADV-PL-001-IMMORTAL-GRANT)."""
        for key in list(self._allow_expires):
            if now >= self._allow_expires[key]:
                self._allow_expires.pop(key, None)
                self._allow_counts.pop(key, None)

    def expire_due(self, now: float | None = None) -> list[PendingConfirmation]:
        """Mark overdue pending as expired; TTL unused grants; return newly expired pending."""
        now = time.time() if now is None else now
        newly: list[PendingConfirmation] = []
        for p in list(self._by_id.values()):
            if p.status == STATUS_PENDING and now >= p.expires_at:
                p.status = STATUS_EXPIRED
                newly.append(p)
        self._purge_expired_grants(now)
        return newly

    def grant_once(self, lease_id: str, category: str, *, now: float | None = None) -> None:
        """Captain confirm → one CDP act of this category may proceed (TTL'd)."""
        now = time.time() if now is None else now
        self._purge_expired_grants(now)
        key = (lease_id, category)
        self._allow_counts[key] = self._allow_counts.get(key, 0) + 1
        # Fresh confirm refreshes the unused-grant window (same as pending TTL).
        self._allow_expires[key] = now + float(confirm_ttl_seconds())

    def has_allowance(self, lease_id: str, category: str, *, now: float | None = None) -> bool:
        """True if a non-expired one-shot grant remains (does not consume)."""
        now = time.time() if now is None else now
        self._purge_expired_grants(now)
        return self._allow_counts.get((lease_id, category), 0) > 0

    def consume_once(self, lease_id: str, category: str, *, now: float | None = None) -> None:
        """Consume one-shot allowance or raise LadderGateError (fail-closed)."""
        now = time.time() if now is None else now
        self._purge_expired_grants(now)
        key = (lease_id, category)
        n = self._allow_counts.get(key, 0)
        if n <= 0:
            raise LadderGateError(category)
        if n == 1:
            del self._allow_counts[key]
            self._allow_expires.pop(key, None)
        else:
            self._allow_counts[key] = n - 1

    def refund_once(self, lease_id: str, category: str, *, now: float | None = None) -> None:
        """Restore one allowance after post-consume failure (ADV-PL-001-CONSUME)."""
        self.grant_once(lease_id, category, now=now)

    def clear_lease(self, lease_id: str) -> None:
        """Drop allowances when a lease leaves the pool."""
        for key in list(self._allow_counts):
            if key[0] == lease_id:
                del self._allow_counts[key]
                self._allow_expires.pop(key, None)


def parse_eval_request(body: dict[str, Any]) -> dict[str, Any]:
    """Validate Runtime.evaluate body; return {expression}. Never keeps secrets."""
    if not isinstance(body, dict):
        raise ActionValidationError("body must be a JSON object")
    try:
        reject_secret_fields(body)
    except AlertValidationError as e:
        raise ActionValidationError(str(e)) from e
    expression = body.get("expression")
    if not isinstance(expression, str) or not expression.strip():
        raise ActionValidationError("expression must be a non-empty string")
    expression = expression.strip()
    if len(expression) > MAX_EVAL_EXPRESSION_CHARS:
        raise ActionValidationError(
            f"expression too long (max {MAX_EVAL_EXPRESSION_CHARS})"
        )
    # Scrub for activity-feed summary only — expression itself is pool-side.
    _ = scrub_text(expression[:200])
    return {"expression": expression}




# --- Stagehand-style act → fallback (pattern only; peers-deep) -------------

_KIND_CLICK = "click"
_KIND_NAV = "navigate"
_SOFT = frozenset({_KIND_CLICK})
STATUS_ACT_OK = "ok"
STATUS_ACT_FAILED = "failed"
STATUS_NEED_FALLBACK = "need_fallback"


class ActFallbackValidationError(ValueError):
    """Bad /act body."""


_MSG_JSON_OBJ = "{label} must be a JSON object"
_LABEL_PRIMARY = "primary"


def max_fallback_steps() -> int:
    raw = os.environ.get("SLIPSTREAM_ACT_FALLBACK_MAX_STEPS", "").strip()
    try:
        n = int(raw) if raw else 5
    except ValueError:
        n = 5
    return max(1, min(n, 20))


def soft_retry_budget() -> int:
    raw = os.environ.get("SLIPSTREAM_ACT_SOFT_RETRY", "").strip()
    if not raw:
        return 1
    try:
        return 1 if int(raw) > 0 else 0
    except ValueError:
        return 1


def _require_obj(raw: Any, label: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ActFallbackValidationError(_MSG_JSON_OBJ.format(label=label))
    try:
        reject_secret_fields(raw)
    except AlertValidationError as e:
        raise ActFallbackValidationError(str(e)) from e
    return raw


def parse_act_step(raw: Any, *, label: str = "step") -> dict[str, Any]:
    raw = _require_obj(raw, label)
    kind = raw.get("kind")
    if kind == _KIND_CLICK:
        sel = raw.get("selector")
        if not isinstance(sel, str) or not scrub_text(sel.strip()):
            raise ActFallbackValidationError("click.selector required")
        return {"kind": _KIND_CLICK, "selector": scrub_text(sel.strip())[:300]}
    if kind == _KIND_NAV:
        url = raw.get("url")
        if not isinstance(url, str) or not scrub_text(url.strip()):
            raise ActFallbackValidationError("navigate.url required")
        return {"kind": _KIND_NAV, "url": scrub_text(url.strip())[:2000]}
    raise ActFallbackValidationError(f"{label}.kind must be click|navigate")


def _plan_cap(body: dict[str, Any]) -> int:
    cap = max_fallback_steps()
    ms = body.get("max_steps")
    if isinstance(ms, int) and ms >= 1:
        return min(ms, 20)
    return cap


def _soft_n(kind: str, soft: Any) -> int:
    if kind not in _SOFT:
        return 0
    if soft is None:
        return soft_retry_budget()
    if isinstance(soft, bool):
        return int(soft)
    if isinstance(soft, int) and soft in (0, 1):
        return soft
    raise ActFallbackValidationError("soft_retry must be bool or 0|1")


def parse_act_request(body: Any) -> dict[str, Any]:
    body = _require_obj(body, "body")
    primary = parse_act_step(body.get(_LABEL_PRIMARY, body), label=_LABEL_PRIMARY)
    plan_raw = body.get("fallback_plan") or body.get("plan") or []
    if not isinstance(plan_raw, list):
        raise ActFallbackValidationError("fallback_plan must be a list")
    cap = _plan_cap(body)
    if len(plan_raw) > cap:
        raise ActFallbackValidationError(f"fallback_plan longer than max_steps ({cap})")
    return {
        "primary": primary,
        "fallback_plan": [parse_act_step(s, label=f"plan[{i}]") for i, s in enumerate(plan_raw)],
        "max_steps": cap,
        "soft_retry": _soft_n(primary["kind"], body.get("soft_retry")),
        "confirm_retry": bool(body.get("confirm_retry", True)),
    }


def act_step_summary(step: dict[str, Any]) -> str:
    if step["kind"] == _KIND_NAV:
        return f"act nav {step.get('url', '')[:80]}"
    return f"act clk {step.get('selector', '')[:80]}"


def act_feed_kind(kind: str) -> str:
    return _KIND_NAV if kind == _KIND_NAV else _KIND_CLICK


def finalize_act_loop(
    *,
    primary_ok: bool,
    attempts: int,
    steps_run: list[dict[str, Any]],
    confirm_retry: bool,
    used_fallback: bool,
    last_reason: str,
) -> dict[str, Any]:
    if primary_ok and not used_fallback:
        status, reason = STATUS_ACT_OK, ("primary" if attempts == 1 else "soft_retry")
    elif primary_ok:
        status, reason = STATUS_ACT_OK, "fallback_plan"
    elif used_fallback:
        status, reason = STATUS_ACT_FAILED, last_reason
    else:
        status, reason = STATUS_NEED_FALLBACK, last_reason
    return {
        "status": status,
        "reason": reason,
        "attempts": attempts,
        "steps_run": steps_run,
        "confirm_retry": confirm_retry,
    }
