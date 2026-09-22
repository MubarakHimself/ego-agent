"""Thin Chromium DevTools HTTP helpers (stdlib only).

Used by live smoke + bench. Not a full CDP client — production driver TBD.
Navigate via PUT /json/new?<url>; inspect via /json/list and /json/version.
"""

from __future__ import annotations

import json
import time
import urllib.request
from typing import Any
from urllib.parse import quote


def wait_cdp_ready(cdp_http_url: str, *, timeout: float = 15.0) -> dict[str, Any]:
    """Poll GET {cdp}/json/version until Chrome answers or timeout."""
    base = cdp_http_url.rstrip("/")
    url = f"{base}/json/version"
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:
                if resp.status == 200:
                    return json.load(resp)
        except Exception as e:  # noqa: BLE001 — probe loop
            last_err = e
            time.sleep(0.1)
    raise TimeoutError(f"CDP not ready at {url} within {timeout}s (last={last_err})")


def list_targets(cdp_http_url: str) -> list[dict[str, Any]]:
    base = cdp_http_url.rstrip("/")
    with urllib.request.urlopen(f"{base}/json/list", timeout=3.0) as resp:
        return json.load(resp)


def navigate_via_json_new(
    cdp_http_url: str,
    page_url: str,
    *,
    expect_title_substr: str | None = None,
    settle_timeout: float = 10.0,
) -> dict[str, Any]:
    """Open ``page_url`` with PUT /json/new and wait for title/url to settle.

    Returns the matching target dict plus ``matched`` (bool).
    """
    base = cdp_http_url.rstrip("/")
    # Chrome requires PUT for /json/new (GET → 405 on modern builds).
    req = urllib.request.Request(
        f"{base}/json/new?{quote(page_url, safe=':/?#&=%')}",
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
            # When no title expectation: require URL containment (do not
            # settle on any non-empty title alone — about:blank etc.).
            needle = page_url.rstrip("/")
            url_ok = needle in (url or "") or page_url in (url or "")
            if expect_title_substr is not None:
                if title_ok:
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
