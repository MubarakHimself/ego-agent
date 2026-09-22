"""Thin Chromium DevTools HTTP helpers (stdlib only).

Used by live smoke + bench. Not a full CDP client — production driver TBD.
Navigate via PUT /json/new?<url>; inspect via /json/list and /json/version.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import quote, urlparse

from slipstream.cli import _validate_api_request_url


def _url_settled(page_url: str, observed_url: str) -> bool:
    """True when observed URL matches page_url by scheme+netloc and path prefix."""
    want = urlparse(page_url)
    got = urlparse(observed_url or "")
    if not want.scheme or not want.netloc:
        return False
    if got.scheme != want.scheme or got.netloc != want.netloc:
        return False
    want_path = want.path or "/"
    got_path = got.path or "/"
    if got_path == want_path:
        return True
    prefix = want_path if want_path.endswith("/") else want_path + "/"
    return got_path.startswith(prefix)


def wait_cdp_ready(cdp_http_url: str, *, timeout: float = 15.0) -> dict[str, Any]:
    """Poll GET {cdp}/json/version until Chrome answers or timeout."""
    base = cdp_http_url.rstrip("/")
    url = f"{base}/json/version"
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                _validate_api_request_url(url), timeout=1.0
            ) as resp:
                if resp.status == 200:
                    return json.load(resp)
        except Exception as e:  # noqa: BLE001 — probe loop
            last_err = e
            time.sleep(0.1)
    raise TimeoutError(f"CDP not ready at {url} within {timeout}s (last={last_err})")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects on attach health probes (SSRF: loopback → metadata)."""

    def redirect_request(self, *args):  # urllib signature; never follow
        req = args[0] if args else None
        code = args[2] if len(args) > 2 else 302
        headers = args[4] if len(args) > 4 else None
        newurl = args[5] if len(args) > 5 else "?"
        full = getattr(req, "full_url", "")
        raise urllib.error.HTTPError(
            full,
            code,
            f"attach CDP probe refused redirect to {newurl}",
            headers,
            None,
        )


def _attach_probe_url(cdp_http_url: str) -> tuple[str, str]:
    """Return (/json/version URL, host) after attach peer policy check."""
    from slipstream.tiers import TierError, assert_attach_peer_allowed

    base = cdp_http_url.rstrip("/")
    url = f"{base}/json/version"
    parsed = urlparse(url)
    if (parsed.scheme or "").lower() not in ("http", "https"):
        raise TierError(
            f"attach probe scheme not allowed (got {parsed.scheme!r})"
        )
    host = (parsed.hostname or "").lower().strip("[]")
    assert_attach_peer_allowed(host)
    return url, host


def wait_attach_cdp_ready(cdp_http_url: str, *, timeout: float = 5.0) -> dict[str, Any]:
    """Attach-tier CDP probe: no redirects; re-validate host/IP each attempt.

    Does **not** honor ``SLIPSTREAM_ALLOW_REMOTE_URL`` — attach policy is
    ``tiers.assert_attach_peer_allowed`` (loopback / ATTACH_ALLOW_HOSTS).
    """
    from slipstream.tiers import TierError, assert_attach_peer_allowed

    url, host = _attach_probe_url(cdp_http_url)
    opener = urllib.request.build_opener(_NoRedirectHandler)
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            assert_attach_peer_allowed(host)
            with opener.open(url, timeout=1.0) as resp:
                final_host = (urlparse(resp.geturl()).hostname or "").lower().strip("[]")
                if final_host:
                    assert_attach_peer_allowed(final_host)
                if resp.status == 200:
                    return json.load(resp)
        except TierError:
            raise
        except Exception as e:  # noqa: BLE001 — probe loop
            last_err = e
            time.sleep(0.1)
    raise TimeoutError(f"CDP not ready at {url} within {timeout}s (last={last_err})")


def list_targets(cdp_http_url: str) -> list[dict[str, Any]]:
    base = cdp_http_url.rstrip("/")
    with urllib.request.urlopen(
        _validate_api_request_url(f"{base}/json/list"), timeout=3.0
    ) as resp:
        return json.load(resp)


def navigate_via_json_new(
    cdp_http_url: str,
    page_url: str,
    *,
    expect_title_substr: str | None = None,
    settle_timeout: float = 10.0,
    allowed_domains: list[str] | None = None,
) -> dict[str, Any]:
    """Open ``page_url`` with PUT /json/new and wait for title/url to settle.

    Returns the matching target dict plus ``matched`` (bool).

    When ``allowed_domains`` is None, uses ``SLIPSTREAM_ALLOWED_DOMAINS`` (empty
    = unrestricted). Raises DomainAllowlistError if the host is outside the list.
    """
    from slipstream.domains import allowed_domains_from_env, check_navigate_url

    patterns = (
        list(allowed_domains)
        if allowed_domains is not None
        else allowed_domains_from_env()
    )
    check_navigate_url(page_url, patterns)
    base = cdp_http_url.rstrip("/")
    # Chrome requires PUT for /json/new (GET → 405 on modern builds).
    req = urllib.request.Request(
        _validate_api_request_url(
            f"{base}/json/new?{quote(page_url, safe=':/?#&=%')}"
        ),
        method="PUT",
    )
    with urllib.request.urlopen(req, timeout=10.0) as resp:
        created = json.load(resp)
    target_id = created.get("id")

    deadline = time.monotonic() + settle_timeout
    last: dict[str, Any] = dict(created)
    while time.monotonic() < deadline:
        try:
            targets = list_targets(cdp_http_url)
        except Exception:
            time.sleep(0.15)
            continue
        for t in targets:
            if target_id and t.get("id") != target_id:
                continue
            if t.get("type") not in (None, "page"):
                continue
            title = (t.get("title") or "").strip()
            url = t.get("url") or ""
            last = t
            title_ok = (
                expect_title_substr is not None
                and expect_title_substr.lower() in title.lower()
            )
            # Settle by scheme+netloc equality and path prefix (not raw substring).
            url_ok = _url_settled(page_url, url)
            if expect_title_substr is not None:
                # Title expectation requires both title and URL to match.
                if title_ok and url_ok:
                    out = dict(t)
                    out["matched"] = True
                    return out
            elif url_ok:
                out = dict(t)
                out["matched"] = True
                return out
        time.sleep(0.15)

    out = dict(last)
    out["matched"] = False
    return out
