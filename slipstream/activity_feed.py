"""Lease-scoped append-only Watch activity feed (thin dock).

CTO cut slipstream-activity-feed-007: chronological actions beside the Watch
JPEG — navigate / click / type / fill / alert / confirm. Bounded ring buffer;
secrets redacted; same watch_url TTL/revoke (410). No replay, no second
notification path (alerts reuse need_human / task_done).
"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

# Ship-now action kinds (CTO cut). Pair-browse scroll→click; key→type.
FEED_KINDS = frozenset({"navigate", "click", "type", "fill", "alert", "confirm"})

DEFAULT_CAPACITY = 100
DEFAULT_LIST_LIMIT = 80

# Keys that must never appear in feed detail (mirrors watch/alerts denylist).
_SECRET_KEYS = frozenset(
    {
        "cookie",
        "cookies",
        "password",
        "passwd",
        "token",
        "watch_token",
        "private_key",
        "jwt",
        "bearer",
        "access_key",
        "credential",
        "credentials",
        "authorization",
        "secret",
        "secrets",
        "storage_state",
        "cdp_http",
        "cdp_http_url",
        "websocketdebuggerurl",
        "vault",
        "username",
        "user",
        "email",
        "text",  # typed / filled plaintext — use text_len only
        "value",
        "values",
    }
)

_SECRET_KEY_RE = re.compile(
    r"(password|passwd|secret|token|cookie|authorization|bearer|jwt|vault|cdp)",
    re.IGNORECASE,
)


@dataclass
class ActivityEvent:
    """One scrubbed feed row (JSON-serializable via to_public)."""

    seq: int
    ts: float
    kind: str
    summary: str
    outcome: str = "ok"
    detail: dict[str, Any] = field(default_factory=dict)

    def to_public(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "kind": self.kind,
            "summary": self.summary,
            "outcome": self.outcome,
            "detail": dict(self.detail),
        }


def safe_url_summary(url: str | None) -> str:
    """Host + path only — strip query/fragment (may carry tokens)."""
    if not url or not isinstance(url, str):
        return "(no url)"
    try:
        p = urlparse(url.strip())
    except Exception:
        return "(bad url)"
    host = (p.hostname or "").lower().rstrip(".")
    if not host:
        return "(no host)"
    path = p.path or "/"
    if len(path) > 80:
        path = path[:77] + "..."
    scheme = (p.scheme or "https").lower()
    if scheme not in ("http", "https"):
        return f"(refused scheme {scheme})"
    return f"{scheme}://{host}{path}"


def scrub_feed_detail(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Drop secret-like keys; never keep typed text / vault / CDP auth.

    ADV-FEED-003: align key denylist with alerts.key_looks_secret and scrub
    list-of-string values (not only bare strings).
    """
    if not raw:
        return {}
    from slipstream.alerts import key_looks_secret, scrub_text

    out: dict[str, Any] = {}
    for k, v in raw.items():
        if not isinstance(k, str):
            continue
        kl = k.strip().lower().replace("-", "_")
        # Feed-specific drops (typed plaintext) + shared alerts denylist.
        if (
            kl in _SECRET_KEYS
            or _SECRET_KEY_RE.search(kl)
            or key_looks_secret(k)
        ):
            continue
        if isinstance(v, str):
            out[k] = scrub_text(v)[:200]
        elif isinstance(v, (int, float, bool)):
            out[k] = v
        elif isinstance(v, list) and all(isinstance(x, str) for x in v):
            # fill labels / string lists — scrub each value (ADV-FEED-003)
            out[k] = [scrub_text(str(x))[:64] for x in v[:20]]
        elif v is None:
            continue
        else:
            # refuse nested objects (could hide secrets)
            continue
    return out


def scrub_summary(text: str) -> str:
    from slipstream.alerts import scrub_text

    return scrub_text((text or "")[:160])


class LeaseActivityFeed:
    """Append-only ring buffer of scrubbed events for one lease."""

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        self._capacity = max(1, int(capacity))
        self._seq = 0
        self._buf: deque[ActivityEvent] = deque(maxlen=self._capacity)

    def append(
        self,
        kind: str,
        summary: str,
        *,
        outcome: str = "ok",
        detail: dict[str, Any] | None = None,
        ts: float | None = None,
    ) -> ActivityEvent:
        if kind not in FEED_KINDS:
            raise ValueError(f"unknown feed kind: {kind!r}")
        self._seq += 1
        ev = ActivityEvent(
            seq=self._seq,
            ts=time.time() if ts is None else float(ts),
            kind=kind,
            summary=scrub_summary(summary),
            outcome=scrub_summary(outcome or "ok")[:32],
            detail=scrub_feed_detail(detail),
        )
        self._buf.append(ev)
        return ev

    def list_events(
        self, *, after_seq: int = 0, limit: int = DEFAULT_LIST_LIMIT
    ) -> list[dict[str, Any]]:
        """Chronological (oldest→newest) events with seq > after_seq."""
        lim = max(1, min(int(limit), self._capacity))
        after = max(0, int(after_seq))
        rows = [e.to_public() for e in self._buf if e.seq > after]
        if len(rows) > lim:
            rows = rows[-lim:]
        return rows

    def clear(self) -> None:
        self._buf.clear()
        # keep seq monotonic so clients do not replay old ids after revoke remint



# Public aliases
