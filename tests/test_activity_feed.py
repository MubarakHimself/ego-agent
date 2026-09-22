"""Watch activity feed — scrubbed lease-scoped event dock (CTO-007)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlparse

import pytest

from slipstream.activity_feed import (
    LeaseActivityFeed,
    safe_url_summary,
    scrub_feed_detail,
)
from slipstream.api import PoolServer
from slipstream.boundaries import (
    reset_boundary_nonce_for_tests,
    sanitize_origin,
    wrap_page_content,
)
from slipstream.cli import _validate_api_request_url
from slipstream.config import PoolConfig
from slipstream.domains import host_matches
from slipstream.pool import BrowserPool


@pytest.fixture
def api_server(tmp_path):
    cfg = PoolConfig(
        K=5,
        W=1,
        spaces_root=tmp_path / "spaces",
        cdp_base_port=19750,
        mock=True,
        host="127.0.0.1",
        port=18790,
    )
    pool = BrowserPool(cfg)
    server = PoolServer(pool, port=18790)
    server.start(background=True)
    yield server
    server.stop()


def _req(method: str, url: str, body: dict | None = None):
    if not url.startswith(("http://127.0.0.1:", "http://localhost:")):
        raise ValueError(f"test helper refuses non-loopback URL: {url!r}")
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"} if data is not None else {}
    request = urllib.request.Request(
        _validate_api_request_url(url),
        data=data,
        method=method,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as resp:  # skylos: ignore[SKY-D216]
            raw_body = resp.read()
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "json" in ctype:
                return resp.status, json.loads(raw_body.decode("utf-8"))
            return resp.status, raw_body
    except urllib.error.HTTPError as e:
        raw_body = e.read()
        try:
            return e.code, json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError:
            return e.code, raw_body


def _lease(base: str, space: str = "af-space") -> str:
    code, lease = _req(
        "POST",
        f"{base}/v1/leases",
        {"agent_id": "agent-af", "space_id": space},
    )
    assert code == 200, lease
    return lease["lease_id"]


def _token_from_watch_url(watch_url: str) -> str:
    qs = parse_qs(urlparse(watch_url).query)
    assert "token" in qs and qs["token"][0]
    return qs["token"][0]


def test_safe_url_summary_strips_query():
    s = safe_url_summary("https://ex.test/a?token=secret")
    assert "token=" not in s
    assert s.startswith("https://ex.test/a")


def test_scrub_feed_detail_drops_secrets():
    out = scrub_feed_detail(
        {"password": "x", "text": "typed", "text_len": 5, "fields": ["username"]}
    )
    assert "password" not in out and "text" not in out
    assert out.get("text_len") == 5
    assert out.get("fields") == ["username"]


def test_feed_ring_capacity():
    feed = LeaseActivityFeed(capacity=3)
    for i in range(5):
        feed.append("navigate", f"https://ex.test/{i}")
    rows = feed.list_events(after_seq=0)
    assert len(rows) == 3
    assert rows[0]["summary"].endswith("/2")


def test_adv_bound_002_space_in_origin_refused(monkeypatch):
    reset_boundary_nonce_for_tests()
    monkeypatch.setenv("SLIPSTREAM_CONTENT_BOUNDARIES", "1")
    evil = "http://evil.com origin=http://good.com"
    assert sanitize_origin(evil) is None
    wrapped = wrap_page_content("PAGE", origin=evil)
    begin = wrapped.split("\n", 1)[0]
    assert begin.count("origin=") <= 1
    assert "origin=http://evil.com origin=" not in begin


def test_adv_dom_006_wildcard_excludes_apex():
    assert not host_matches("example.com", "*.example.com")
    assert host_matches("www.example.com", "*.example.com")


def test_activity_feed_on_watch(api_server: PoolServer):
    base = api_server.base_url
    lid = _lease(base)
    code, env = _req(
        "POST",
        f"{base}/v1/leases/{lid}/alerts",
        {"event": "need_human", "reason": "login", "detail": "please", "ttl_s": 120},
    )
    assert code == 200, env
    watch = env["alert"]["watch_url"]
    token = _token_from_watch_url(watch)

    code, nav = _req(
        "POST",
        f"{base}/v1/leases/{lid}/navigate",
        {"url": "https://example.com/path?tok=sekrit"},
    )
    assert code == 200, nav

    code, html = _req("GET", watch)
    assert code == 200
    body = html.decode() if isinstance(html, (bytes, bytearray)) else html
    assert "Activity" in body
    assert "/watch/events" in body

    code, feed = _req("GET", f"{base}/v1/leases/{lid}/watch/events?token={token}")
    assert code == 200, feed
    kinds = [e["kind"] for e in feed["events"]]
    assert "alert" in kinds
    assert "navigate" in kinds
    blob = json.dumps(feed)
    assert "sekrit" not in blob

    code, conf = _req(
        "POST", f"{base}/v1/leases/{lid}/watch/confirm?token={token}", {}
    )
    assert code == 200, conf
    code, feed2 = _req(
        "GET",
        f"{base}/v1/leases/{lid}/watch/events?token={token}&after_seq=0",
    )
    assert code == 200
    assert any(e["kind"] == "confirm" for e in feed2["events"])

    code, inp = _req(
        "POST",
        f"{base}/v1/leases/{lid}/watch/input?token={token}",
        {"kind": "type", "text": "super-secret-password"},
    )
    assert code == 200, inp
    code, feed3 = _req("GET", f"{base}/v1/leases/{lid}/watch/events?token={token}")
    assert code == 200
    blob3 = json.dumps(feed3)
    assert "super-secret-password" not in blob3
    type_ev = [e for e in feed3["events"] if e["kind"] == "type"]
    assert type_ev
    assert type_ev[-1]["detail"].get("text_len") == len("super-secret-password")

    code, done = _req(
        "POST",
        f"{base}/v1/leases/{lid}/alerts",
        {"event": "task_done", "outcome": {"ok": True, "summary": "done"}},
    )
    assert code == 200, done
    code, gone = _req("GET", f"{base}/v1/leases/{lid}/watch/events?token={token}")
    assert code == 410

    lid2 = _lease(base, space="af-space-2")
    code, env2 = _req(
        "POST",
        f"{base}/v1/leases/{lid2}/alerts",
        {"event": "need_human", "reason": "other"},
    )
    assert code == 200
    code, bad = _req("GET", f"{base}/v1/leases/{lid2}/watch/events?token=wrong")
    assert code == 401
