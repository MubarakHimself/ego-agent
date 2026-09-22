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
        {"agent_id": "a1", "space_id": "s1", "mode": "isolated"},
    )
    assert code == 200
    lid = lease["lease_id"]

    code, hb = _req("POST", f"{base}/v1/leases/{lid}/heartbeat", {})
    assert code == 200 and hb["ok"] is True

    code, rel = _req("DELETE", f"{base}/v1/leases/{lid}")
    assert code == 200 and rel["released"] is True


def test_pool_full_returns_503(api_server: PoolServer):
    base = api_server.base_url
    for i in range(5):
        code, _ = _req("POST", f"{base}/v1/leases", {"agent_id": f"a{i}", "space_id": f"s{i}"})
        assert code == 200
    code, body = _req("POST", f"{base}/v1/leases", {"agent_id": "overflow", "space_id": "sx"})
    assert code == 503
    assert body["error"] == "pool_full"
