"""Live Watch v1 — tokenized observe-only JPEG/HTML + Take-over confirm + revoke."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlparse

import pytest

from slipstream.api import PoolServer
from slipstream.cli import _validate_api_request_url
from slipstream.config import PoolConfig
from slipstream.pool import BrowserPool


@pytest.fixture
def api_server(tmp_path):
    cfg = PoolConfig(
        K=5,
        W=1,
        spaces_root=tmp_path / "spaces",
        cdp_base_port=19622,
        mock=True,
        host="127.0.0.1",
        port=18767,
    )
    pool = BrowserPool(cfg)
    server = PoolServer(pool, port=18767)
    server.start(background=True)
    yield server
    server.stop()


def _req(method: str, url: str, body: dict | None = None, *, raw: bool = False):
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
            if raw:
                return resp.status, dict(resp.headers), raw_body
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "json" in ctype:
                return resp.status, json.loads(raw_body.decode("utf-8"))
            return resp.status, raw_body
    except urllib.error.HTTPError as e:
        raw_body = e.read()
        if raw:
            return e.code, dict(e.headers), raw_body
        try:
            return e.code, json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError:
            return e.code, raw_body


def _lease(base: str, space: str = "watch-space") -> str:
    code, lease = _req(
        "POST",
        f"{base}/v1/leases",
        {"agent_id": "agent-watch", "space_id": space},
    )
    assert code == 200, lease
    return lease["lease_id"]


def _token_from_watch_url(watch_url: str) -> str:
    qs = parse_qs(urlparse(watch_url).query)
    assert "token" in qs and qs["token"][0]
    return qs["token"][0]


def test_need_human_mints_tokenized_watch_url(api_server: PoolServer):
    base = api_server.base_url
    lid = _lease(base)
    code, env = _req(
        "POST",
        f"{base}/v1/leases/{lid}/alerts",
        {"event": "need_human", "reason": "stuck", "detail": "need eyes", "ttl_s": 120},
    )
    assert code == 200
    watch = env["alert"]["watch_url"]
    takeover = env["harness"]["takeover_url"]
    assert f"/v1/leases/{lid}/watch" in watch
    assert "token=" in watch
    assert "mode=takeover" in takeover
    assert "token=" in takeover
    # Token must not appear as a JSON field named token
    blob = json.dumps(env)
    assert '"token"' not in blob


def test_watch_html_and_jpeg_observe_only(api_server: PoolServer):
    base = api_server.base_url
    lid = _lease(base)
    code, env = _req(
        "POST",
        f"{base}/v1/leases/{lid}/alerts",
        {"event": "need_human", "reason": "captcha", "detail": "challenge"},
    )
    assert code == 200
    watch = env["alert"]["watch_url"]
    token = _token_from_watch_url(watch)

    code, html = _req("GET", watch)
    assert code == 200
    assert isinstance(html, (bytes, bytearray))
    text = html.decode("utf-8")
    assert "Watch" in text
    assert "observe-only" in text.lower() or "Observe only" in text
    # Safety copy may mention the words; actual secret material must be absent.
    for forbidden in ("cdp_http", "websocketdebugger", "password=", "authorization:", "set-cookie"):
        assert forbidden not in text.lower()

    frame_url = f"{base}/v1/leases/{lid}/watch/frame?token={token}"
    code, headers, jpeg = _req("GET", frame_url, raw=True)
    assert code == 200
    assert "image/jpeg" in (headers.get("Content-Type") or "").lower()
    assert jpeg[:2] == b"\xff\xd8"


def test_watch_missing_or_bad_token_401(api_server: PoolServer):
    base = api_server.base_url
    lid = _lease(base)
    code, env = _req(
        "POST",
        f"{base}/v1/leases/{lid}/alerts",
        {"event": "need_human", "reason": "other"},
    )
    assert code == 200
    code, body = _req("GET", f"{base}/v1/leases/{lid}/watch")
    assert code == 401
    code, body = _req("GET", f"{base}/v1/leases/{lid}/watch?token=not-the-real-token-value-xxx")
    assert code == 401


def test_takeover_confirm_pauses(api_server: PoolServer):
    base = api_server.base_url
    lid = _lease(base)
    code, env = _req(
        "POST",
        f"{base}/v1/leases/{lid}/alerts",
        {"event": "need_human", "reason": "login", "detail": "2FA"},
    )
    assert code == 200
    token = _token_from_watch_url(env["alert"]["watch_url"])
    code, result = _req(
        "POST",
        f"{base}/v1/leases/{lid}/watch/confirm?token={token}",
        {},
    )
    assert code == 200, result
    assert result["ok"] is True
    assert result["agent_paused"] is True
    assert result["action"] == "pause"
    assert result["takeover_confirmed"] is True
    assert result["input_enabled"] is True
    assert result["status"] == "awaiting_human"
    # Lease still alive
    code, hb = _req("POST", f"{base}/v1/leases/{lid}/heartbeat", {})
    assert code == 200


def test_task_done_revokes_watch_410(api_server: PoolServer):
    base = api_server.base_url
    lid = _lease(base)
    code, env = _req(
        "POST",
        f"{base}/v1/leases/{lid}/alerts",
        {"event": "need_human", "reason": "stuck"},
    )
    assert code == 200
    watch = env["alert"]["watch_url"]
    token = _token_from_watch_url(watch)
    code, _ = _req("GET", watch)
    assert code == 200

    code, done = _req(
        "POST",
        f"{base}/v1/leases/{lid}/alerts",
        {"event": "task_done", "outcome": {"ok": True, "summary": "human finished"}},
    )
    assert code == 200
    assert done["harness"]["lease_released"] is True

    code, body = _req("GET", watch)
    assert code == 410, body
    code, body = _req("GET", f"{base}/v1/leases/{lid}/watch/frame?token={token}")
    assert code == 410
    code, body = _req("POST", f"{base}/v1/leases/{lid}/watch/confirm?token={token}", {})
    assert code == 410
    code, body = _req(
        "POST",
        f"{base}/v1/leases/{lid}/watch/input?token={token}",
        {"kind": "click", "x": 1, "y": 1},
    )
    assert code == 410
    code, body = _req("POST", f"{base}/v1/leases/{lid}/watch/cede?token={token}", {})
    assert code == 410


def test_watch_ttl_expiry_410(api_server: PoolServer):
    base = api_server.base_url
    lid = _lease(base)
    code, env = _req(
        "POST",
        f"{base}/v1/leases/{lid}/alerts",
        {"event": "need_human", "reason": "other", "ttl_s": 1},
    )
    assert code == 200
    watch = env["alert"]["watch_url"]
    # Force expiry
    sess = api_server.pool._watches[lid]
    sess.expires_at = time.time() - 1
    code, body = _req("GET", watch)
    assert code == 410


def test_watch_page_no_secrets(api_server: PoolServer):
    base = api_server.base_url
    lid = _lease(base)
    code, env = _req(
        "POST",
        f"{base}/v1/leases/{lid}/alerts",
        {"event": "need_human", "reason": "captcha", "detail": "ok"},
    )
    assert code == 200
    code, html = _req("GET", env["alert"]["watch_url"])
    assert code == 200
    text = html.decode("utf-8").lower()
    for needle in ("set-cookie", "password=", "authorization:", "private_key", "vault_root"):
        assert needle not in text


def test_adv_watch_005_cross_lease_missing_unknown(api_server: PoolServer):
    """ADV-WATCH-005: cross-lease token → 401; missing → 401; unknown lease → 404."""
    base = api_server.base_url
    lid_a = _lease(base, space="watch-a")
    lid_b = _lease(base, space="watch-b")
    code, env_a = _req(
        "POST",
        f"{base}/v1/leases/{lid_a}/alerts",
        {"event": "need_human", "reason": "stuck"},
    )
    assert code == 200
    code, env_b = _req(
        "POST",
        f"{base}/v1/leases/{lid_b}/alerts",
        {"event": "need_human", "reason": "captcha"},
    )
    assert code == 200
    token_a = _token_from_watch_url(env_a["alert"]["watch_url"])
    token_b = _token_from_watch_url(env_b["alert"]["watch_url"])

    # Cross-lease token on B's path → 401
    code, body = _req("GET", f"{base}/v1/leases/{lid_b}/watch?token={token_a}")
    assert code == 401, body
    code, body = _req("GET", f"{base}/v1/leases/{lid_b}/watch/frame?token={token_a}")
    assert code == 401, body
    code, body = _req(
        "POST", f"{base}/v1/leases/{lid_a}/watch/confirm?token={token_b}", {}
    )
    assert code == 401, body
    # ADV-PAIR-004: cross-lease on pair-browse endpoints → 401
    code, body = _req(
        "POST",
        f"{base}/v1/leases/{lid_b}/watch/input?token={token_a}",
        {"kind": "click", "x": 1, "y": 1},
    )
    assert code == 401, body
    code, body = _req(
        "POST", f"{base}/v1/leases/{lid_b}/watch/cede?token={token_a}", {}
    )
    assert code == 401, body

    # Missing token → 401
    code, body = _req("GET", f"{base}/v1/leases/{lid_a}/watch")
    assert code == 401
    code, body = _req("GET", f"{base}/v1/leases/{lid_a}/watch/frame")
    assert code == 401

    # Unknown lease (no watch session) → 404
    code, body = _req(
        "GET",
        f"{base}/v1/leases/lease_does_not_exist_zzzz/watch?token={token_a}",
    )
    assert code == 404, body


def test_adv_watch_003_clickjack_headers(api_server: PoolServer):
    """ADV-WATCH-003: X-Frame-Options DENY + CSP frame-ancestors none."""
    base = api_server.base_url
    lid = _lease(base, space="watch-cj")
    code, env = _req(
        "POST",
        f"{base}/v1/leases/{lid}/alerts",
        {"event": "need_human", "reason": "other"},
    )
    assert code == 200
    watch = env["alert"]["watch_url"]
    token = _token_from_watch_url(watch)

    for url in (
        watch,
        f"{base}/v1/leases/{lid}/watch/frame?token={token}",
    ):
        code, headers, _body = _req("GET", url, raw=True)
        assert code == 200, url
        assert headers.get("X-Frame-Options") == "DENY", headers
        csp = headers.get("Content-Security-Policy") or ""
        assert "frame-ancestors" in csp and "'none'" in csp, csp

    code, headers, _body = _req(
        "POST",
        f"{base}/v1/leases/{lid}/watch/confirm?token={token}",
        {},
        raw=True,
    )
    assert code == 200
    assert headers.get("X-Frame-Options") == "DENY"
    assert "frame-ancestors" in (headers.get("Content-Security-Policy") or "")

    code, headers, _body = _req(
        "POST",
        f"{base}/v1/leases/{lid}/watch/input?token={token}",
        {"kind": "click", "x": 1, "y": 1},
        raw=True,
    )
    assert code == 200
    assert headers.get("X-Frame-Options") == "DENY"

    code, headers, _body = _req(
        "POST",
        f"{base}/v1/leases/{lid}/watch/cede?token={token}",
        {},
        raw=True,
    )
    assert code == 200
    assert headers.get("X-Frame-Options") == "DENY"


def test_adv_watch_001_frame_inflight_vs_revoke_410(api_server: PoolServer, monkeypatch):
    """ADV-WATCH-001: frame in flight vs task_done → 410 (no mock soft-fallback)."""
    base = api_server.base_url
    lid = _lease(base, space="watch-race")
    code, env = _req(
        "POST",
        f"{base}/v1/leases/{lid}/alerts",
        {"event": "need_human", "reason": "stuck"},
    )
    assert code == 200
    token = _token_from_watch_url(env["alert"]["watch_url"])
    frame_url = f"{base}/v1/leases/{lid}/watch/frame?token={token}"

    entered = threading.Event()
    release = threading.Event()

    def slow_capture(cdp_http_url: str, *, mock: bool = False):
        entered.set()
        assert release.wait(timeout=5), "release not signaled"
        # Return distinctive non-mock bytes so a soft-fallback would be visible
        return b"\xff\xd8" + b"RACE" + b"\xff\xd9"

    monkeypatch.setattr("slipstream.pool.capture_jpeg_frame", slow_capture)

    result: dict = {}

    def _worker():
        result["status"], result["headers"], result["body"] = _req(
            "GET", frame_url, raw=True
        )

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    assert entered.wait(timeout=5), "capture never started"
    # Revoke while capture is in flight
    code, done = _req(
        "POST",
        f"{base}/v1/leases/{lid}/alerts",
        {"event": "task_done", "outcome": {"ok": True, "summary": "revoked mid-frame"}},
    )
    assert code == 200
    assert done["harness"]["lease_released"] is True
    # Simulate port reuse: a new lease may occupy the same slot/CDP later;
    # revoke alone must already yield 410 before bytes return.
    release.set()
    t.join(timeout=5)
    assert not t.is_alive()
    assert result["status"] == 410, result
    # Must not have returned the in-flight JPEG (or a mock soft-fallback 200)
    assert b"RACE" not in (result.get("body") or b"")


def test_adv_watch_001_identity_mismatch_after_capture(api_server: PoolServer, monkeypatch):
    """ADV-WATCH-001: slot/CDP identity change during capture → 410."""
    base = api_server.base_url
    lid = _lease(base, space="watch-ident")
    code, env = _req(
        "POST",
        f"{base}/v1/leases/{lid}/alerts",
        {"event": "need_human", "reason": "other"},
    )
    assert code == 200
    token = _token_from_watch_url(env["alert"]["watch_url"])
    frame_url = f"{base}/v1/leases/{lid}/watch/frame?token={token}"

    entered = threading.Event()
    release = threading.Event()

    def slow_capture(cdp_http_url: str, *, mock: bool = False):
        entered.set()
        assert release.wait(timeout=5)
        return b"\xff\xd8XXXX\xff\xd9"

    monkeypatch.setattr("slipstream.pool.capture_jpeg_frame", slow_capture)

    result: dict = {}

    def _worker():
        result["status"], _, result["body"] = _req("GET", frame_url, raw=True)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    assert entered.wait(timeout=5)
    # Mutate lease CDP identity while capture holds (port-reuse style)
    with api_server.pool._lock:
        lease = api_server.pool._leases[lid]
        lease.cdp_http_url = "http://127.0.0.1:1"  # different port identity
    release.set()
    t.join(timeout=5)
    assert result["status"] == 410, result


def test_adv_watch_001_no_mock_soft_fallback_after_auth(api_server: PoolServer, monkeypatch):
    """ADV-WATCH-001: CDP failure after auth must not soft-return mock JPEG 200."""
    from slipstream.watch import WatchCaptureError

    base = api_server.base_url
    lid = _lease(base, space="watch-nofallback")
    code, env = _req(
        "POST",
        f"{base}/v1/leases/{lid}/alerts",
        {"event": "need_human", "reason": "stuck"},
    )
    assert code == 200
    token = _token_from_watch_url(env["alert"]["watch_url"])

    def boom(cdp_http_url: str, *, mock: bool = False):
        raise WatchCaptureError("simulated CDP failure")

    monkeypatch.setattr("slipstream.pool.capture_jpeg_frame", boom)
    code, body = _req("GET", f"{base}/v1/leases/{lid}/watch/frame?token={token}")
    # Not 200 with mock JPEG — capture failure after auth → 502 (no soft-fallback).
    assert code == 502, body
    assert isinstance(body, dict)
    assert body.get("error") == "watch_capture_failed"


def test_adv_watch_006_reject_bad_ws_debugger_url():
    """ADV-WATCH-006: non-loopback / non-ws(s) webSocketDebuggerUrl refused."""
    from slipstream.watch import WatchCaptureError, assert_ws_debugger_url
    from slipstream.cdp_inject import CdpInjectError, _assert_ws_debugger_url

    assert_ws_debugger_url("ws://127.0.0.1:9222/devtools/page/x")
    assert_ws_debugger_url("wss://localhost:9222/devtools/page/x")
    for bad in (
        "http://127.0.0.1:9222/devtools/page/x",
        "ws://evil.example:9222/devtools/page/x",
        "ws://8.8.8.8:9222/devtools/page/x",
        "ftp://127.0.0.1:9222/x",
    ):
        try:
            assert_ws_debugger_url(bad)
            raise AssertionError(f"expected refuse for {bad}")
        except WatchCaptureError:
            pass
        try:
            _assert_ws_debugger_url(bad)
            raise AssertionError(f"expected cdp refuse for {bad}")
        except CdpInjectError:
            pass


def test_pair_browse_observe_only_blocks_input(api_server: PoolServer):
    """Before Take-over confirm, /watch/input is 403."""
    base = api_server.base_url
    lid = _lease(base, space="pb-obs")
    code, env = _req(
        "POST",
        f"{base}/v1/leases/{lid}/alerts",
        {"event": "need_human", "reason": "stuck"},
    )
    assert code == 200
    token = _token_from_watch_url(env["alert"]["watch_url"])
    code, body = _req(
        "POST",
        f"{base}/v1/leases/{lid}/watch/input?token={token}",
        {"kind": "click", "x": 10, "y": 20},
    )
    assert code == 403, body
    assert body.get("error") == "forbidden"
    assert api_server.pool._watch_input_log == []


def test_pair_browse_confirm_click_type_scroll(api_server: PoolServer):
    """After confirm, click/type/scroll reach CDP mock bridge; ack never echoes text."""
    base = api_server.base_url
    lid = _lease(base, space="pb-drive")
    code, env = _req(
        "POST",
        f"{base}/v1/leases/{lid}/alerts",
        {"event": "need_human", "reason": "captcha", "detail": "need eyes"},
    )
    assert code == 200
    token = _token_from_watch_url(env["alert"]["watch_url"])
    code, conf = _req(
        "POST", f"{base}/v1/leases/{lid}/watch/confirm?token={token}", {}
    )
    assert code == 200
    assert conf["input_enabled"] is True
    assert conf["agent_paused"] is True

    # HTML should enable pair-browse + Cede (no secrets)
    code, html = _req(
        "GET", f"{base}/v1/leases/{lid}/watch?token={token}&mode=takeover"
    )
    assert code == 200
    text = html.decode("utf-8")
    assert "pair-browse" in text.lower() or "Pair-browse" in text
    assert "Cede" in text
    assert "/watch/input" in text
    for forbidden in ("password=", "set-cookie", "cdp_http", "authorization:"):
        assert forbidden not in text.lower()

    code, click = _req(
        "POST",
        f"{base}/v1/leases/{lid}/watch/input?token={token}",
        {"kind": "click", "x": 42, "y": 84, "button": "left"},
    )
    assert code == 200, click
    assert click == {"ok": True, "kind": "click", "input_enabled": True}

    secretish = "hunter2-not-a-real-secret"
    code, typed = _req(
        "POST",
        f"{base}/v1/leases/{lid}/watch/input?token={token}",
        {"kind": "type", "text": secretish},
    )
    assert code == 200, typed
    assert typed == {"ok": True, "kind": "type", "input_enabled": True}
    # Must not echo typed text in response
    assert secretish not in json.dumps(typed)

    code, scroll = _req(
        "POST",
        f"{base}/v1/leases/{lid}/watch/input?token={token}",
        {"kind": "scroll", "x": 10, "y": 10, "deltaX": 0, "deltaY": -120},
    )
    assert code == 200, scroll
    assert scroll["kind"] == "scroll"

    log = api_server.pool._watch_input_log
    kinds = [e["kind"] for e in log]
    assert kinds == ["click", "type", "scroll"]
    assert log[0]["x"] == 42 and log[0]["y"] == 84
    # ADV-PAIR-003: mock recorder stores text_len only — never full typed text
    assert log[1].get("text_len") == len(secretish)
    assert "text" not in log[1]
    assert secretish not in json.dumps(log)
    assert log[2]["deltaY"] == -120


def test_pair_browse_cede_disables_input_clears_pause(api_server: PoolServer):
    base = api_server.base_url
    lid = _lease(base, space="pb-cede")
    code, env = _req(
        "POST",
        f"{base}/v1/leases/{lid}/alerts",
        {"event": "need_human", "reason": "login"},
    )
    assert code == 200
    token = _token_from_watch_url(env["alert"]["watch_url"])
    code, _ = _req("POST", f"{base}/v1/leases/{lid}/watch/confirm?token={token}", {})
    assert code == 200

    code, ceded = _req("POST", f"{base}/v1/leases/{lid}/watch/cede?token={token}", {})
    assert code == 200, ceded
    assert ceded["ok"] is True
    assert ceded["agent_paused"] is False
    assert ceded["input_enabled"] is False
    assert ceded["action"] == "continue"
    assert ceded["status"] == "leased"
    assert ceded["lease_kept"] is True

    # Input blocked after cede
    code, body = _req(
        "POST",
        f"{base}/v1/leases/{lid}/watch/input?token={token}",
        {"kind": "click", "x": 1, "y": 1},
    )
    assert code == 403, body

    # Lease still alive (heartbeat)
    code, hb = _req("POST", f"{base}/v1/leases/{lid}/heartbeat", {})
    assert code == 200

    # Watch page still observe-able; Confirm available again
    code, html = _req("GET", f"{base}/v1/leases/{lid}/watch?token={token}&mode=takeover")
    assert code == 200
    assert b"Confirm Take-over" in html
    assert b"pair-browse" not in html.lower() or b"until then" in html.lower()


def test_pair_browse_refuses_secret_fields(api_server: PoolServer):
    base = api_server.base_url
    lid = _lease(base, space="pb-sec")
    code, env = _req(
        "POST",
        f"{base}/v1/leases/{lid}/alerts",
        {"event": "need_human", "reason": "other"},
    )
    assert code == 200
    token = _token_from_watch_url(env["alert"]["watch_url"])
    _req("POST", f"{base}/v1/leases/{lid}/watch/confirm?token={token}", {})
    code, body = _req(
        "POST",
        f"{base}/v1/leases/{lid}/watch/input?token={token}",
        {"kind": "type", "text": "x", "password": "nope"},
    )
    assert code == 400, body
    assert body.get("error") == "invalid_input"


def test_pair_browse_bad_token_401(api_server: PoolServer):
    base = api_server.base_url
    lid = _lease(base, space="pb-auth")
    code, env = _req(
        "POST",
        f"{base}/v1/leases/{lid}/alerts",
        {"event": "need_human", "reason": "stuck"},
    )
    assert code == 200
    token = _token_from_watch_url(env["alert"]["watch_url"])
    _req("POST", f"{base}/v1/leases/{lid}/watch/confirm?token={token}", {})
    code, body = _req(
        "POST",
        f"{base}/v1/leases/{lid}/watch/input?token=wrong-token-value-xxxxxxxx",
        {"kind": "click", "x": 1, "y": 1},
    )
    assert code == 401, body


def test_pair_browse_ttl_expiry_blocks_input(api_server: PoolServer):
    """Legacy: Watch TTL → input 410."""
    base = api_server.base_url
    lid = _lease(base, space="pb-ttl")
    code, env = _req(
        "POST",
        f"{base}/v1/leases/{lid}/alerts",
        {"event": "need_human", "reason": "other", "ttl_s": 60},
    )
    assert code == 200
    token = _token_from_watch_url(env["alert"]["watch_url"])
    _req("POST", f"{base}/v1/leases/{lid}/watch/confirm?token={token}", {})
    sess = api_server.pool._watches[lid]
    sess.expires_at = time.time() - 1
    code, body = _req(
        "POST",
        f"{base}/v1/leases/{lid}/watch/input?token={token}",
        {"kind": "click", "x": 1, "y": 1},
    )
    assert code == 410, body


def test_adv_pair_001_watch_ttl_returns_drive(api_server: PoolServer):
    """ADV-PAIR-001: confirm → force expires_at past → lease leased + input 410 + hb 200."""
    base = api_server.base_url
    lid = _lease(base, space="pb-ttl-drive")
    code, env = _req(
        "POST",
        f"{base}/v1/leases/{lid}/alerts",
        {"event": "need_human", "reason": "stuck", "ttl_s": 60},
    )
    assert code == 200
    token = _token_from_watch_url(env["alert"]["watch_url"])
    code, conf = _req(
        "POST", f"{base}/v1/leases/{lid}/watch/confirm?token={token}", {}
    )
    assert code == 200
    assert conf["status"] == "awaiting_human"
    assert api_server.pool._leases[lid].status == "awaiting_human"

    sess = api_server.pool._watches[lid]
    sess.expires_at = time.time() - 1

    code, body = _req(
        "POST",
        f"{base}/v1/leases/{lid}/watch/input?token={token}",
        {"kind": "click", "x": 1, "y": 1},
    )
    assert code == 410, body
    assert api_server.pool._leases[lid].status == "leased"
    assert sess.input_enabled is False
    assert sess.takeover_confirmed is False

    code, hb = _req("POST", f"{base}/v1/leases/{lid}/heartbeat", {})
    assert code == 200, hb


def test_adv_pair_001_watch_ttl_sweep_returns_drive(api_server: PoolServer):
    """ADV-PAIR-001: idle/TTL sweep also returns drive without waiting for watch hit."""
    base = api_server.base_url
    lid = _lease(base, space="pb-ttl-sweep")
    code, env = _req(
        "POST",
        f"{base}/v1/leases/{lid}/alerts",
        {"event": "need_human", "reason": "captcha", "ttl_s": 60},
    )
    assert code == 200
    token = _token_from_watch_url(env["alert"]["watch_url"])
    code, _ = _req("POST", f"{base}/v1/leases/{lid}/watch/confirm?token={token}", {})
    assert code == 200
    sess = api_server.pool._watches[lid]
    sess.expires_at = time.time() - 1
    api_server.pool.evict_idle()
    assert api_server.pool._leases[lid].status == "leased"
    assert sess.input_enabled is False
    assert sess.takeover_confirmed is False
    code, body = _req("GET", f"{base}/v1/leases/{lid}/watch?token={token}")
    assert code == 410, body


def test_adv_pair_002_cede_during_inflight_input_410(api_server: PoolServer, monkeypatch):
    """ADV-PAIR-002: slow CDP + concurrent cede → input 410; lease leased; no dual success."""
    from slipstream import watch as watch_mod

    base = api_server.base_url
    lid = _lease(base, space="pb-race")
    code, env = _req(
        "POST",
        f"{base}/v1/leases/{lid}/alerts",
        {"event": "need_human", "reason": "stuck"},
    )
    assert code == 200
    token = _token_from_watch_url(env["alert"]["watch_url"])
    code, _ = _req("POST", f"{base}/v1/leases/{lid}/watch/confirm?token={token}", {})
    assert code == 200

    release = threading.Event()
    entered = threading.Event()
    real = watch_mod.dispatch_cdp_input

    def slow_dispatch(cdp_http_url, event, *, mock=False, mock_log=None):
        entered.set()
        assert release.wait(timeout=5), "cede did not unblock mock CDP"
        return real(cdp_http_url, event, mock=mock, mock_log=mock_log)

    monkeypatch.setattr(watch_mod, "dispatch_cdp_input", slow_dispatch)
    # pool imported the symbol — patch on pool module too
    import slipstream.pool as pool_mod

    monkeypatch.setattr(pool_mod, "dispatch_cdp_input", slow_dispatch)

    result: dict = {}

    def do_input():
        code, body = _req(
            "POST",
            f"{base}/v1/leases/{lid}/watch/input?token={token}",
            {"kind": "click", "x": 9, "y": 9},
        )
        result["code"] = code
        result["body"] = body

    t = threading.Thread(target=do_input, daemon=True)
    t.start()
    assert entered.wait(timeout=5), "dispatch never entered slow CDP"
    code, ceded = _req("POST", f"{base}/v1/leases/{lid}/watch/cede?token={token}", {})
    assert code == 200, ceded
    assert ceded["status"] == "leased"
    release.set()
    t.join(timeout=5)
    assert not t.is_alive()
    assert result["code"] == 410, result
    assert api_server.pool._leases[lid].status == "leased"
    assert api_server.pool._watches[lid].input_enabled is False


def test_adv_pair_004_cede_without_confirm_400(api_server: PoolServer):
    """ADV-PAIR-004: cede before confirm → 400 nothing to cede (not success no-op)."""
    base = api_server.base_url
    lid = _lease(base, space="pb-cede-early")
    code, env = _req(
        "POST",
        f"{base}/v1/leases/{lid}/alerts",
        {"event": "need_human", "reason": "login"},
    )
    assert code == 200
    token = _token_from_watch_url(env["alert"]["watch_url"])
    assert api_server.pool._leases[lid].status == "awaiting_human"
    code, body = _req("POST", f"{base}/v1/leases/{lid}/watch/cede?token={token}", {})
    assert code == 400, body
    assert body.get("error") == "invalid_input"
    assert "nothing to cede" in (body.get("detail") or "")
    # Status unchanged — still awaiting_human (agent paused for human)
    assert api_server.pool._leases[lid].status == "awaiting_human"
