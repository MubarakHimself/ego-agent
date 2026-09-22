"""Unit tests — mocked launcher; no real browsers required."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from slipstream.config import PoolConfig
from slipstream.launcher import LaunchHandle
from slipstream.models import SlotStatus
from slipstream.pool import (
    BrowserPool,
    LeaseExpiredError,
    LeaseNotFoundError,
    PoolFullError,
    SpaceInUseError,
)
from slipstream.rss import sample_tree_rss


def test_config_defaults():
    cfg = PoolConfig()
    assert cfg.K == 5
    assert cfg.W == 1
    assert cfg.idle_ttl_seconds == 300
    assert cfg.keep_alive_ttl_seconds == 600
    assert cfg.cdp_base_port == 9222
    assert not hasattr(cfg, "heartbeat_interval_hint_seconds")


def test_space_path_convention(mock_config):
    path = mock_config.space_path("task-42")
    assert path == mock_config.spaces_root / "task-42"
    assert path.name == "task-42"


@pytest.mark.parametrize("bad", [".", "..", "", "   ", "a/b", "a\\b", "foo..bar", "x\0y"])
def test_space_id_rejects_unsafe(mock_config, bad):
    with pytest.raises(ValueError):
        mock_config.space_path(bad)
    with pytest.raises(ValueError):
        PoolConfig.normalize_space_id(bad)


def test_space_id_slash_rejected_not_collapsed(mock_config):
    """Path separators must 400 — never rewritten into '_' (would collapse ids)."""
    with pytest.raises(ValueError, match="unsafe"):
        PoolConfig.normalize_space_id("a/b")
    # Distinct underscore form remains distinct and valid
    assert PoolConfig.normalize_space_id("a_b") == "a_b"
    assert mock_config.space_path("a_b") == mock_config.spaces_root / "a_b"


def test_lease_heartbeat_release(pool: BrowserPool):
    lease = pool.lease("agent-1", "space-a")
    assert lease["status"] == "leased"
    # Firstmate B: raw CDP URLs omitted from lease JSON by default
    assert "cdp_http_url" not in lease
    assert "cdp_ws_url" not in lease
    assert lease["space_id"] == "space-a"
    lid = lease["lease_id"]
    # Internal handle still has CDP for pool-side inject
    assert pool._leases[lid].cdp_http_url.startswith("http://127.0.0.1:")

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


def test_idempotent_release_after_hard_ttl_gets_fresh(pool: BrowserPool):
    """Expired (agent_id, space_id) hit must release then issue a new lease."""
    a = pool.lease("agent-1", "space-a", ttl_seconds=60)
    old_id = a["lease_id"]
    pool._leases[old_id].expires_at = time.time() - 1
    b = pool.lease("agent-1", "space-a")
    assert b["lease_id"] != old_id
    assert b["status"] == "leased"
    assert old_id not in pool._leases
    assert pool.status()["leased"] == 1
    # Fresh lease is heartbeatable
    assert pool.heartbeat(b["lease_id"])["ok"] is True


def test_ttl_seconds_clamped_to_hard_ceiling(pool: BrowserPool):
    """Client ttl above lease_hard_ttl_seconds is clamped; shorter still allowed."""
    ceiling = pool.config.lease_hard_ttl_seconds
    before = time.time()
    over = pool.lease("agent-1", "space-a", ttl_seconds=ceiling + 10_000)
    after = time.time()
    assert over["expires_at"] <= after + ceiling + 0.5
    assert over["expires_at"] >= before + ceiling - 0.5
    pool.release(over["lease_id"])

    before = time.time()
    short = pool.lease("agent-1", "space-b", ttl_seconds=60)
    after = time.time()
    assert short["expires_at"] <= after + 60 + 0.5
    assert short["expires_at"] >= before + 60 - 0.5


def test_idempotent_dead_leased_handle_cold_starts(pool: BrowserPool):
    """Same (agent_id, space_id) LEASED hit with a dead handle → release + cold start."""
    a = pool.lease("agent-1", "space-a")
    old_id = a["lease_id"]
    slot = pool._find_slot_by_lease(old_id)
    assert slot is not None
    dead_proc = MagicMock()
    dead_proc.poll.return_value = 1  # exited
    pool._handles[slot.slot_id] = LaunchHandle(
        pid=slot.chromium_pid or 99999,
        cdp_port=slot.cdp_port or 19222,
        cdp_http_url=slot.cdp_http_url or "http://127.0.0.1:19222",
        cdp_ws_url=slot.cdp_ws_url,
        user_data_dir=pool.config.spaces_root / "space-a",
        process=dead_proc,
        mocked=False,
    )

    with patch.object(pool.launcher, "launch", wraps=pool.launcher.launch) as launch_spy:
        b = pool.lease("agent-1", "space-a")
        assert launch_spy.call_count == 1

    assert b["lease_id"] != old_id
    assert old_id not in pool._leases
    assert b["status"] == "leased"
    assert pool.status()["leased"] == 1
    assert pool.heartbeat(b["lease_id"])["ok"] is True


def test_stop_final_wait_timeout_is_best_effort(mock_config):
    """TimeoutExpired on final wait must not raise — release can clear lease state."""
    from slipstream.launcher import ChromiumLauncher

    launcher = ChromiumLauncher(mock_config)
    proc = MagicMock()
    proc.poll.return_value = None  # still running
    proc.pid = 12345
    # First wait (grace) times out → SIGKILL path; final wait also times out
    proc.wait.side_effect = [
        __import__("subprocess").TimeoutExpired(cmd="chrome", timeout=2),
        __import__("subprocess").TimeoutExpired(cmd="chrome", timeout=2),
    ]
    handle = LaunchHandle(
        pid=12345,
        cdp_port=9222,
        cdp_http_url="http://127.0.0.1:9222",
        cdp_ws_url=None,
        user_data_dir=mock_config.spaces_root / "x",
        process=proc,
        mocked=False,
    )
    with patch("slipstream.launcher.os.killpg"):
        launcher.stop(handle, grace_seconds=0.01)  # must not raise
    assert proc.wait.call_count == 2


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


def test_warm_slot_on_client_release(pool: BrowserPool):
    lease = pool.lease("agent-1", "space-a")
    rel = pool.release(lease["lease_id"])
    assert rel["kept_warm"] is True
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


def test_dead_warm_same_space_relaunches(pool: BrowserPool):
    """Matching FREE_WARM with a dead process must stop + cold start."""
    lease = pool.lease("agent-1", "space-a")
    pool.release(lease["lease_id"])
    assert pool.status()["warm"] == 1
    warm = next(s for s in pool._slots if s.status == SlotStatus.FREE_WARM)
    # Simulate a non-mocked dead handle (poll returns exit code)
    dead_proc = MagicMock()
    dead_proc.poll.return_value = 1  # exited
    pool._handles[warm.slot_id] = LaunchHandle(
        pid=warm.chromium_pid or 99999,
        cdp_port=warm.cdp_port or 19222,
        cdp_http_url=warm.cdp_http_url or "http://127.0.0.1:19222",
        cdp_ws_url=warm.cdp_ws_url,
        user_data_dir=pool.config.spaces_root / "space-a",
        process=dead_proc,
        mocked=False,
    )

    with patch.object(pool.launcher, "launch", wraps=pool.launcher.launch) as launch_spy:
        lease2 = pool.lease("agent-2", "space-a")
        assert launch_spy.call_count == 1

    assert lease2["space_id"] == "space-a"
    assert lease2["lease_id"] != lease["lease_id"]
    slot = pool._find_slot_by_lease(lease2["lease_id"])
    assert slot is not None
    assert slot.status == SlotStatus.LEASED


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


def test_idle_evict_stops_chromium_no_warm(pool: BrowserPool):
    lease = pool.lease("agent-1", "space-a")
    lid = lease["lease_id"]
    # Force stale heartbeat
    slot = pool._find_slot_by_lease(lid)
    assert slot is not None
    slot.last_heartbeat = time.time() - (pool.config.idle_ttl_seconds + 10)
    evicted = pool.evict_idle()
    assert lid in evicted
    assert pool.status()["leased"] == 0
    assert pool.status()["warm"] == 0
    cold = [s for s in pool.status()["slots"] if s["status"] == SlotStatus.FREE_COLD.value]
    assert len(cold) == pool.config.K


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


def test_hard_ttl_expires_on_heartbeat_stops_no_warm(pool: BrowserPool):
    lease = pool.lease("agent-1", "space-a", ttl_seconds=1)
    lid = lease["lease_id"]
    # Force expires_at into the past
    pool._leases[lid].expires_at = time.time() - 1
    with pytest.raises(LeaseExpiredError):
        pool.heartbeat(lid)
    assert pool.status()["leased"] == 0
    assert pool.status()["warm"] == 0
    with pytest.raises(LeaseNotFoundError):
        pool.heartbeat(lid)


def test_hard_ttl_expires_via_evict_idle_stops_no_warm(pool: BrowserPool):
    lease = pool.lease("agent-1", "space-a", ttl_seconds=60)
    lid = lease["lease_id"]
    pool._leases[lid].expires_at = time.time() - 1
    # Keep heartbeat fresh so only hard TTL triggers
    slot = pool._find_slot_by_lease(lid)
    slot.last_heartbeat = time.time()
    evicted = pool.evict_idle()
    assert lid in evicted
    assert pool.status()["leased"] == 0
    assert pool.status()["warm"] == 0


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
    import slipstream.rss as rss_mod

    assert not hasattr(rss_mod, "sample_tree_rss_mb")


def test_slot_status_no_evicting_dead_releasing():
    assert not hasattr(SlotStatus, "EVICTING")
    assert not hasattr(SlotStatus, "DEAD")
    assert not hasattr(SlotStatus, "RELEASING")


def test_space_dir_created_on_lease(pool: BrowserPool, mock_config):
    pool.lease("agent-1", "my-space")
    assert (mock_config.spaces_root / "my-space").is_dir()


# --- concurrent lock-drop (RLock must not span Chromium IO) -----------------


def test_heartbeat_during_slow_launch_does_not_block(pool: BrowserPool):
    """A slow launch must not hold the pool lock across launcher.launch.

    Concurrent heartbeat on an existing lease must succeed before the slow
    launch finishes (proves lock is dropped during launch IO).
    """
    import threading

    existing = pool.lease("agent-hold", "space-hold")
    lid = existing["lease_id"]

    launch_entered = threading.Event()
    release_launch = threading.Event()
    heartbeat_done = threading.Event()
    heartbeat_result: dict = {}
    errors: list[BaseException] = []

    real_launch = pool.launcher.launch

    def slow_launch(space_id: str, cdp_port: int):
        launch_entered.set()
        if not release_launch.wait(timeout=5.0):
            raise TimeoutError("release_launch never set — heartbeat blocked?")
        return real_launch(space_id, cdp_port)

    pool.launcher.launch = slow_launch  # type: ignore[method-assign]

    def lease_worker():
        try:
            pool.lease("agent-slow", "space-slow")
        except BaseException as exc:  # noqa: BLE001 — collect for main thread
            errors.append(exc)

    def heartbeat_worker():
        try:
            assert launch_entered.wait(timeout=5.0), "slow launch never entered"
            t0 = time.perf_counter()
            heartbeat_result.update(pool.heartbeat(lid))
            heartbeat_result["elapsed"] = time.perf_counter() - t0
            heartbeat_done.set()
            release_launch.set()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
            release_launch.set()
            heartbeat_done.set()

    try:
        t_lease = threading.Thread(target=lease_worker, name="slow-lease")
        t_hb = threading.Thread(target=heartbeat_worker, name="heartbeat")
        t_lease.start()
        t_hb.start()
        t_hb.join(timeout=10.0)
        t_lease.join(timeout=10.0)
    finally:
        pool.launcher.launch = real_launch  # type: ignore[method-assign]

    assert not errors, f"worker errors: {errors}"
    assert heartbeat_done.is_set()
    assert heartbeat_result.get("ok") is True
    # Heartbeat must not wait for the slow launch.
    assert heartbeat_result.get("elapsed", 99) < 1.0
    assert pool.status()["leased"] == 2


def test_concurrent_lease_different_spaces_while_slow_launch(pool: BrowserPool):
    """Second lease on a different space proceeds while another launch is slow."""
    import threading

    launch_entered = threading.Event()
    release_launch = threading.Event()
    second_done = threading.Event()
    second_result: dict = {}
    errors: list[BaseException] = []
    call_lock = threading.Lock()
    launch_calls = {"n": 0}

    real_launch = pool.launcher.launch

    def gated_launch(space_id: str, cdp_port: int):
        with call_lock:
            launch_calls["n"] += 1
            n = launch_calls["n"]
        if n == 1:
            launch_entered.set()
            if not release_launch.wait(timeout=5.0):
                raise TimeoutError("release_launch never set")
        return real_launch(space_id, cdp_port)

    pool.launcher.launch = gated_launch  # type: ignore[method-assign]

    def slow_worker():
        try:
            pool.lease("agent-a", "space-a")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def fast_worker():
        try:
            assert launch_entered.wait(timeout=5.0), "slow launch never entered"
            t0 = time.perf_counter()
            second_result.update(pool.lease("agent-b", "space-b"))
            second_result["elapsed"] = time.perf_counter() - t0
            second_done.set()
            release_launch.set()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
            release_launch.set()
            second_done.set()

    try:
        t1 = threading.Thread(target=slow_worker, name="slow-lease")
        t2 = threading.Thread(target=fast_worker, name="fast-lease")
        t1.start()
        t2.start()
        t2.join(timeout=10.0)
        t1.join(timeout=10.0)
    finally:
        pool.launcher.launch = real_launch  # type: ignore[method-assign]

    assert not errors, f"worker errors: {errors}"
    assert second_done.is_set()
    assert second_result.get("status") == "leased"
    assert second_result.get("space_id") == "space-b"
    assert second_result.get("elapsed", 99) < 1.0
    assert pool.status()["leased"] == 2


def test_status_readable_during_slow_launch(pool: BrowserPool):
    """status() must not block behind a slow launcher.launch."""
    import threading

    launch_entered = threading.Event()
    release_launch = threading.Event()
    status_result: dict = {}
    errors: list[BaseException] = []

    real_launch = pool.launcher.launch

    def slow_launch(space_id: str, cdp_port: int):
        launch_entered.set()
        if not release_launch.wait(timeout=5.0):
            raise TimeoutError("release_launch never set")
        return real_launch(space_id, cdp_port)

    pool.launcher.launch = slow_launch  # type: ignore[method-assign]

    def lease_worker():
        try:
            pool.lease("agent-1", "space-a")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def status_worker():
        try:
            assert launch_entered.wait(timeout=5.0)
            t0 = time.perf_counter()
            st = pool.status()
            status_result["elapsed"] = time.perf_counter() - t0
            status_result["starting"] = sum(
                1 for s in st["slots"] if s["status"] == SlotStatus.STARTING.value
            )
            status_result["ok"] = True
            release_launch.set()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
            release_launch.set()

    try:
        t1 = threading.Thread(target=lease_worker)
        t2 = threading.Thread(target=status_worker)
        t1.start()
        t2.start()
        t2.join(timeout=10.0)
        t1.join(timeout=10.0)
    finally:
        pool.launcher.launch = real_launch  # type: ignore[method-assign]

    assert not errors, f"worker errors: {errors}"
    assert status_result.get("ok") is True
    assert status_result.get("elapsed", 99) < 1.0
    assert status_result.get("starting") == 1

def test_awaiting_human_skips_soft_idle_keeps_chromium(pool: BrowserPool):
    """need_human then idle past idle_ttl without heartbeat → lease kept."""
    import time

    lease = pool.lease("agent-human", "await-space")
    lid = lease["lease_id"]
    env = pool.raise_alert(
        lid, {"event": "need_human", "reason": "captcha", "detail": "challenge"}
    )
    assert env["harness"]["lease_kept"] is True
    assert pool._leases[lid].status == "awaiting_human"

    slot = pool._find_slot_by_lease(lid)
    assert slot is not None
    # Age heartbeat past soft idle (as if agent stopped heartbeating)
    slot.last_heartbeat = time.time() - (pool.config.idle_ttl_seconds + 60)
    evicted = pool.evict_idle()
    assert lid not in evicted
    assert lid in pool._leases
    assert pool.status()["leased"] == 1
    assert pool._handle_alive(slot)


def test_awaiting_human_still_hard_ttl_evicts(pool: BrowserPool):
    """Hard TTL still tears down even while awaiting_human."""
    import time

    lease = pool.lease("agent-hard", "await-hard-space", ttl_seconds=30)
    lid = lease["lease_id"]
    pool.raise_alert(lid, {"event": "need_human", "reason": "login"})
    assert pool._leases[lid].status == "awaiting_human"
    # Force hard expiry
    pool._leases[lid].expires_at = time.time() - 1
    evicted = pool.evict_idle()
    assert lid in evicted
    assert lid not in pool._leases
    assert pool.status()["leased"] == 0

