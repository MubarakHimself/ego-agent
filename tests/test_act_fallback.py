"""Stagehand-style act → multi-step fallback — mock fail/retry/ladder."""

from __future__ import annotations

import pytest

from slipstream.actions import (
    STATUS_ACT_OK,
    STATUS_NEED_FALLBACK,
    parse_act_request,
)
from slipstream.api import PoolServer
from slipstream.config import PoolConfig
from slipstream.pool import BrowserPool

from tests.conftest import grant_ladder, ladder_http


@pytest.fixture
def api_server(tmp_path):
    cfg = PoolConfig(
        K=5,
        W=1,
        spaces_root=tmp_path / "spaces",
        vault_root=tmp_path / "vault",
        cdp_base_port=19741,
        mock=True,
        host="127.0.0.1",
        port=18841,
    )
    pool = BrowserPool(cfg)
    server = PoolServer(pool, port=18841)
    server.start(background=True)
    yield server
    server.stop()


def _lease(base: str, space: str = "act-fb-space") -> str:
    code, lease = ladder_http(
        "POST",
        f"{base}/v1/leases",
        {"agent_id": "agent-act-fb", "space_id": space},
    )
    assert code == 200, lease
    return lease["lease_id"]


def test_parse_accepts_nested_primary_and_plan():
    parsed = parse_act_request(
        {
            "primary": {"kind": "click", "selector": "#a"},
            "fallback_plan": [{"kind": "click", "selector": "#b"}],
            "soft_retry": False,
        }
    )
    assert parsed["primary"]["selector"] == "#a"
    assert parsed["fallback_plan"][0]["selector"] == "#b"
    assert parsed["soft_retry"] == 0


def test_fail_then_fallback_success(api_server: PoolServer):
    base = api_server.base_url
    lid = _lease(base)
    api_server.pool._mock_act_fail_remaining[lid] = 1
    code, body = ladder_http(
        "POST",
        f"{base}/v1/leases/{lid}/act",
        {
            "kind": "click",
            "selector": "#primary",
            "soft_retry": False,
            "fallback_plan": [{"kind": "click", "selector": "#fallback"}],
        },
    )
    assert code == 200, body
    assert body["status"] == STATUS_ACT_OK
    assert body["reason"] == "fallback_plan"
    assert body["attempts"] >= 2


def test_soft_retry_then_ok(api_server: PoolServer):
    base = api_server.base_url
    lid = _lease(base, "soft-retry-space")
    api_server.pool._mock_act_fail_remaining[lid] = 1
    code, body = ladder_http(
        "POST",
        f"{base}/v1/leases/{lid}/act",
        {"kind": "click", "selector": "#once", "soft_retry": True},
    )
    assert code == 200, body
    assert body["status"] == STATUS_ACT_OK
    assert body["reason"] == "soft_retry"


def test_need_fallback_when_no_plan(api_server: PoolServer):
    base = api_server.base_url
    lid = _lease(base, "need-fb-space")
    api_server.pool._mock_act_fail_remaining[lid] = 1
    code, body = ladder_http(
        "POST",
        f"{base}/v1/leases/{lid}/act",
        {"kind": "click", "selector": "#gone", "soft_retry": False},
    )
    assert code == 200, body
    assert body["status"] == STATUS_NEED_FALLBACK
    assert "attempts" in body


def test_gated_navigate_needs_confirm(api_server: PoolServer):
    base = api_server.base_url
    lid = _lease(base, "gated-space")
    code, body = ladder_http(
        "POST",
        f"{base}/v1/leases/{lid}/act",
        {"kind": "navigate", "url": "https://example.com/"},
    )
    assert code == 403, body
    assert body["status"] == "confirmation_required"
    assert body["category"] == "nav_irreversible"


def test_gated_after_confirm_then_ok(api_server: PoolServer):
    base = api_server.base_url
    lid = _lease(base, "gated-ok-space")
    grant_ladder(base, lid, "nav_irreversible", "act navigate once")
    code, body = ladder_http(
        "POST",
        f"{base}/v1/leases/{lid}/act",
        {"kind": "navigate", "url": "https://example.com/ok"},
    )
    assert code == 200, body
    assert body["status"] == STATUS_ACT_OK


def test_soft_browse_free(api_server: PoolServer):
    base = api_server.base_url
    lid = _lease(base, "soft-free-space")
    code, body = ladder_http(
        "POST",
        f"{base}/v1/leases/{lid}/act",
        {"kind": "click", "selector": "#free"},
    )
    assert code == 200, body
    assert body["status"] == STATUS_ACT_OK
    code, hb = ladder_http("POST", f"{base}/v1/leases/{lid}/heartbeat", {})
    assert code == 200, hb
