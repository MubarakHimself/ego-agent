"""Top-frame navigation domain allowlist (Browserbase / agent-browser pattern).

When an allowlist is set, refuse navigate (and nav_irreversible with a URL)
outside the list. Empty allowlist = unrestricted.

v1 limitation (like Browserbase experimental): top-frame navigation only —
iframe / subframe loads and subresource requests are NOT blocked.
"""

from __future__ import annotations

import os
import re
from typing import Sequence
from urllib.parse import urlparse

# Host patterns: letters/digits/dots/hyphens/underscores + optional leading *.
_PATTERN_RE = re.compile(r"^\*?\.?[A-Za-z0-9._-]+$")


class DomainAllowlistError(ValueError):
    """URL host is outside the configured allowlist."""

    def __init__(self, message: str, *, host: str | None = None, url: str | None = None):
        super().__init__(message)
        self.host = host
        self.url = url


def parse_allowed_domains(raw: str | Sequence[str] | None) -> list[str]:
    """Parse comma/space-separated env string or list into normalized patterns.

    Empty / None → ``[]`` (unrestricted when used as effective allowlist).
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = re.split(r"[\s,]+", raw.strip())
    else:
        parts = list(raw)
    out: list[str] = []
    seen: set[str] = set()
    for p in parts:
        if not isinstance(p, str):
            raise DomainAllowlistError(
                f"allowed_domains entry must be a string, got {type(p).__name__}"
            )
        pat = p.strip().lower().rstrip(".")
        if not pat:
            continue
        if not _PATTERN_RE.match(pat):
            raise DomainAllowlistError(f"invalid allowed domain pattern: {p!r}")
        if pat not in seen:
            seen.add(pat)
            out.append(pat)
    return out


def allowed_domains_from_env() -> list[str]:
    """Read ``SLIPSTREAM_ALLOWED_DOMAINS`` (comma/space-separated)."""
    return parse_allowed_domains(os.environ.get("SLIPSTREAM_ALLOWED_DOMAINS"))


def host_matches(host: str, pattern: str) -> bool:
    """True if host matches pattern.

    - ``*.example.com`` — bare ``example.com`` + any subdomain
    - ``example.com`` — exact + subdomains (Browserbase-style)
    """
    host = (host or "").lower().rstrip(".")
    pattern = (pattern or "").lower().strip().rstrip(".")
    if not host or not pattern:
        return False
    if pattern.startswith("*."):
        base = pattern[2:]
        if not base:
            return False
        return host == base or host.endswith("." + base)
    return host == pattern or host.endswith("." + pattern)


def url_host(url: str) -> str | None:
    """Return lowercase hostname for http(s) URLs; None if missing."""
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    host = parsed.hostname
    if not host:
        return None
    return host.lower().rstrip(".")


def url_allowed(url: str, patterns: Sequence[str] | None) -> bool:
    """Return True if navigate to ``url`` is permitted.

    Empty / None patterns → unrestricted. Non-http(s) schemes (``about:``,
    ``chrome:``, ``data:``, …) are always allowed — allowlist applies to
    top-frame http(s) only (Browserbase limitation mirror).
    """
    if not patterns:
        return True
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return True
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return False
    return any(host_matches(host, p) for p in patterns)


def check_navigate_url(url: str, patterns: Sequence[str] | None) -> None:
    """Raise DomainAllowlistError if ``url`` is outside ``patterns``."""
    if not isinstance(url, str) or not url.strip():
        raise DomainAllowlistError("navigate url must be a non-empty string", url=url)
    url = url.strip()
    if url_allowed(url, patterns):
        return
    host = url_host(url)
    raise DomainAllowlistError(
        f"navigate refused: host {host!r} is outside allowed_domains",
        host=host,
        url=url,
    )


def effective_allowed_domains(
    *,
    lease_domains: list[str] | None = None,
    space_domains: list[str] | None = None,
    config_domains: list[str] | None = None,
) -> list[str]:
    """Resolve effective allowlist: lease override → Space → pool/env config.

    ``None`` at a level means "not set" (fall through). An explicit empty list
    at the chosen level means unrestricted.
    """
    if lease_domains is not None:
        return list(lease_domains)
    if space_domains is not None:
        return list(space_domains)
    return list(config_domains or [])
