"""Unit tests — mocked launcher; no real browsers required."""

from __future__ import annotations

import time

import pytest

from ego_pool.config import PoolConfig
from ego_pool.models import SlotStatus
from ego_pool.pool import BrowserPool, LeaseNotFoundError, PoolFullError
from ego_pool.rss import sample_tree_rss


def test_config_defaults():
    cfg = PoolConfig()
    assert cfg.K == 5
    assert cfg.W == 1
    assert cfg.idle_ttl_seconds == 300
    assert cfg.cdp_base_port == 9222


def test_space_path_convention(mock_config):
    path = mock_config.space_path("task-42")
    assert path == mock_config.spaces_root / "task-42"
    assert path.name == "task-42"


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


def test_rss_hook_stub_none_for_missing_pid():
    assert sample_tree_rss(None) is None
    # Unlikely pid — should return None without crashing
    assert sample_tree_rss(999_999_999) is None


def test_rss_hook_self_process():
    import os

    rss = sample_tree_rss(os.getpid())
    # On Linux box this should return a positive int
    assert rss is None or (isinstance(rss, int) and rss > 0)


def test_space_dir_created_on_lease(pool: BrowserPool, mock_config):
    pool.lease("agent-1", "my-space")
    assert (mock_config.spaces_root / "my-space").is_dir()
