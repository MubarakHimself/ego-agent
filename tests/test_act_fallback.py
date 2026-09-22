"""Stagehand-style act → multi-step fallback — mock fail/retry/ladder."""

from __future__ import annotations

import pytest

import json

from slipstream.actions import (
    STATUS_ACT_OK,
    STATUS_NEED_FALLBACK,
    ActFallbackValidationError,
    act_step_summary,
    parse_act_request,
)
from slipstream.activity_feed import safe_url_summary
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


def test_plan_cap_respects_env_and_rejects_bool(monkeypatch):
    monkeypatch.setenv("SLIPSTREAM_ACT_FALLBACK_MAX_STEPS", "3")
    # Client cannot raise above env cap.
    with pytest.raises(ActFallbackValidationError, match="max_steps"):
        parse_act_request(
            {
                "kind": "click",
                "selector": "#a",
                "max_steps": 10,
                "fallback_plan": [
                    {"kind": "click", "selector": f"#f{i}"} for i in range(4)
                ],
            }
        )
    parsed = parse_act_request(
        {
            "kind": "click",
            "selector": "#a",
            "max_steps": 2,
            "fallback_plan": [
                {"kind": "click", "selector": "#f0"},
                {"kind": "click", "selector": "#f1"},
            ],
        }
    )
    assert parsed["max_steps"] == 2
    # bool True must not be treated as int 1 (ADV-ACT-002).
    with pytest.raises(ActFallbackValidationError, match="max_steps"):
        parse_act_request(
            {"kind": "click", "selector": "#a", "max_steps": True, "fallback_plan": []}
        )


def test_act_step_summary_scrubs_navigate_query():
    raw = "https://example.com/path?sig=secret&token=abc#frag"
    summary = act_step_summary({"kind": "navigate", "url": raw})
    assert "sig=secret" not in summary
    assert "token=abc" not in summary
    assert "frag" not in summary
    assert safe_url_summary(raw) in summary


def test_navigate_secret_not_in_steps_run_or_feed(api_server: PoolServer):
    base = api_server.base_url
    lid = _lease(base, "scrub-nav-space")
    grant_ladder(base, lid, "nav_irreversible", "act navigate scrub")
    secret = "https://example.com/ok?sig=secret&token=abc#frag"
    code, body = ladder_http(
        "POST",
        f"{base}/v1/leases/{lid}/act",
        {"kind": "navigate", "url": secret},
    )
    assert code == 200, body
    blob = json.dumps(body)
    assert "sig=secret" not in blob
    assert "token=abc" not in blob
    assert "#frag" not in blob
    for step in body.get("steps_run") or []:
        result = step.get("result") or {}
        for key in ("url", "observed_url"):
            if key in result:
                assert "?" not in result[key]
                assert "#" not in result[key]
    # Activity feed summaries must also stay scrubbed.
    feed = api_server.pool._activity_feeds.get(lid)
    assert feed is not None
    for ev in feed.list_events(after_seq=0):
        s = json.dumps(ev)
        assert "sig=secret" not in s
        assert "token=abc" not in s


def test_fallback_plan_navigate_ladder_gated(api_server: PoolServer):
    """ADV-ACT-005: navigate inside fallback_plan still needs ladder grant."""
    base = api_server.base_url
    lid = _lease(base, "fb-nav-gate-space")
    api_server.pool._mock_act_fail_remaining[lid] = 1
    code, body = ladder_http(
        "POST",
        f"{base}/v1/leases/{lid}/act",
        {
            "kind": "click",
            "selector": "#primary-miss",
            "soft_retry": False,
            "fallback_plan": [{"kind": "navigate", "url": "https://example.com/next"}],
        },
    )
    assert code == 403, body
    assert body["status"] == "confirmation_required"
    assert body["category"] == "nav_irreversible"
