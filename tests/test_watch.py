"""Live Watch v1 — tokenized observe-only JPEG/HTML + Take-over confirm + revoke."""

from __future__ import annotations

import json
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
