"""Login-once wizard + signed-in Space badge (slipstream-login-once-001)."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

import pytest

from slipstream.api import PoolServer, is_refused_credentials_path
from slipstream.config import PoolConfig
from slipstream.pool import BrowserPool
from slipstream.signed_in import (
    SignedInValidationError,
    normalize_signed_in_host,
    validate_signed_in_body,
)


@pytest.fixture
def pool(tmp_path):
    cfg = PoolConfig(
        K=3,
        W=1,
        spaces_root=tmp_path / "spaces",
        vault_root=tmp_path / "vault",
        mock=True,
        host="127.0.0.1",
        port=0,
    )
    p = BrowserPool(cfg)
    yield p
    p.shutdown()


@pytest.fixture
def api_server(tmp_path):
    cfg = PoolConfig(
        K=3,
        W=1,
        spaces_root=tmp_path / "spaces",
        vault_root=tmp_path / "vault",
        cdp_base_port=19622,
        mock=True,
        host="127.0.0.1",
        port=18771,
    )
    pool = BrowserPool(cfg)
    server = PoolServer(pool, port=18771)
    server.start(background=True)
    yield server
    server.stop()


def _req(method: str, url: str, body: dict | None = None):
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"} if body is not None else {}
    r = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"raw": raw}
        return e.code, payload


def test_validate_signed_in_body_and_host():
    assert validate_signed_in_body({"signed_in": True, "host": "GitHub.com"}) == (
        True,
        "github.com",
    )
    assert validate_signed_in_body({"signed_in": "true"}) == (True, None)
    assert validate_signed_in_body({"signed_in": False, "host": "x.com"}) == (
        False,
        None,
    )
    assert normalize_signed_in_host("https://github.com/login") == "github.com"
    with pytest.raises(SignedInValidationError):
        normalize_signed_in_host("https://user:pass@evil.com")
    with pytest.raises(SignedInValidationError):
        validate_signed_in_body({"signed_in": True, "cookie": "a=b"})
    with pytest.raises(SignedInValidationError):
        validate_signed_in_body({})


def test_mark_unmark_signed_in_and_badge_in_list(pool: BrowserPool):
    out = pool.set_space_signed_in("s1", {"signed_in": True, "host": "github.com"})
    assert out["signed_in"] is True
    assert out["signed_in_host"] == "github.com"
    assert out["user_metadata"]["signed_in"] == "true"
    assert "persist_note" in out
    assert "cookie" not in json.dumps(out).lower() or "never cookie" in out["persist_note"].lower()

    spaces = pool.list_spaces()
    item = next(s for s in spaces["spaces"] if s["space_id"] == "s1")
    assert item["signed_in"] is True
    assert item["signed_in_host"] == "github.com"

    # q= filter via mirrored metadata
    filtered = pool.list_spaces(q="signed_in=true")
    assert any(s["space_id"] == "s1" for s in filtered["spaces"])

    lease = pool.lease("agent-a", "s1")
    assert lease["signed_in"] is True
    assert lease["signed_in_host"] == "github.com"

    leases = pool.list_leases()
    assert leases["leases"][0]["signed_in"] is True

    cleared = pool.set_space_signed_in("s1", {"signed_in": False})
    assert cleared["signed_in"] is False
    assert "signed_in_host" not in cleared
    lease2 = pool.list_leases()["leases"][0]
    assert lease2["signed_in"] is False


def test_need_human_login_path_still_works(api_server: PoolServer):
    base = api_server.base_url
    code, lease = _req(
        "POST",
        f"{base}/v1/leases",
        {"agent_id": "a1", "space_id": "login-space"},
    )
    assert code == 200
    lid = lease["lease_id"]
    code, env = _req(
        "POST",
        f"{base}/v1/leases/{lid}/alerts",
        {"event": "need_human", "reason": "login", "detail": "please sign in", "ttl_s": 120},
    )
    assert code == 200
    assert env["alert"]["reason"] == "login"
    assert env["alert"]["status"] == "awaiting_human"
    assert "watch_url" in env["alert"]
    assert env["harness"]["action"] == "pause"
    # Watch HTML includes not-signed-in chip + mark form for reason=login
    watch = env["harness"]["watch_url"] + "&mode=takeover"
    with urllib.request.urlopen(watch, timeout=5) as resp:
        html = resp.read().decode()
    assert "not signed-in" in html or "chip off" in html
    assert "Mark Space signed-in" in html


def test_login_once_endpoint_and_mark_api(api_server: PoolServer):
    base = api_server.base_url
    code, out = _req(
        "POST",
        f"{base}/v1/spaces/once-space/login-once",
        {"agent_id": "a1", "detail": "sign in", "host": "example.com", "ttl_s": 90},
    )
    assert code == 200
    assert out["flow"] == "login_once"
    assert out["lease"]["space_id"] == "once-space"
    assert out["alert"]["reason"] == "login"
    assert out["host_hint"] == "example.com"
    assert "watch_url" in out["harness"]

    code, marked = _req(
        "POST",
        f"{base}/v1/spaces/once-space/signed-in",
        {"signed_in": True, "host": "example.com"},
    )
    assert code == 200
    assert marked["signed_in"] is True
    assert marked["signed_in_host"] == "example.com"

    code, spaces = _req("GET", f"{base}/v1/spaces?q=signed_in=true")
    assert code == 200
    assert any(s["space_id"] == "once-space" and s["signed_in"] for s in spaces["spaces"])

    code, leases = _req("GET", f"{base}/v1/leases")
    assert code == 200
    assert leases["leases"][0]["signed_in"] is True

    # Watch chip flips to signed-in
    watch = out["harness"]["watch_url"]
    with urllib.request.urlopen(watch, timeout=5) as resp:
        html = resp.read().decode()
    assert "chip on" in html or ">signed-in" in html

    code, cleared = _req(
        "POST",
        f"{base}/v1/spaces/once-space/signed-in",
        {"signed_in": False},
    )
    assert code == 200
    assert cleared["signed_in"] is False


def test_watch_mark_signed_in_tokenized(api_server: PoolServer):
    base = api_server.base_url
    code, out = _req(
        "POST",
        f"{base}/v1/spaces/wmark/login-once",
        {"agent_id": "a1", "ttl_s": 120},
    )
    assert code == 200
    watch_url = out["harness"]["watch_url"]
    token = urllib.parse.parse_qs(urllib.parse.urlparse(watch_url).query)["token"][0]
    lid = out["lease"]["lease_id"]
    # Confirm take-over first (optional for mark, but exercises path)
    _req("POST", f"{base}/v1/leases/{lid}/watch/confirm?token={token}", {})
    code, marked = _req(
        "POST",
        f"{base}/v1/leases/{lid}/watch/mark-signed-in?token={token}",
        {"signed_in": True, "host": "news.ycombinator.com"},
    )
    assert code == 200
    assert marked["signed_in"] is True
    assert marked["signed_in_host"] == "news.ycombinator.com"
    # Bad token
    code, err = _req(
        "POST",
        f"{base}/v1/leases/{lid}/watch/mark-signed-in?token=nope",
        {"signed_in": True},
    )
    assert code in (401, 403, 404)


def test_refuse_secret_free_read_endpoints(api_server: PoolServer):
    base = api_server.base_url
    for path in (
        "/v1/spaces/s1/credentials/cookies",
        "/v1/spaces/s1/credentials/secret",
        "/v1/spaces/s1/credentials/storage_state",
        "/v1/spaces/s1/credentials/dump",
    ):
        code, body = _req("GET", f"{base}{path}")
        assert code == 404
        assert body.get("error") == "refused"
        code, body = _req("POST", f"{base}{path}", {})
        assert code == 404
        assert body.get("error") == "refused"
    assert is_refused_credentials_path(
        ["v1", "spaces", "s1", "credentials", "cookies"]
    )
    # Mark endpoint must refuse secret keys in body
    code, body = _req(
        "POST",
        f"{base}/v1/spaces/s1/signed-in",
        {"signed_in": True, "password": "nope"},
    )
    assert code == 400
    assert body.get("error") == "invalid_signed_in"


def test_signed_in_response_never_contains_cookie_values(pool: BrowserPool):
    out = pool.set_space_signed_in("safe", {"signed_in": True, "host": "example.com"})
    blob = json.dumps(out)
    assert "Set-Cookie" not in blob
    assert "cookie_jar" not in blob
    assert "storageState" not in blob
    assert out["signed_in"] is True
