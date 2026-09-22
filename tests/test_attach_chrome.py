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


def test_adv_ac001_release_after_attach_to_ephemeral_never_warms_user_chrome(
    pool: BrowserPool, monkeypatch: pytest.MonkeyPatch
):
    """Re-lease attach→ephemeral fully detaches; release must not FREE_WARM user CDP."""
    monkeypatch.setenv("SLIPSTREAM_ALLOW_ATTACH", "1")
    # W>=1 so warm would be possible if the bug returned.
    pool.config.W = 2

    first = pool.lease(
        "agent-a",
        "space-flip",
        tier="attach",
        cdp_url="http://127.0.0.1:9555",
    )
    assert first["tier"] == TIER_ATTACH
    assert pool._handles[first["slot_id"]].external is True

    # Same agent re-leases without attach → must spawn pool Chrome, not keep user CDP.
    second = pool.lease("agent-a", "space-flip", tier="ephemeral")
    assert second["tier"] == TIER_EPHEMERAL
    assert "risk_label" not in second
    handle = pool._handles[second["slot_id"]]
    assert handle.external is False
    assert handle.mocked is True
    lease_obj = pool._leases[second["lease_id"]]
    assert lease_obj.external_attach is False

    rel = pool.release(second["lease_id"])
    assert rel["released"] is True
    # Even with W room, must not warm a slot that previously held attach origin
    # after the flip path (fresh mock handle is owned — warm OK for owned).
    # Critical: agent-b without ALLOW_ATTACH must not inherit user CDP.
    monkeypatch.delenv("SLIPSTREAM_ALLOW_ATTACH", raising=False)
    third = pool.lease("agent-b", "space-flip")
    assert third["tier"] == TIER_EPHEMERAL
    h3 = pool._handles[third["slot_id"]]
    assert h3.external is False
    assert "9555" not in (h3.cdp_http_url or "")


def test_adv_ac001_never_warm_external_handle_on_release(
    pool: BrowserPool, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("SLIPSTREAM_ALLOW_ATTACH", "1")
    pool.config.W = 5
    lease = pool.lease(
        "agent-a",
        "space-nowarm",
        tier="attach",
        cdp_port=9666,
    )
    # Corrupt lease flag but leave handle.external — defense in depth.
    pool._leases[lease["lease_id"]].external_attach = False
    pool._leases[lease["lease_id"]].tier = TIER_EPHEMERAL
    rel = pool.release(lease["lease_id"])
    assert rel.get("kept_warm") is False
    slot = pool._slots[lease["slot_id"]]
    assert slot.status == SlotStatus.FREE_COLD
    assert pool._handles.get(lease["slot_id"]) is None


def test_adv_ac002_ephemeral_to_attach_stops_owned_chromium(
    pool: BrowserPool, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("SLIPSTREAM_ALLOW_ATTACH", "1")
    first = pool.lease("agent-a", "space-orphan", tier="ephemeral")
    slot_id = first["slot_id"]
    owned = pool._handles[slot_id]
    assert owned.external is False
    assert owned.mocked is True

    stops: list = []
    real_stop = pool.launcher.stop

    def tracking_stop(h, *a, **k):
        stops.append(h)
        return real_stop(h, *a, **k)

    monkeypatch.setattr(pool.launcher, "stop", tracking_stop)

    second = pool.lease(
        "agent-a",
        "space-orphan",
        tier="attach",
        cdp_url="http://127.0.0.1:9777",
    )
    assert second["tier"] == TIER_ATTACH
    new_handle = pool._handles[second["slot_id"]]
    assert new_handle.external is True
    assert new_handle.cdp_http_url == "http://127.0.0.1:9777"
    # Owned tree must have been stopped (not orphaned via external no-op).
    assert any(h is owned or (not getattr(h, "external", False)) for h in stops)
    assert owned in stops
    assert pool._slots[second["slot_id"]].chromium_pid is None


def test_adv_ac003_refuse_unspecified_and_link_local(monkeypatch: pytest.MonkeyPatch):
    with pytest.raises(TierError):
        resolve_attach_cdp(cdp_url="http://0.0.0.0:9222")
    with pytest.raises(TierError):
        resolve_attach_cdp(cdp_url="http://169.254.169.254:80")
    monkeypatch.setenv("SLIPSTREAM_ATTACH_ALLOW_HOSTS", "169.254.169.254")
    with pytest.raises(TierError):
        resolve_attach_cdp(cdp_url="http://169.254.169.254:80")


def test_adv_ac003_probe_refuses_redirect(monkeypatch: pytest.MonkeyPatch):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading

    from slipstream.cdp_http import wait_attach_cdp_ready

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.end_headers()

        def log_message(self, *args):  # noqa: ARG002
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises((TimeoutError, Exception)):
            wait_attach_cdp_ready(f"http://127.0.0.1:{port}", timeout=1.5)
    finally:
        server.shutdown()


def test_adv_ac004_zero_zero_not_loopback():
    with pytest.raises(TierError):
        resolve_attach_cdp(cdp_url="http://0.0.0.0:9222")


def test_adv_ac005_list_spaces_includes_tier_only(pool: BrowserPool):
    pool.set_space_metadata("tier-only-space", tier="named")
    listed = pool.list_spaces()
    ids = {s["space_id"] for s in listed["spaces"]}
    assert "tier-only-space" in ids
    row = next(s for s in listed["spaces"] if s["space_id"] == "tier-only-space")
    assert row["tier"] == TIER_NAMED
