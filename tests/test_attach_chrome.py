"""Space tiers: ephemeral / named / attach (slipstream-attach-chrome-001)."""

from __future__ import annotations

import os

import pytest

from slipstream.config import PoolConfig
from slipstream.models import SlotStatus
from slipstream.pool import BrowserPool
from slipstream.tiers import (
    ATTACH_RISK_LABEL,
    TIER_ATTACH,
    TIER_EPHEMERAL,
    TIER_NAMED,
    AttachDisabledError,
    TierError,
    parse_tier,
    resolve_attach_cdp,
)


def test_parse_tier_and_cdp_validation():
    assert parse_tier(None) == TIER_EPHEMERAL
    assert parse_tier("named") == TIER_NAMED
    assert parse_tier("ATTACH") == TIER_ATTACH
    with pytest.raises(TierError):
        parse_tier("cloud")
    http, port = resolve_attach_cdp(cdp_port=9222)
    assert http == "http://127.0.0.1:9222"
    assert port == 9222
    http, port = resolve_attach_cdp(cdp_url="ws://127.0.0.1:9333/devtools/browser/x")
    assert http.startswith("http://127.0.0.1:9333")
    with pytest.raises(TierError):
        resolve_attach_cdp(cdp_url="file:///tmp/x")
    with pytest.raises(TierError):
        resolve_attach_cdp(cdp_url="javascript:alert(1)")
    with pytest.raises(TierError):
        resolve_attach_cdp(cdp_url="http://evil.example:9222")


def test_ephemeral_still_spawns(pool: BrowserPool):
    lease = pool.lease("agent-1", "space-eph")
    assert lease["tier"] == TIER_EPHEMERAL
    assert "risk_label" not in lease
    handle = pool._handles[lease["slot_id"]]
    assert handle.external is False
    assert handle.mocked is True  # mock config
    slot = pool._slots[lease["slot_id"]]
    assert slot.status == SlotStatus.LEASED
    assert slot.chromium_pid is not None


def test_named_tier_labels_durable_profile(pool: BrowserPool):
    lease = pool.lease("agent-1", "space-named", tier="named")
    assert lease["tier"] == TIER_NAMED
    assert "risk_label" not in lease
    # Named still pool-spawns Chromium (durable profile dir).
    assert pool._handles[lease["slot_id"]].external is False


def test_attach_requires_gate(pool: BrowserPool, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SLIPSTREAM_ALLOW_ATTACH", raising=False)
    with pytest.raises(AttachDisabledError):
        pool.lease(
            "agent-1",
            "space-att",
            tier="attach",
            cdp_url="http://127.0.0.1:9222",
        )


def test_attach_skips_spawn_and_uses_cdp(
    pool: BrowserPool, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("SLIPSTREAM_ALLOW_ATTACH", "1")
    lease = pool.lease(
        "agent-1",
        "space-att",
        mode="attach",
        cdp_port=9333,
    )
    assert lease["tier"] == TIER_ATTACH
    assert lease["risk_label"] == ATTACH_RISK_LABEL
    handle = pool._handles[lease["slot_id"]]
    assert handle.external is True
    assert handle.cdp_http_url == "http://127.0.0.1:9333"
    slot = pool._slots[lease["slot_id"]]
    assert slot.chromium_pid is None
    assert slot.cdp_http_url == "http://127.0.0.1:9333"

    listed = pool.list_leases()
    row = next(r for r in listed["leases"] if r["lease_id"] == lease["lease_id"])
    assert row["tier"] == TIER_ATTACH
    assert row["risk_label"] == ATTACH_RISK_LABEL

    sessions = pool.list_sessions()
    srow = next(r for r in sessions["sessions"] if r["lease_id"] == lease["lease_id"])
    assert srow["tier"] == TIER_ATTACH
    assert srow["risk_label"] == ATTACH_RISK_LABEL


def test_attach_release_does_not_stop_external_chrome(
    pool: BrowserPool, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("SLIPSTREAM_ALLOW_ATTACH", "1")
    lease = pool.lease(
        "agent-1",
        "space-att2",
        tier="attach",
        cdp_url="http://127.0.0.1:9444",
    )
    lid = lease["lease_id"]
    slot_id = lease["slot_id"]
    handle = pool._handles[slot_id]
    assert handle.external is True

    stops: list = []
    real_stop = pool.launcher.stop

    def tracking_stop(h, *a, **k):
        stops.append(h)
        return real_stop(h, *a, **k)

    monkeypatch.setattr(pool.launcher, "stop", tracking_stop)
    rel = pool.release(lid)
    assert rel["released"] is True
    assert rel.get("kept_warm") is False
    # stop may be called with external handle — must be a no-op (Chrome lives).
    assert lid not in pool._leases
    assert pool._handles.get(slot_id) is None
    for h in stops:
        assert h.external is True


def test_attach_bad_url_rejected(pool: BrowserPool, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SLIPSTREAM_ALLOW_ATTACH", "1")
    with pytest.raises(TierError):
        pool.lease(
            "agent-1",
            "space-bad",
            tier="attach",
            cdp_url="file:///etc/passwd",
        )


def test_space_default_tier_named(pool: BrowserPool):
    pool.set_space_metadata("space-def", tier="named")
    meta = pool.get_space_metadata("space-def")
    assert meta["tier"] == TIER_NAMED
    lease = pool.lease("agent-1", "space-def")
    assert lease["tier"] == TIER_NAMED


def test_cli_tier_flags():
    from slipstream.__main__ import build_parser

    p = build_parser()
    a = p.parse_args(
        [
            "lease",
            "--agent-id",
            "a",
            "--space-id",
            "s",
            "--tier",
            "attach",
            "--cdp-port",
            "9222",
        ]
    )
    assert a.tier == "attach"
    assert a.cdp_port == 9222
