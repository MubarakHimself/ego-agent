"""Top-frame navigation domain allowlist (Browserbase / agent-browser pattern).

When an allowlist is set, refuse navigate (and nav_irreversible with a URL)
outside the list. Empty effective allowlist (no Space/config/lease lockdown)
= unrestricted.

ADV-DOM-001: lease ``allowed_domains=[]`` inherits Space∩config (never clears
lockdown); lease may only narrow, never widen past the parent intersection.
ADV-DOM-002: reject ``\\`` / ``%5C`` in authority and refuse userinfo (parser
differential vs Chromium).
ADV-DOM-003: fail-closed — only ``http``/``https`` with a host when allowlist
is active; ``file:``/``javascript:``/``data:``/scheme-relative ``//…`` refused.

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

# Internal sentinel: non-empty allowlist that matches no real host (deny-all).
_DENY_ALL = ["__deny.all__"]

# Backslash / percent-encoded backslash in authority (Chromium vs urlparse).
_BACKSLASH_IN_URL_RE = re.compile(r"\\|%5c", re.IGNORECASE)


class DomainAllowlistError(ValueError):
    """URL host is outside the configured allowlist / URL refused."""

    def __init__(self, message: str, *, host: str | None = None, url: str | None = None):
        super().__init__(message)
        self.host = host
        self.url = url


def parse_allowed_domains(raw: str | Sequence[str] | None) -> list[str]:
    """Parse comma/space-separated env string or list into normalized patterns.

    Empty / None → ``[]`` (unrestricted when no parent lockdown applies).
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


def _pattern_within_parent(child: str, parent_patterns: Sequence[str]) -> bool:
    """True if ``child`` pattern is equal to or narrower than some parent pattern."""
    child = (child or "").lower().strip().rstrip(".")
    if not child or not parent_patterns:
        return False
    for p in parent_patterns:
        p = (p or "").lower().strip().rstrip(".")
        if not p:
            continue
        if child == p:
            return True
        # Child host/base must match parent (subdomain of parent base).
        probe = child[2:] if child.startswith("*.") else child
        if host_matches(probe, p):
            return True
    return False


def _intersect_pattern_lists(a: Sequence[str], b: Sequence[str]) -> list[str]:
    """Keep patterns from ``a`` that lie within ``b`` (order of ``a``)."""
    return [p for p in a if _pattern_within_parent(p, b)]


def _resolve_parent(
    space_domains: list[str] | None,
    config_domains: list[str] | None,
) -> list[str]:
    """Space∩config. Empty Space list inherits config (does not clear lockdown)."""
    cfg = list(config_domains or [])
    if space_domains is None or len(space_domains) == 0:
        return cfg
    space = list(space_domains)
    if not cfg:
        return space
    inter = _intersect_pattern_lists(space, cfg)
    if not inter:
        return list(_DENY_ALL)
    return inter


def url_host(url: str) -> str | None:
    """Return lowercase hostname for http(s) URLs; None if missing/unsafe."""
    try:
        host = _navigate_host(url)
    except DomainAllowlistError:
        return None
    return host


def _authority_hazard(url: str) -> str | None:
    """Return hazard reason if URL has backslash / userinfo differential risk."""
    if _BACKSLASH_IN_URL_RE.search(url):
        return "backslash or %5C in URL (parser differential)"
    try:
        parsed = urlparse(url)
    except Exception:
        return "unparseable URL"
    # Refuse credentials in navigate URLs (ADV-DOM-002).
    if parsed.username is not None or parsed.password is not None:
        return "userinfo (credentials) not allowed in navigate URL"
    # Netloc with @ after decoding hazards already caught; bare @ without
    # userinfo means urlparse already split — username set. Extra: reject
    # literal backslash lingering in netloc.
    netloc = parsed.netloc or ""
    if "\\" in netloc or "%5c" in netloc.lower():
        return "backslash in netloc"
    return None


def _navigate_host(url: str) -> str:
    """Canonical host for allowlist check; raises DomainAllowlistError if unsafe."""
    hazard = _authority_hazard(url)
    if hazard:
        raise DomainAllowlistError(f"navigate refused: {hazard}", url=url)
    try:
        parsed = urlparse(url)
    except Exception as e:
        raise DomainAllowlistError(f"navigate refused: unparseable URL ({e})", url=url) from e
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise DomainAllowlistError(
            f"navigate refused: scheme {scheme!r} not allowed when allowlist active "
            "(only http/https with host)",
            url=url,
        )
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise DomainAllowlistError(
            "navigate refused: http(s) URL missing host",
            url=url,
        )
    return host


def url_allowed(url: str, patterns: Sequence[str] | None) -> bool:
    """Return True if navigate to ``url`` is permitted.

    Empty / None patterns → unrestricted (no lockdown configured).

    When patterns are non-empty (allowlist active): only ``http``/``https`` with
    a host, no userinfo, no ``\\``/``%5C``; host must match a pattern.
    ``file:`` / ``javascript:`` / ``data:`` / scheme-relative ``//…`` → False.
    """
    if not patterns:
        return True
    if not isinstance(url, str) or not url.strip():
        return False
    url = url.strip()
    try:
        host = _navigate_host(url)
    except DomainAllowlistError:
        return False
    return any(host_matches(host, p) for p in patterns)


def check_navigate_url(url: str, patterns: Sequence[str] | None) -> None:
    """Raise DomainAllowlistError if ``url`` is outside ``patterns``."""
    if not isinstance(url, str) or not url.strip():
        raise DomainAllowlistError("navigate url must be a non-empty string", url=url)
    url = url.strip()
    if not patterns:
        # Unrestricted — still refuse backslash/userinfo hazards so CDP never
        # sees differential URLs even without an allowlist.
        hazard = _authority_hazard(url)
        if hazard:
            raise DomainAllowlistError(f"navigate refused: {hazard}", url=url)
        return
    if url_allowed(url, patterns):
        return
    host: str | None
    try:
        host = _navigate_host(url)
    except DomainAllowlistError as e:
        raise e
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
    """Resolve effective allowlist: Space∩config, lease may only narrow.

    ADV-DOM-001:
    - ``None`` or ``[]`` lease → inherit parent (empty lease does **not** clear
      Space/config lockdown).
    - Non-empty lease under a parent lockdown must be within parent; otherwise
      ``DomainAllowlistError`` (never widen).
    - Empty parent (no Space/config patterns) → unrestricted ``[]``, or lease
      patterns alone when lease sets them.
    """
    parent = _resolve_parent(space_domains, config_domains)
    if lease_domains is None or len(lease_domains) == 0:
        return list(parent)
    lease = list(lease_domains)
    if not parent:
        return lease
    if parent == _DENY_ALL:
        raise DomainAllowlistError(
            "lease allowed_domains refused: Space∩config allowlist is empty "
            "(no overlapping patterns)"
        )
    narrowed = _intersect_pattern_lists(lease, parent)
    # Any lease pattern outside parent → widen attempt → refuse.
    if len(narrowed) != len(lease) or any(
        not _pattern_within_parent(p, parent) for p in lease
    ):
        raise DomainAllowlistError(
            "lease allowed_domains cannot widen past Space∩config intersection"
        )
    return narrowed
