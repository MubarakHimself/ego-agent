"""Unit tests — mocked launcher; no real browsers required."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from ego_pool.config import PoolConfig
from ego_pool.models import SlotStatus
from ego_pool.pool import (
    BrowserPool,
    LeaseExpiredError,
    LeaseNotFoundError,
    PoolFullError,
    SpaceInUseError,
)
from ego_pool.rss import sample_tree_rss


def test_config_defaults():
    cfg = PoolConfig()
    assert cfg.K == 5
    assert cfg.W == 1
    assert cfg.idle_ttl_seconds == 300
    assert cfg.cdp_base_port == 9222
    assert not hasattr(cfg, "heartbeat_interval_hint_seconds")


def test_space_path_convention(mock_config):
    path = mock_config.space_path("task-42")
    assert path == mock_config.spaces_root / "task-42"
    assert path.name == "task-42"


@pytest.mark.parametrize("bad", [".", "..", "", "   "])
def test_space_id_rejects_unsafe(mock_config, bad):
    with pytest.raises(ValueError):
        mock_config.space_path(bad)
    with pytest.raises(ValueError):
        PoolConfig.normalize_space_id(bad)


def test_lease_heartbeat_release(pool: BrowserPool):
    lease = pool.lease("agent-1", "space-a")
    assert lease["status"] == "leased"
    assert lease["cdp_http_url"].startswith("http://127.0.0.1:")
    assert lease["space_id"] == "space-a"
    lid = lease["lease_id"]

    hb = pool.heartbeat(lid)
    assert hb["ok"] is True

    st = pool.status()
    assert st["K"] == 5
    assert st["W"] == 1
    assert st["leased"] == 1
    assert st["mock"] is True

    rel = pool.release(lid)
    assert rel["released"] is True
    assert pool.status()["leased"] == 0


def test_idempotent_lease_same_agent_space(pool: BrowserPool):
    a = pool.lease("agent-1", "space-a")
    b = pool.lease("agent-1", "space-a")
    assert a["lease_id"] == b["lease_id"]


def test_space_exclusivity_different_agent(pool: BrowserPool):
    pool.lease("agent-1", "space-a")
    with pytest.raises(SpaceInUseError):
        pool.lease("agent-2", "space-a")


def test_hard_k_cap(pool: BrowserPool):
    leases = []
    for i in range(5):
        leases.append(pool.lease(f"agent-{i}", f"space-{i}"))
    assert pool.status()["leased"] == 5
    with pytest.raises(PoolFullError):
        pool.lease("agent-overflow", "space-overflow")
    # release one → can lease again
    pool.release(leases[0]["lease_id"])
    pool.lease("agent-overflow", "space-overflow")


def test_warm_slot_on_release(pool: BrowserPool):
    lease = pool.lease("agent-1", "space-a")
    pool.release(lease["lease_id"])
    st = pool.status()
    assert st["warm"] == 1  # W=1
    warm_slots = [s for s in st["slots"] if s["status"] == SlotStatus.FREE_WARM.value]
    assert len(warm_slots) == 1
    # Space binding retained for warm reuse
    assert warm_slots[0]["space_id"] == "space-a"


def test_warm_reuse_same_space_no_relaunch(pool: BrowserPool):
    lease = pool.lease("agent-1", "space-a")
    pid_before = pool._find_slot_by_lease(lease["lease_id"]).chromium_pid
    port_before = pool._find_slot_by_lease(lease["lease_id"]).cdp_port
    pool.release(lease["lease_id"])
    assert pool.status()["warm"] == 1

    with patch.object(pool.launcher, "launch", wraps=pool.launcher.launch) as launch_spy:
        lease2 = pool.lease("agent-2", "space-a")
        assert launch_spy.call_count == 0

    slot = pool._find_slot_by_lease(lease2["lease_id"])
    assert slot is not None
    assert slot.chromium_pid == pid_before
    assert slot.cdp_port == port_before
    assert lease2["space_id"] == "space-a"
    assert lease2["lease_id"] != lease["lease_id"]


def test_warm_different_space_relaunches(pool: BrowserPool):
    lease = pool.lease("agent-1", "space-a")
    pool.release(lease["lease_id"])
    with patch.object(pool.launcher, "launch", wraps=pool.launcher.launch) as launch_spy:
        lease2 = pool.lease("agent-2", "space-b")
        assert launch_spy.call_count == 1
    assert lease2["space_id"] == "space-b"


def test_second_release_goes_cold_when_warm_full(pool: BrowserPool):
    l1 = pool.lease("a1", "s1")
    l2 = pool.lease("a2", "s2")
    pool.release(l1["lease_id"])
    assert pool.status()["warm"] == 1
    pool.release(l2["lease_id"])
    # Still only W=1 warm; second becomes cold
    assert pool.status()["warm"] == 1
    cold = [s for s in pool.status()["slots"] if s["status"] == SlotStatus.FREE_COLD.value]
    assert len(cold) >= 1


def test_heartbeat_unknown_lease(pool: BrowserPool):
    with pytest.raises(LeaseNotFoundError):
        pool.heartbeat("does-not-exist")


def test_idle_evict(pool: BrowserPool):
    lease = pool.lease("agent-1", "space-a")
    lid = lease["lease_id"]
    # Force stale heartbeat
    slot = pool._find_slot_by_lease(lid)
    assert slot is not None
    slot.last_heartbeat = time.time() - (pool.config.idle_ttl_seconds + 10)
    evicted = pool.evict_idle()
    assert lid in evicted
    assert pool.status()["leased"] == 0


def test_evict_skips_freshly_heartbeated_lease(pool: BrowserPool):
    """Race: stale snapshot must not kill a lease heartbeated before teardown."""
    lease = pool.lease("agent-1", "space-a")
    lid = lease["lease_id"]
    slot = pool._find_slot_by_lease(lid)
    assert slot is not None
    # Make it look stale, then heartbeat (as a concurrent agent would)
    slot.last_heartbeat = time.time() - (pool.config.idle_ttl_seconds + 10)
    pool.heartbeat(lid)
    # Eviction at "now" should see the fresh heartbeat and skip
    evicted = pool.evict_idle(now=time.time())
    assert lid not in evicted
    assert pool.status()["leased"] == 1
    # Still heartbeatable
    assert pool.heartbeat(lid)["ok"] is True


def test_hard_ttl_expires_on_heartbeat(pool: BrowserPool):
    lease = pool.lease("agent-1", "space-a", ttl_seconds=1)
    lid = lease["lease_id"]
    # Force expires_at into the past
    pool._leases[lid].expires_at = time.time() - 1
    with pytest.raises(LeaseExpiredError):
        pool.heartbeat(lid)
    assert pool.status()["leased"] == 0
    with pytest.raises(LeaseNotFoundError):
        pool.heartbeat(lid)


def test_hard_ttl_expires_via_evict_idle(pool: BrowserPool):
    lease = pool.lease("agent-1", "space-a", ttl_seconds=60)
    lid = lease["lease_id"]
    pool._leases[lid].expires_at = time.time() - 1
    # Keep heartbeat fresh so only hard TTL triggers
    slot = pool._find_slot_by_lease(lid)
    slot.last_heartbeat = time.time()
    evicted = pool.evict_idle()
    assert lid in evicted
    assert pool.status()["leased"] == 0


def test_launch_failure_frees_slot(pool: BrowserPool):
    with patch.object(pool.launcher, "launch", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError, match="boom"):
            pool.lease("agent-1", "space-a")
    st = pool.status()
    assert st["leased"] == 0
    starting = [s for s in st["slots"] if s["status"] == SlotStatus.STARTING.value]
    assert starting == []
    cold = [s for s in st["slots"] if s["status"] == SlotStatus.FREE_COLD.value]
    assert len(cold) == pool.config.K
    # Slot is reusable after failure
    lease = pool.lease("agent-1", "space-a")
    assert lease["status"] == "leased"


def test_rss_hook_stub_none_for_missing_pid():
    assert sample_tree_rss(None) is None
    # Unlikely pid — should return None without crashing
    assert sample_tree_rss(999_999_999) is None


def test_rss_hook_self_process():
    import os

    rss = sample_tree_rss(os.getpid())
    # On Linux box this should return a positive int
    assert rss is None or (isinstance(rss, int) and rss > 0)


def test_sample_tree_rss_mb_removed():
    import ego_pool.rss as rss_mod

    assert not hasattr(rss_mod, "sample_tree_rss_mb")


def test_slot_status_no_evicting_dead():
    assert not hasattr(SlotStatus, "EVICTING")
    assert not hasattr(SlotStatus, "DEAD")


def test_space_dir_created_on_lease(pool: BrowserPool, mock_config):
    pool.lease("agent-1", "my-space")
    assert (mock_config.spaces_root / "my-space").is_dir()
