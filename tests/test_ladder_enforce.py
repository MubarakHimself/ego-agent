"""ADV-PL-001 server-enforced permission ladder — mock CDP refuse / one-shot."""

from __future__ import annotations

import pytest

from slipstream.actions import CATEGORIES, ENFORCED_CDP_CATEGORIES
from slipstream.api import PoolServer
from slipstream.cdp_inject import CdpInjectError, MockCdpInjector
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


def test_lease_omits_raw_cdp_by_default(api_server: PoolServer, monkeypatch):
    """Firstmate B: lease JSON omits cdp_* unless SLIPSTREAM_EXPOSE_RAW_CDP=1."""
    monkeypatch.delenv("SLIPSTREAM_EXPOSE_RAW_CDP", raising=False)
    base = api_server.base_url
    code, lease = ladder_http(
        "POST",
        f"{base}/v1/leases",
        {"agent_id": "agent-cdp", "space_id": "hide-cdp"},
    )
    assert code == 200
    assert "cdp_http_url" not in lease
    assert "cdp_ws_url" not in lease
    assert "cdp_port" not in lease
    # Internal slot still has CDP for pool-side inject
    lid = lease["lease_id"]
    assert api_server.pool._leases[lid].cdp_http_url

    monkeypatch.setenv("SLIPSTREAM_EXPOSE_RAW_CDP", "1")
    code, lease2 = ladder_http(
        "POST",
        f"{base}/v1/leases",
        {"agent_id": "agent-cdp2", "space_id": "show-cdp"},
    )
    assert code == 200
    assert lease2["cdp_http_url"].startswith("http://127.0.0.1:")


def test_origin_mismatch_preserves_fill_grant(api_server: PoolServer):
    """ADV-PL-001-CONSUME: origin check before consume — mismatch does not burn."""
    base = api_server.base_url
    lid = _lease(base, "orig-preserve")
    code, bind = ladder_http(
        "POST",
        f"{base}/v1/spaces/orig-preserve/credentials/bind",
        {
            "label": "demo",
            "origin": "https://example.com",
            "username": "u",
            "secret": "s",
        },
    )
    assert code == 200, bind
    inj = api_server.pool._cdp_injector
    assert isinstance(inj, MockCdpInjector)
    inj.current_url = "https://evil.example/phish"
    inj.calls.clear()

    grant_ladder(base, lid, "fill", "will mismatch")
    code, err = ladder_http(
        "POST",
        f"{base}/v1/leases/{lid}/credentials/fill",
        {"cred_id": bind["cred_id"], "fields": {"username": "#u"}},
    )
    assert code == 400
    assert "origin mismatch" in err.get("detail", "")
    # page_url ran (origin check) but fill_fields did not
    assert any(c.get("purpose") == "page_url" for c in inj.calls)
    assert not any(c.get("field") for c in inj.calls)

    # Grant preserved — fix origin and fill succeeds without re-confirm
    inj.current_url = "https://example.com/login"
    inj.calls.clear()
    code, fill = ladder_http(
        "POST",
        f"{base}/v1/leases/{lid}/credentials/fill",
        {"cred_id": bind["cred_id"], "fields": {"username": "#u"}},
    )
    assert code == 200 and fill["ok"] is True


def test_fill_cdp_failure_refunds_grant(api_server: PoolServer):
    """ADV-PL-001-CONSUME: post-consume fill_fields failure refunds allowance."""
    base = api_server.base_url
    lid = _lease(base, "refund-fill")
    code, bind = ladder_http(
        "POST",
        f"{base}/v1/spaces/refund-fill/credentials/bind",
        {
            "label": "demo",
            "origin": "https://example.com",
            "username": "u",
            "secret": "s",
        },
    )
    assert code == 200, bind
    inj = api_server.pool._cdp_injector
    assert isinstance(inj, MockCdpInjector)
    inj.current_url = "https://example.com/login"

    real_fill = inj.fill_fields

    def boom(cdp_http_url, fields):
        raise CdpInjectError("cdp inject boom")

    inj.fill_fields = boom  # type: ignore[method-assign]
    grant_ladder(base, lid, "fill", "will refund")
    code, err = ladder_http(
        "POST",
        f"{base}/v1/leases/{lid}/credentials/fill",
        {"cred_id": bind["cred_id"], "fields": {"username": "#u"}},
    )
    assert code >= 400
    inj.fill_fields = real_fill  # type: ignore[method-assign]

    # Refunded — second fill without fresh confirm works
    code, fill = ladder_http(
        "POST",
        f"{base}/v1/leases/{lid}/credentials/fill",
        {"cred_id": bind["cred_id"], "fields": {"username": "#u"}},
    )
    assert code == 200 and fill["ok"] is True


def test_unused_grant_ttl_expires(api_server: PoolServer, monkeypatch):
    """ADV-PL-001-IMMORTAL-GRANT: unused allowances TTL with confirm window."""
    monkeypatch.setenv("SLIPSTREAM_CONFIRM_TTL", "1")
    base = api_server.base_url
    lid = _lease(base, "grant-ttl")
    grant_ladder(base, lid, "eval", "expire me")

    import time

    # Advance past grant TTL via expire_due
    store = api_server.pool._confirmations
    key = (lid, "eval")
    assert store._allow_counts.get(key, 0) == 1
    # Force expiry timestamp into the past
    store._allow_expires[key] = time.time() - 1
    store.expire_due(time.time())
    assert store._allow_counts.get(key, 0) == 0

    code, body = ladder_http(
        "POST",
        f"{base}/v1/leases/{lid}/eval",
        {"expression": "1+1"},
    )
    assert code == 403
    assert body["error"] == "confirmation_required"


def test_eval_missing_cdp_does_not_burn(api_server: PoolServer):
    """ADV-PL-001-CONSUME: cdp_http precondition before consume_once."""
    base = api_server.base_url
    lid = _lease(base, "no-cdp-eval")
    grant_ladder(base, lid, "eval", "need cdp")
    # Clear CDP on slot + lease internal handles
    with api_server.pool._lock:
        slot = api_server.pool._find_slot_by_lease(lid)
        assert slot is not None
        slot.cdp_http_url = None
        api_server.pool._leases[lid].cdp_http_url = None

    code, body = ladder_http(
        "POST",
        f"{base}/v1/leases/{lid}/eval",
        {"expression": "1"},
    )
    assert code >= 400
    # Grant preserved
    assert api_server.pool._confirmations.has_allowance(lid, "eval")



def test_status_omits_cdp_ports_by_default(api_server: PoolServer, monkeypatch):
    """ADV-PL-001-BYPASS-RAW-CDP-PORT: status/lease cannot reconstruct CDP without env."""
    monkeypatch.delenv("SLIPSTREAM_EXPOSE_RAW_CDP", raising=False)
    base = api_server.base_url
    lid = _lease(base, "port-leak")

    code, st = ladder_http("GET", f"{base}/v1/pool/status")
    assert code == 200
    assert "cdp_base_port" not in st
    for slot in st["slots"]:
        assert "cdp_port" not in slot
        assert "cdp_http_url" not in slot
        assert "cdp_ws_url" not in slot

    # Lease wire JSON from create already checked in test_lease_omits_raw_cdp_by_default;
    # list leases must also omit reconstructable tip fields.
    code, listed = ladder_http("GET", f"{base}/v1/leases")
    assert code == 200
    rows = listed if isinstance(listed, list) else listed.get("leases") or listed.get("items") or []
    mine = [r for r in rows if r.get("lease_id") == lid]
    assert mine, listed
    for row in mine:
        assert "cdp_port" not in row
        assert "cdp_http_url" not in row
        assert "cdp_ws_url" not in row
        assert "cdp_base_port" not in row

    with api_server.pool._lock:
        internal = api_server.pool._leases[lid]
        slot = api_server.pool._find_slot_by_lease(lid)
        assert internal.cdp_http_url
        assert slot is not None and slot.cdp_port is not None
        internal_url = internal.cdp_http_url
        internal_port = slot.cdp_port

    public_ports = [
        s.get("cdp_port") for s in st["slots"] if s.get("cdp_port") is not None
    ]
    assert internal_port not in public_ports

    monkeypatch.setenv("SLIPSTREAM_EXPOSE_RAW_CDP", "1")
    code, st2 = ladder_http("GET", f"{base}/v1/pool/status")
    assert code == 200
    assert st2["cdp_base_port"] == api_server.pool.config.cdp_base_port
    leased_slots = [s for s in st2["slots"] if s.get("lease_id") == lid]
    assert leased_slots
    assert leased_slots[0]["cdp_port"] == internal_port
    assert leased_slots[0]["cdp_http_url"] == internal_url


def test_nav_matched_false_refunds_grant(api_server: PoolServer, monkeypatch):
    """ADV-PL-001-NAV-SOFTFAIL-BURNS: matched=False refunds nav_irreversible grant."""
    monkeypatch.delenv("SLIPSTREAM_EXPOSE_RAW_CDP", raising=False)
    base = api_server.base_url
    lid = _lease(base, "nav-softfail")

    # Force non-mock navigate path with a stub that returns matched=False
    api_server.pool.config.mock = False
    with api_server.pool._lock:
        slot = api_server.pool._find_slot_by_lease(lid)
        assert slot is not None
        slot.cdp_http_url = "http://127.0.0.1:9"
        api_server.pool._leases[lid].cdp_http_url = slot.cdp_http_url

    import slipstream.cdp_http as cdp_http_mod

    def fake_nav(cdp_http, url, allowed_domains=None):
        return {"matched": False, "title": "", "url": url}

    monkeypatch.setattr(cdp_http_mod, "navigate_via_json_new", fake_nav)

    grant_ladder(base, lid, "nav_irreversible", "will softfail")
    code, body = ladder_http(
        "POST",
        f"{base}/v1/leases/{lid}/navigate",
        {"url": "https://example.com/softfail"},
    )
    assert code == 200
    assert body.get("ok") is False
    assert body.get("matched") is False
    assert api_server.pool._confirmations.has_allowance(lid, "nav_irreversible")

    code, body2 = ladder_http(
        "POST",
        f"{base}/v1/leases/{lid}/navigate",
        {"url": "https://example.com/softfail2"},
    )
    assert code == 200
    assert body2.get("matched") is False
