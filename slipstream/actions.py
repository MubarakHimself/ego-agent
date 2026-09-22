"""Thin confirm-actions / permission ladder (v1).

Gated categories: eval | download | upload | nav_irreversible.
Soft browse (snapshot/click/scroll/wait) is free — not gated here.

Gated act → confirmation_required + sibling need_human on the same alerts bus
(kind=confirmation_required + confirm_id). Confirm|deny within ~60s; auto-deny
on expiry. Non-TTY interactive confirm → deny.

Defer: once/always/never policy matrix, Comet UI. Domain allowlist +
content-boundaries ship separately (pool navigate gate / boundaries helper).
Pattern from agent-browser Security docs (no code theft).

v1 honor-system (ADV-PL-001): POST /actions is an advisory pause — CDP fill /
eval / nav are not server-enforced by this ladder yet. Server-enforced ladder
is a later track. Domain allowlist on navigate IS enforced at the pool gate.
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

CATEGORIES = frozenset({"eval", "download", "upload", "nav_irreversible"})
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
    """In-memory pending confirmations (process-lifetime; cleared on shutdown)."""

    _by_id: dict[str, PendingConfirmation] = field(default_factory=dict)
    _by_lease: dict[str, list[str]] = field(default_factory=dict)

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

    def expire_due(self, now: float | None = None) -> list[PendingConfirmation]:
        """Mark overdue pending as expired; return those newly expired."""
        now = time.time() if now is None else now
        newly: list[PendingConfirmation] = []
        for p in list(self._by_id.values()):
            if p.status == STATUS_PENDING and now >= p.expires_at:
                p.status = STATUS_EXPIRED
                newly.append(p)
        return newly
