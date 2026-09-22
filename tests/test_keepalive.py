"""keepAlive — survive soft-idle until keep_alive_ttl; hard TTL still wins."""

from __future__ import annotations

import time

import pytest

from slipstream.config import PoolConfig
from slipstream.models import SlotStatus
from slipstream.pool import BrowserPool, LeaseExpiredError, LeaseNotFoundError


def _stale_heartbeat(pool: BrowserPool, lease_id: str, age: float) -> None:
    slot = pool._find_slot_by_lease(lease_id)
    assert slot is not None
    slot.last_heartbeat = time.time() - age


def test_keep_alive_ttl_default_and_clamp(monkeypatch: pytest.MonkeyPatch, tmp_path):
    cfg = PoolConfig()
    assert cfg.idle_ttl_seconds == 300
    assert cfg.keep_alive_ttl_seconds == 600  # 2× idle_ttl
    assert cfg.effective_keep_alive_ttl() == 600
    cfg.keep_alive_ttl_seconds = 99999
    assert cfg.effective_keep_alive_ttl() == cfg.lease_hard_ttl_seconds
    monkeypatch.setenv("SLIPSTREAM_SPACES_ROOT", str(tmp_path / "spaces"))
    monkeypatch.setenv("SLIPSTREAM_VAULT_ROOT", str(tmp_path / "vault"))
    monkeypatch.setenv("SLIPSTREAM_ARTIFACTS_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("SLIPSTREAM_KEEPALIVE_TTL", "120")
    env_cfg = PoolConfig.from_env()
    assert env_cfg.keep_alive_ttl_seconds == 120
    monkeypatch.setenv("SLIPSTREAM_KEEPALIVE_TTL", "99999")
    clamped = PoolConfig.from_env()
    assert clamped.keep_alive_ttl_seconds == clamped.lease_hard_ttl_seconds


def test_omit_without_env_is_false(monkeypatch: pytest.MonkeyPatch, pool: BrowserPool):
    """ADV-KA-002: SLIPSTREAM_KEEPALIVE must not pin omit → true."""
    monkeypatch.setenv("SLIPSTREAM_KEEPALIVE", "1")
    lease = pool.lease("agent-1", "space-omit")
    assert lease["keep_alive"] is False
    monkeypatch.delenv("SLIPSTREAM_KEEPALIVE", raising=False)
    lease2 = pool.lease("agent-2", "space-omit2")
    assert lease2["keep_alive"] is False


def test_lease_json_shows_keep_alive_false_by_default(pool: BrowserPool):
    lease = pool.lease("agent-1", "space-ka-off")
    assert lease["keep_alive"] is False
    assert pool._leases[lease["lease_id"]].keep_alive is False


def test_body_true_works_in_status_and_sessions(pool: BrowserPool):
    lease = pool.lease("agent-1", "space-ka-on", keep_alive=True)
    lid = lease["lease_id"]
    assert lease["keep_alive"] is True

    listed = pool.list_leases()
    row = next(r for r in listed["leases"] if r["lease_id"] == lid)
    assert row["keep_alive"] is True

    sessions = pool.list_sessions()
    srow = next(r for r in sessions["sessions"] if r["lease_id"] == lid)
    assert srow["keep_alive"] is True

    st = pool.status()
    assert st["keep_alive_ttl_seconds"] == pool.config.effective_keep_alive_ttl()


def test_keepalive_survives_soft_idle_before_ka_ttl(pool: BrowserPool):
    """Past idle_ttl but within keep_alive_ttl → still leased."""
    pool.config.keep_alive_ttl_seconds = 600
    lease = pool.lease("agent-1", "space-survive", keep_alive=True)
    lid = lease["lease_id"]
    _stale_heartbeat(pool, lid, pool.config.idle_ttl_seconds + 60)
    evicted = pool.evict_idle()
    assert lid not in evicted
    assert lid in pool._leases
    assert pool.status()["leased"] == 1
    slot = pool._find_slot_by_lease(lid)
    assert slot is not None
    assert slot.status == SlotStatus.LEASED
    assert pool.heartbeat(lid)["ok"] is True


def test_keepalive_expires_after_ka_ttl(pool: BrowserPool):
    """Past keep_alive_ttl since last heartbeat → tear down like soft-idle."""
    pool.config.keep_alive_ttl_seconds = 120
    lease = pool.lease("agent-1", "space-ka-expire", keep_alive=True)
    lid = lease["lease_id"]
    _stale_heartbeat(pool, lid, pool.config.effective_keep_alive_ttl() + 30)
    evicted = pool.evict_idle()
    assert lid in evicted
    assert lid not in pool._leases
    assert pool.status()["leased"] == 0
    assert pool.status()["warm"] == 0


def test_non_keepalive_released_on_simulated_disconnect(pool: BrowserPool):
    lease = pool.lease("agent-1", "space-drop", keep_alive=False)
    lid = lease["lease_id"]
    _stale_heartbeat(pool, lid, pool.config.idle_ttl_seconds + 60)
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


def test_keepalive_hard_ttl_still_wins(pool: BrowserPool):
    pool.config.keep_alive_ttl_seconds = 600
    lease = pool.lease("agent-1", "space-hard", keep_alive=True, ttl_seconds=60)
    lid = lease["lease_id"]
    # Fresh heartbeat but hard TTL expired
    pool._leases[lid].expires_at = time.time() - 1
    slot = pool._find_slot_by_lease(lid)
    assert slot is not None
    slot.last_heartbeat = time.time()
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


def test_idempotent_omit_preserves_keepalive(pool: BrowserPool):
    a = pool.lease("agent-1", "space-idem", keep_alive=True)
    b = pool.lease("agent-1", "space-idem")  # omit
    assert a["lease_id"] == b["lease_id"]
    assert b["keep_alive"] is True
    c = pool.lease("agent-1", "space-idem", keep_alive=False)
    assert c["lease_id"] == a["lease_id"]
    assert c["keep_alive"] is False


def test_cli_keep_alive_and_no_keep_alive_flags():
    from slipstream.__main__ import build_parser

    p = build_parser()
    a = p.parse_args(["lease", "--agent-id", "a", "--space-id", "s", "--keep-alive"])
    assert a.keep_alive is True
    b = p.parse_args(["lease", "--agent-id", "a", "--space-id", "s", "--no-keep-alive"])
    assert b.keep_alive is False
    c = p.parse_args(["lease", "--agent-id", "a", "--space-id", "s"])
    assert c.keep_alive is None
    with pytest.raises(SystemExit):
        p.parse_args(
            ["lease", "--agent-id", "a", "--space-id", "s", "--keep-alive", "--no-keep-alive"]
        )


def test_cli_cmd_lease_sends_explicit_keep_alive(monkeypatch):
    """CLI sends keep_alive only when --keep-alive / --no-keep-alive set."""
    from slipstream import cli as cli_mod

    seen: list[dict | None] = []

    def fake_request(method, url, body=None):
        seen.append(dict(body) if body is not None else None)
        return 200, {"lease_id": "x", "keep_alive": body.get("keep_alive", False) if body else False}

    monkeypatch.setattr(cli_mod, "_request", fake_request)
    monkeypatch.setattr(cli_mod, "_print_json", lambda *_a, **_k: None)
    assert cli_mod.cmd_lease(agent_id="a1", space_id="cli-ka", url="http://127.0.0.1:8755", keep_alive=True) == 0
    assert seen[-1].get("keep_alive") is True
    assert cli_mod.cmd_lease(
        agent_id="a2", space_id="cli-noka", url="http://127.0.0.1:8755", keep_alive=False
    ) == 0
    assert seen[-1].get("keep_alive") is False
    assert cli_mod.cmd_lease(agent_id="a3", space_id="cli-omit", url="http://127.0.0.1:8755") == 0
    assert "keep_alive" not in seen[-1]
