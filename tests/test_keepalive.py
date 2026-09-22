"""keepAlive — survive soft-idle / driver disconnect (Browserbase pattern)."""

from __future__ import annotations

import time

import pytest

from slipstream.config import keep_alive_default
from slipstream.models import SlotStatus
from slipstream.pool import BrowserPool, LeaseExpiredError, LeaseNotFoundError


def _stale_heartbeat(pool: BrowserPool, lease_id: str) -> None:
    slot = pool._find_slot_by_lease(lease_id)
    assert slot is not None
    slot.last_heartbeat = time.time() - (pool.config.idle_ttl_seconds + 60)


def test_keep_alive_default_env_off(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SLIPSTREAM_KEEPALIVE", raising=False)
    assert keep_alive_default() is False
    monkeypatch.setenv("SLIPSTREAM_KEEPALIVE", "1")
    assert keep_alive_default() is True
    monkeypatch.setenv("SLIPSTREAM_KEEPALIVE", "true")
    assert keep_alive_default() is True
    monkeypatch.setenv("SLIPSTREAM_KEEPALIVE", "0")
    assert keep_alive_default() is False


def test_lease_json_shows_keep_alive_false_by_default(pool: BrowserPool):
    lease = pool.lease("agent-1", "space-ka-off")
    assert lease["keep_alive"] is False
    assert pool._leases[lease["lease_id"]].keep_alive is False


def test_lease_keep_alive_true_in_status_and_sessions(pool: BrowserPool):
    lease = pool.lease("agent-1", "space-ka-on", keep_alive=True)
    lid = lease["lease_id"]
    assert lease["keep_alive"] is True

    listed = pool.list_leases()
    row = next(r for r in listed["leases"] if r["lease_id"] == lid)
    assert row["keep_alive"] is True

    sessions = pool.list_sessions()
    srow = next(r for r in sessions["sessions"] if r["lease_id"] == lid)
    assert srow["keep_alive"] is True


def test_keepalive_survives_simulated_disconnect(pool: BrowserPool):
    """keep_alive lease: soft-idle past idle_ttl (driver gone) → still leased."""
    lease = pool.lease("agent-1", "space-survive", keep_alive=True)
    lid = lease["lease_id"]
    _stale_heartbeat(pool, lid)
    evicted = pool.evict_idle()
    assert lid not in evicted
    assert lid in pool._leases
    assert pool.status()["leased"] == 1
    # Chromium handle still present (mock)
    slot = pool._find_slot_by_lease(lid)
    assert slot is not None
    assert slot.status == SlotStatus.LEASED
    assert pool.heartbeat(lid)["ok"] is True


def test_non_keepalive_released_on_simulated_disconnect(pool: BrowserPool):
    lease = pool.lease("agent-1", "space-drop", keep_alive=False)
    lid = lease["lease_id"]
    _stale_heartbeat(pool, lid)
    evicted = pool.evict_idle()
    assert lid in evicted
    assert lid not in pool._leases
    assert pool.status()["leased"] == 0
    assert pool.status()["warm"] == 0


def test_keepalive_explicit_release_still_works(pool: BrowserPool):
    lease = pool.lease("agent-1", "space-rel", keep_alive=True)
    lid = lease["lease_id"]
    rel = pool.release(lid, reason="done")
    assert rel["released"] is True
    assert lid not in pool._leases
    with pytest.raises(LeaseNotFoundError):
        pool.heartbeat(lid)


def test_keepalive_hard_ttl_still_evicts(pool: BrowserPool):
    lease = pool.lease("agent-1", "space-hard", keep_alive=True, ttl_seconds=60)
    lid = lease["lease_id"]
    pool._leases[lid].expires_at = time.time() - 1
    evicted = pool.evict_idle()
    assert lid in evicted
    assert lid not in pool._leases
    assert pool.status()["warm"] == 0


def test_keepalive_hard_ttl_on_heartbeat(pool: BrowserPool):
    lease = pool.lease("agent-1", "space-hb-hard", keep_alive=True, ttl_seconds=60)
    lid = lease["lease_id"]
    pool._leases[lid].expires_at = time.time() - 1
    with pytest.raises(LeaseExpiredError):
        pool.heartbeat(lid)
    assert lid not in pool._leases


def test_env_default_keepalive(monkeypatch: pytest.MonkeyPatch, pool: BrowserPool):
    monkeypatch.setenv("SLIPSTREAM_KEEPALIVE", "1")
    lease = pool.lease("agent-env", "space-env")
    assert lease["keep_alive"] is True
    lid = lease["lease_id"]
    _stale_heartbeat(pool, lid)
    assert lid not in pool.evict_idle()
    # Explicit false overrides env
    lease2 = pool.lease("agent-env2", "space-env2", keep_alive=False)
    assert lease2["keep_alive"] is False


def test_idempotent_omit_preserves_keepalive(pool: BrowserPool):
    a = pool.lease("agent-1", "space-idem", keep_alive=True)
    b = pool.lease("agent-1", "space-idem")  # omit
    assert a["lease_id"] == b["lease_id"]
    assert b["keep_alive"] is True
    c = pool.lease("agent-1", "space-idem", keep_alive=False)
    assert c["lease_id"] == a["lease_id"]
    assert c["keep_alive"] is False
