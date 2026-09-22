"""Space/lease tiers: ephemeral | named | attach (ui-peers three-tier).

Thin attach-my-Chrome: bind a lease to an existing CDP endpoint without
spawning pool Chromium. Attach is default-off (``SLIPSTREAM_ALLOW_ATTACH=1``).
"""

from __future__ import annotations

import os
import urllib.parse
from typing import Any

TIER_EPHEMERAL = "ephemeral"
TIER_NAMED = "named"
TIER_ATTACH = "attach"
SPACE_TIERS = frozenset({TIER_EPHEMERAL, TIER_NAMED, TIER_ATTACH})

# Shown on status/list/sessions for attach leases (shared browser + honor-system).
ATTACH_RISK_LABEL = (
    "shared-browser; honor-system raw CDP / ladder bypass when driving attached "
    "Chrome directly (or with SLIPSTREAM_EXPOSE_RAW_CDP); release detaches — "
    "does not quit user Chrome"
)

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})
_ALLOWED_SCHEMES = frozenset({"http", "https", "ws", "wss"})
_REFUSED_SCHEMES = frozenset({"file", "javascript", "data", "blob", "about", "ftp"})


class TierError(ValueError):
    """Invalid tier / attach endpoint (maps to 400)."""


class AttachDisabledError(RuntimeError):
    """Attach requested but SLIPSTREAM_ALLOW_ATTACH is not enabled (maps to 403)."""


def attach_allowed() -> bool:
    """True when attach tier is enabled (safer default: off)."""
    return os.environ.get("SLIPSTREAM_ALLOW_ATTACH", "").strip() == "1"


def parse_tier(raw: Any, *, default: str = TIER_EPHEMERAL) -> str:
    """Parse tier string; ``None``/omit → default. Reject unknown."""
    if raw is None:
        return default
    if not isinstance(raw, str):
        raise TierError("tier must be a string")
    t = raw.strip().lower()
    if not t:
        return default
    if t not in SPACE_TIERS:
        raise TierError(
            f"tier must be one of {sorted(SPACE_TIERS)} (got {raw!r})"
        )
    return t


def _attach_allow_hosts() -> set[str]:
    """Loopback + optional SLIPSTREAM_ATTACH_ALLOW_HOSTS (comma/space list)."""
    hosts = set(_LOOPBACK_HOSTS)
    extra = os.environ.get("SLIPSTREAM_ATTACH_ALLOW_HOSTS", "")
    for part in extra.replace(",", " ").split():
        h = part.strip().lower().strip("[]")
        if h:
            hosts.add(h)
    return hosts


def cdp_http_from_ws(url: str) -> str:
    """Map ws(s) CDP URL to http(s) base for /json/version probes."""
    p = urllib.parse.urlparse(url)
    scheme = "https" if p.scheme == "wss" else "http"
    # Keep netloc only — drop /devtools/... path for version probe base.
    return urllib.parse.urlunparse((scheme, p.netloc, "", "", "", ""))


def resolve_attach_cdp(
    *,
    cdp_url: str | None = None,
    cdp_port: int | None = None,
) -> tuple[str, int | None]:
    """Validate and normalize attach CDP to an http(s) base URL + optional port.

    Accepts ``cdp_url`` (http/https/ws/wss) or ``cdp_port`` (→ http://127.0.0.1:PORT).
    Refuses file: / javascript: / data: and non-allowlisted hosts.
    """
    if cdp_url is not None and cdp_port is not None:
        raise TierError("provide cdp_url or cdp_port, not both")
    if cdp_url is None and cdp_port is None:
        raise TierError("attach tier requires cdp_url or cdp_port")

    if cdp_port is not None:
        if isinstance(cdp_port, bool) or not isinstance(cdp_port, int):
            raise TierError("cdp_port must be an integer")
        if cdp_port < 1 or cdp_port > 65535:
            raise TierError("cdp_port out of range")
        return f"http://127.0.0.1:{cdp_port}", cdp_port

    if not isinstance(cdp_url, str) or not cdp_url.strip():
        raise TierError("cdp_url must be a non-empty string")
    raw = cdp_url.strip()
    parsed = urllib.parse.urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    if scheme in _REFUSED_SCHEMES or scheme not in _ALLOWED_SCHEMES:
        raise TierError(
            f"cdp_url scheme not allowed (got {scheme!r}; use http/https/ws/wss)"
        )
    host = (parsed.hostname or "").lower().strip("[]")
    if not host:
        raise TierError("cdp_url must include a host")
    if host not in _attach_allow_hosts():
        raise TierError(
            f"cdp_url host {host!r} not allowlisted (loopback or "
            "SLIPSTREAM_ATTACH_ALLOW_HOSTS)"
        )
    if scheme in ("ws", "wss"):
        http_url = cdp_http_from_ws(raw)
    else:
        # Drop path/query/fragment — CDP HTTP base is origin only.
        http_url = urllib.parse.urlunparse(
            (scheme, parsed.netloc, "", "", "", "")
        )
    port = parsed.port
    if port is None:
        port = 443 if scheme in ("https", "wss") else 80
    return http_url.rstrip("/"), port


def risk_label_for_tier(tier: str) -> str | None:
    if tier == TIER_ATTACH:
        return ATTACH_RISK_LABEL
    return None


def tier_blurb_table() -> str:
    """Markdown table for SKILL / POOL_API."""
    return (
        "| Tier | Meaning |\n"
        "| --- | --- |\n"
        "| `ephemeral` | **Default.** Pool-spawned Chromium; Space dir is a disposable "
        "profile label (no attach). |\n"
        "| `named` | Durable/named Space profile id (`user-data-dir` under "
        "`spaces_root`) — login-once / warm reuse. |\n"
        "| `attach` | Attach to **existing** Chrome via user CDP URL/port — "
        "**no** pool Chromium spawn. Requires `SLIPSTREAM_ALLOW_ATTACH=1`. |\n"
    )
