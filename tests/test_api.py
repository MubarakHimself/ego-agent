"""HTTP API smoke tests with mocked pool."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from ego_pool.api import PoolServer
from ego_pool.config import PoolConfig
from ego_pool.pool import BrowserPool


@pytest.fixture
def api_server(tmp_path):
    cfg = PoolConfig(
        K=5,
        W=1,
        spaces_root=tmp_path / "spaces",
        cdp_base_port=19322,
        mock=True,
        host="127.0.0.1",
        port=18755,
    )
    pool = BrowserPool(cfg)
    server = PoolServer(pool, port=18755)
    server.start(background=True)
    yield server
    server.stop()


def _req(method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
    # Tests only talk to the local PoolServer (loopback).
    if not url.startswith(("http://127.0.0.1:", "http://localhost:")):
        raise ValueError(f"test helper refuses non-loopback URL: {url!r}")
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def test_health_and_status(api_server: PoolServer):
    code, body = _req("GET", f"{api_server.base_url}/healthz")
    assert code == 200 and body["ok"] is True
    code, body = _req("GET", f"{api_server.base_url}/v1/pool/status")
    assert code == 200
    assert body["K"] == 5 and body["W"] == 1


def test_lease_heartbeat_delete(api_server: PoolServer):
    base = api_server.base_url
    code, lease = _req(
        "POST",
        f"{base}/v1/leases",
        {"agent_id": "a1", "space_id": "s1"},
    )
    assert code == 200
    lid = lease["lease_id"]

    code, hb = _req("POST", f"{base}/v1/leases/{lid}/heartbeat", {})
    assert code == 200 and hb["ok"] is True

    code, rel = _req("DELETE", f"{base}/v1/leases/{lid}")
    assert code == 200 and rel["released"] is True
    assert rel["kept_warm"] is True


def test_pool_full_returns_503(api_server: PoolServer):
    base = api_server.base_url
    for i in range(5):
        code, _ = _req("POST", f"{base}/v1/leases", {"agent_id": f"a{i}", "space_id": f"s{i}"})
        assert code == 200
    code, body = _req("POST", f"{base}/v1/leases", {"agent_id": "overflow", "space_id": "sx"})
    assert code == 503
    assert body["error"] == "pool_full"


def test_space_in_use_returns_409(api_server: PoolServer):
    base = api_server.base_url
    code, _ = _req("POST", f"{base}/v1/leases", {"agent_id": "a1", "space_id": "shared"})
    assert code == 200
    code, body = _req("POST", f"{base}/v1/leases", {"agent_id": "a2", "space_id": "shared"})
    assert code == 409
    assert body["error"] == "space_in_use"


def test_bad_space_id_returns_400(api_server: PoolServer):
    base = api_server.base_url
    for bad in (".", "..", "", "a/b", "a\\b", "foo..bar"):
        code, body = _req(
            "POST",
            f"{base}/v1/leases",
            {"agent_id": "a1", "space_id": bad},
        )
        # empty space_id hits "required" check; others hit validation
        assert code == 400
        assert "error" in body


def test_slash_space_id_not_collapsed(api_server: PoolServer):
    base = api_server.base_url
    code, body = _req(
        "POST",
        f"{base}/v1/leases",
        {"agent_id": "a1", "space_id": "task/42"},
    )
    assert code == 400
    assert body["error"] == "bad_request"
    # Underscore form is accepted and stays distinct
    code, lease = _req(
        "POST",
        f"{base}/v1/leases",
        {"agent_id": "a1", "space_id": "task_42"},
    )
    assert code == 200
    assert lease["space_id"] == "task_42"


def test_bad_ttl_seconds_returns_400(api_server: PoolServer):
    base = api_server.base_url
    for bad_ttl in ("not-a-number", True, 1.5, 0, -5, []):
        code, body = _req(
            "POST",
            f"{base}/v1/leases",
            {"agent_id": "a1", "space_id": "s1", "ttl_seconds": bad_ttl},
        )
        assert code == 400, f"ttl={bad_ttl!r} expected 400 got {code}"
        assert body["error"] in ("invalid_ttl_seconds", "bad_request")


def test_post_release_alias_removed(api_server: PoolServer):
    base = api_server.base_url
    code, lease = _req("POST", f"{base}/v1/leases", {"agent_id": "a1", "space_id": "s1"})
    assert code == 200
    lid = lease["lease_id"]
    code, body = _req("POST", f"{base}/v1/leases/{lid}/release", {"reason": "done"})
    assert code == 404
    # DELETE still works
    code, rel = _req("DELETE", f"{base}/v1/leases/{lid}")
    assert code == 200 and rel["released"] is True


def test_launch_failure_returns_503(api_server: PoolServer, monkeypatch):
    base = api_server.base_url

    def boom(*_a, **_k):
        raise RuntimeError("chrome missing")

    monkeypatch.setattr(api_server.pool.launcher, "launch", boom)
    code, body = _req("POST", f"{base}/v1/leases", {"agent_id": "a1", "space_id": "s1"})
    assert code == 503
    assert body["error"] == "launch_failed"
    # Slot freed — subsequent lease (with mock restored) works
    monkeypatch.undo()
    # Re-bind a working launch by creating a fresh mock launcher behavior
    from ego_pool.launcher import ChromiumLauncher

    api_server.pool.launcher = ChromiumLauncher(api_server.pool.config)
    code, lease = _req("POST", f"{base}/v1/leases", {"agent_id": "a1", "space_id": "s1"})
    assert code == 200
    assert lease["status"] == "leased"


def test_oserror_launch_failure_returns_503(api_server: PoolServer, monkeypatch):
    base = api_server.base_url

    def boom(*_a, **_k):
        raise OSError("errno spawn failed")

    monkeypatch.setattr(api_server.pool.launcher, "launch", boom)
    code, body = _req("POST", f"{base}/v1/leases", {"agent_id": "a1", "space_id": "s1"})
    assert code == 503
    assert body["error"] == "launch_failed"
