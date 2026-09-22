"""ADV-PL-001 server-enforced permission ladder — mock CDP refuse / one-shot."""

from __future__ import annotations

import pytest

from slipstream.actions import CATEGORIES, ENFORCED_CDP_CATEGORIES
from slipstream.api import PoolServer
from slipstream.cdp_inject import MockCdpInjector
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
        cdp_base_port=19722,
        mock=True,
        host="127.0.0.1",
        port=18791,
    )
    pool = BrowserPool(cfg)
    pool._cdp_injector = MockCdpInjector(current_url="https://example.com/login")
    server = PoolServer(pool, port=18791)
    server.start(background=True)
    yield server
    server.stop()


def _lease(base: str, space: str = "enf-space") -> str:
    code, lease = ladder_http(
        "POST",
        f"{base}/v1/leases",
        {"agent_id": "agent-enf", "space_id": space},
    )
    assert code == 200, lease
    return lease["lease_id"]


def test_categories_include_fill_and_enforced():
    assert "fill" in CATEGORIES
    assert ENFORCED_CDP_CATEGORIES == frozenset({"fill", "eval", "nav_irreversible"})


def test_fill_eval_nav_refused_without_confirm(api_server: PoolServer):
    base = api_server.base_url
    lid = _lease(base, "refuse-space")

    code, body = ladder_http(
        "POST",
        f"{base}/v1/leases/{lid}/credentials/fill",
        {"cred_id": "cred_x", "fields": {"username": "#u"}},
    )
    assert code == 403
    assert body["error"] == "confirmation_required"
    assert body["category"] == "fill"

    code, body = ladder_http(
        "POST",
        f"{base}/v1/leases/{lid}/eval",
        {"expression": "document.title"},
    )
    assert code == 403
    assert body["error"] == "confirmation_required"
    assert body["category"] == "eval"

    code, body = ladder_http(
        "POST",
        f"{base}/v1/leases/{lid}/navigate",
        {"url": "https://example.com/"},
    )
    assert code == 403
    assert body["error"] == "confirmation_required"
    assert body["category"] == "nav_irreversible"

    # No mock CDP side-effects
    inj = api_server.pool._cdp_injector
    assert isinstance(inj, MockCdpInjector)
    assert inj.calls == []
    assert api_server.pool._nav_log == []


def test_confirm_allows_once_then_re_gate_cdp(api_server: PoolServer):
    base = api_server.base_url
    lid = _lease(base, "once-cdp")

    grant_ladder(base, lid, "eval", "probe title")
    code, body = ladder_http(
        "POST",
        f"{base}/v1/leases/{lid}/eval",
        {"expression": "1+1"},
    )
    assert code == 200 and body["ok"] is True

    # Second eval without fresh confirm → refused
    code, body = ladder_http(
        "POST",
        f"{base}/v1/leases/{lid}/eval",
        {"expression": "2+2"},
    )
    assert code == 403
    assert body["error"] == "confirmation_required"

    # Re-gate: act+confirm again → one more
    grant_ladder(base, lid, "eval", "probe again")
    code, body = ladder_http(
        "POST",
        f"{base}/v1/leases/{lid}/eval",
        {"expression": "3+3"},
    )
    assert code == 200 and body["ok"] is True


def test_nav_and_fill_one_shot(api_server: PoolServer):
    base = api_server.base_url
    lid = _lease(base, "nav-fill")

    # Bind a cred for fill
    code, bind = ladder_http(
        "POST",
        f"{base}/v1/spaces/nav-fill/credentials/bind",
        {
            "label": "demo",
            "origin": "https://example.com",
            "username": "u",
            "secret": "s",
        },
    )
    assert code == 200, bind

    grant_ladder(base, lid, "nav_irreversible", "go home")
    code, body = ladder_http(
        "POST",
        f"{base}/v1/leases/{lid}/navigate",
        {"url": "https://example.com/"},
    )
    assert code == 200 and body["ok"] is True
    code, body = ladder_http(
        "POST",
        f"{base}/v1/leases/{lid}/navigate",
        {"url": "https://example.com/2"},
    )
    assert code == 403

    grant_ladder(base, lid, "fill", "fill login")
    code, fill = ladder_http(
        "POST",
        f"{base}/v1/leases/{lid}/credentials/fill",
        {
            "cred_id": bind["cred_id"],
            "fields": {"username": "#u", "password": "#p"},
        },
    )
    assert code == 200 and fill["ok"] is True
    code, fill2 = ladder_http(
        "POST",
        f"{base}/v1/leases/{lid}/credentials/fill",
        {
            "cred_id": bind["cred_id"],
            "fields": {"username": "#u"},
        },
    )
    assert code == 403


def test_deny_grants_no_allowance(api_server: PoolServer):
    base = api_server.base_url
    lid = _lease(base, "deny-cdp")
    code, body = ladder_http(
        "POST",
        f"{base}/v1/leases/{lid}/actions",
        {"category": "eval", "summary": "nope"},
    )
    assert code == 202
    cid = body["confirm_id"]
    code, resolved = ladder_http(
        "POST",
        f"{base}/v1/leases/{lid}/confirmations/{cid}",
        {"action": "deny"},
    )
    assert code == 200 and resolved["decision"] == "deny"
    code, body = ladder_http(
        "POST",
        f"{base}/v1/leases/{lid}/eval",
        {"expression": "1"},
    )
    assert code == 403


def test_soft_browse_heartbeat_free(api_server: PoolServer):
    """Soft browse (heartbeat / status) stays free — no ladder gate."""
    base = api_server.base_url
    lid = _lease(base, "soft-space")
    code, hb = ladder_http("POST", f"{base}/v1/leases/{lid}/heartbeat", {})
    assert code == 200 and hb["ok"] is True
    code, st = ladder_http("GET", f"{base}/v1/pool/status")
    assert code == 200 and "live" in st
