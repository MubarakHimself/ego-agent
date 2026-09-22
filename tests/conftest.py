"""Shared fixtures — always mock Chrome unless @pytest.mark.live."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from slipstream.cli import _validate_api_request_url
from slipstream.config import PoolConfig
from slipstream.pool import BrowserPool


@pytest.fixture
def mock_config(tmp_path):
    return PoolConfig(
        K=5,
        W=1,
        idle_ttl_seconds=300,
        spaces_root=tmp_path / "spaces",
        cdp_base_port=19222,
        mock=True,
        headless=True,
    )


@pytest.fixture
def pool(mock_config):
    p = BrowserPool(mock_config)
    yield p
    p.shutdown()


def ladder_http(method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
    """Loopback JSON helper for ladder / navigate / fill tests."""
    if not url.startswith(("http://127.0.0.1:", "http://localhost:")):
        raise ValueError(f"test helper refuses non-loopback URL: {url!r}")
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        _validate_api_request_url(url),
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as resp:  # skylos: ignore[SKY-D216]
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def grant_ladder(base: str, lease_id: str, category: str, summary: str = "test allow") -> str:
    """act → confirm; return confirm_id. Grants one-shot CDP allowance."""
    code, body = ladder_http(
        "POST",
        f"{base}/v1/leases/{lease_id}/actions",
        {"category": category, "summary": summary},
    )
    assert code == 202, body
    cid = body["confirm_id"]
    code, resolved = ladder_http(
        "POST",
        f"{base}/v1/leases/{lease_id}/confirmations/{cid}",
        {"action": "confirm"},
    )
    assert code == 200 and resolved.get("decision") == "allow", resolved
    return cid
