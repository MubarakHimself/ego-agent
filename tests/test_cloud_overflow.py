"""Cloud overflow behind the same lease API (mock provider only)."""

from __future__ import annotations

import pytest

from slipstream.tiers import (
    CloudProviderError,
    CloudSession,
    MockCloudProvider,
    build_cloud_provider,
    cloud_overflow_always,
    cloud_overflow_enabled,
    cloud_overflow_mode,
    max_cloud_overflow,
    validate_cloud_session_cdp,
)
from slipstream.config import PoolConfig
from slipstream.pool import BrowserPool, OverflowFullError, PoolFullError


@pytest.fixture
def overflow_pool(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SLIPSTREAM_CLOUD_OVERFLOW", "1")
    monkeypatch.setenv("SLIPSTREAM_CLOUD_PROVIDER", "mock")
    provider = MockCloudProvider()
    cfg = PoolConfig(
        K=2,
        W=0,
        idle_ttl_seconds=300,
        spaces_root=tmp_path / "spaces",
        vault_root=tmp_path / "vault",
        artifacts_root=tmp_path / "artifacts",
        cdp_base_port=19222,
        mock=True,
        headless=True,
    )
    pool = BrowserPool(cfg, cloud_provider=provider)
    yield pool, provider
    pool.shutdown()


def test_cloud_overflow_env_helpers(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SLIPSTREAM_CLOUD_OVERFLOW", raising=False)
    assert cloud_overflow_mode() == ""
    assert cloud_overflow_enabled() is False
    assert cloud_overflow_always() is False
    monkeypatch.setenv("SLIPSTREAM_CLOUD_OVERFLOW", "1")
    assert cloud_overflow_enabled() is True
    assert cloud_overflow_always() is False
    monkeypatch.setenv("SLIPSTREAM_CLOUD_OVERFLOW", "always")
    assert cloud_overflow_always() is True
    assert build_cloud_provider("mock").name == "mock"
    with pytest.raises(CloudProviderError):
        build_cloud_provider("browserbase")


def test_overflow_disabled_still_503(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SLIPSTREAM_CLOUD_OVERFLOW", raising=False)
    cfg = PoolConfig(
        K=2,
        W=0,
        spaces_root=tmp_path / "spaces",
        vault_root=tmp_path / "vault",
        artifacts_root=tmp_path / "artifacts",
        cdp_base_port=19222,
        mock=True,
    )
    pool = BrowserPool(cfg, cloud_provider=MockCloudProvider())
    try:
        pool.lease("a0", "s0")
        pool.lease("a1", "s1")
        with pytest.raises(PoolFullError):
            pool.lease("a2", "s2")
        st = pool.status()
        assert st["cloud_overflow"] is False
        assert "cloud_provider" not in st
    finally:
        pool.shutdown()


def test_overflow_on_pool_full_with_mock(overflow_pool):
    pool, provider = overflow_pool
    local0 = pool.lease("a0", "s0")
    local1 = pool.lease("a1", "s1")
    assert "overflow" not in local0
    assert "overflow" not in local1

    over = pool.lease("a2", "s2")
    assert over["overflow"] is True
    assert over["provider"] == "mock"
    assert over["tier"] == "ephemeral"
    assert over["slot_id"] >= pool.config.K
    assert len(provider.created) == 1
    assert provider.created[0].session_id

    st = pool.status()
    assert st["cloud_overflow"] is True
    assert st["cloud_provider"] == "mock"
    assert st["leased"] == 3  # 2 local + 1 overflow

    listed = pool.list_leases()
    row = next(r for r in listed["leases"] if r["lease_id"] == over["lease_id"])
    assert row["overflow"] is True
    assert row["provider"] == "mock"

    sessions = pool.list_sessions()
    srow = next(r for r in sessions["sessions"] if r["lease_id"] == over["lease_id"])
    assert srow["overflow"] is True
    assert srow["provider"] == "mock"

    # Decision B: raw CDP omitted unless EXPOSE_RAW_CDP
    assert "cdp_http_url" not in over
    assert "cdp_http_url" not in row


def test_overflow_release_calls_provider(overflow_pool):
    pool, provider = overflow_pool
    pool.lease("a0", "s0")
    pool.lease("a1", "s1")
    over = pool.lease("a2", "s2")
    sid = provider.created[0].session_id
    lid = over["lease_id"]

    rel = pool.release(lid)
    assert rel["released"] is True
    assert rel["kept_warm"] is False
    assert sid in provider.released
    assert lid not in pool._leases
    # Overflow slot removed — back to local K slots only.
    assert all(s.slot_id < pool.config.K for s in pool._slots)
    assert pool.status()["leased"] == 2


def test_overflow_api_pool_full_succeeds(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """HTTP create/release/heartbeat still work for overflow leases."""
    from slipstream.api import PoolServer
    from tests.conftest import ladder_http

    monkeypatch.setenv("SLIPSTREAM_CLOUD_OVERFLOW", "1")
    monkeypatch.setenv("SLIPSTREAM_CLOUD_PROVIDER", "mock")
    monkeypatch.setenv("SLIPSTREAM_MOCK", "1")
    provider = MockCloudProvider()
    cfg = PoolConfig(
        K=1,
        W=0,
        spaces_root=tmp_path / "spaces",
        vault_root=tmp_path / "vault",
        artifacts_root=tmp_path / "artifacts",
        cdp_base_port=19322,
        mock=True,
        host="127.0.0.1",
        port=18766,
    )
    pool = BrowserPool(cfg, cloud_provider=provider)
    server = PoolServer(pool, host="127.0.0.1", port=18766)
    server.start()
    try:
        base = server.base_url
        code, local = ladder_http(
            "POST", f"{base}/v1/leases", {"agent_id": "a0", "space_id": "s0"}
        )
        assert code == 200 and "overflow" not in local
        code, over = ladder_http(
            "POST", f"{base}/v1/leases", {"agent_id": "a1", "space_id": "s1"}
        )
        assert code == 200
        assert over["overflow"] is True
        assert over["provider"] == "mock"
        lid = over["lease_id"]
        code, hb = ladder_http("POST", f"{base}/v1/leases/{lid}/heartbeat", {})
        assert code == 200 and hb["ok"] is True
        code, rel = ladder_http("DELETE", f"{base}/v1/leases/{lid}")
        assert code == 200 and rel["released"] is True
        assert provider.released
    finally:
        server.stop()
        pool.shutdown()


def test_always_overflow_skips_local(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SLIPSTREAM_CLOUD_OVERFLOW", "always")
    provider = MockCloudProvider()
    cfg = PoolConfig(
        K=5,
        W=1,
        spaces_root=tmp_path / "spaces",
        vault_root=tmp_path / "vault",
        artifacts_root=tmp_path / "artifacts",
        cdp_base_port=19422,
        mock=True,
    )
    pool = BrowserPool(cfg, cloud_provider=provider)
    try:
        lease = pool.lease("agent", "space-a", tier="named")
        assert lease["overflow"] is True
        assert lease["provider"] == "mock"
        assert lease["tier"] == "named"
        assert len(provider.created) == 1
        # Local K slots remain free_cold (no Chromium spawn).
        local_leased = [
            s for s in pool._slots if s.slot_id < pool.config.K and s.status.value == "leased"
        ]
        assert local_leased == []
    finally:
        pool.shutdown()


def test_overflow_re_lease_keeps_remote_session(overflow_pool):
    """ADV-CO-001/004: re-lease under =1 refreshes in place; no detach+rebind."""
    pool, provider = overflow_pool
    pool.lease("a0", "s0")
    pool.lease("a1", "s1")
    first = pool.lease("a2", "s2")
    assert first["overflow"] is True
    lid = first["lease_id"]
    lease_obj = pool._leases[lid]
    assert lease_obj.external_attach is False
    assert lease_obj.overflow is True
    remote_sid = lease_obj.remote_session_id
    assert remote_sid
    assert pool._handles[first["slot_id"]].external is True
    assert len(provider.created) == 1
    assert list(provider.released) == []

    second = pool.lease("a2", "s2")
    assert second["lease_id"] == lid
    assert second["overflow"] is True
    assert second["provider"] == "mock"
    again = pool._leases[lid]
    assert again.remote_session_id == remote_sid
    assert again.external_attach is False
    assert len(provider.created) == 1
    assert list(provider.released) == []
    assert pool.status()["leased"] == 3


def test_validate_cloud_session_cdp_policy():
    """ADV-CO-002: refuse file/javascript/data; allow mock loopback."""
    http, ws = validate_cloud_session_cdp(
        "http://127.0.0.1:19001", "ws://127.0.0.1:19001/devtools/browser/x"
    )
    assert http == "http://127.0.0.1:19001"
    assert ws.startswith("ws://127.0.0.1:19001")
    for bad in (
        "file:///tmp/x",
        "javascript:alert(1)",
        "data:text/html,hi",
        "http://evil.example:9222",
        "http://169.254.169.254/",
    ):
        with pytest.raises(CloudProviderError):
            validate_cloud_session_cdp(bad)
    with pytest.raises(CloudProviderError):
        validate_cloud_session_cdp(
            "http://127.0.0.1:9", "javascript:alert(1)"
        )


def test_bind_rejects_evil_provider_urls(overflow_pool):
    """ADV-CO-002: bind-time validation refuses bad provider CDP URLs."""
    pool, provider = overflow_pool
    pool.lease("a0", "s0")
    pool.lease("a1", "s1")

    def evil_create(*, agent_id, space_id, **kwargs):
        return CloudSession(
            session_id="evil-1",
            cdp_http_url="file:///etc/passwd",
            cdp_ws_url="ws://127.0.0.1:1/x",
            provider="mock",
        )

    provider.create_session = evil_create  # type: ignore[method-assign]
    with pytest.raises(CloudProviderError):
        pool.lease("a2", "s2")
    # Slot rolled back — still at local K.
    assert all(s.slot_id < pool.config.K for s in pool._slots)


def test_max_overflow_default_k_and_cap(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """ADV-CO-003: default max_overflow=K; above cap → OverflowFullError."""
    monkeypatch.setenv("SLIPSTREAM_CLOUD_OVERFLOW", "1")
    monkeypatch.delenv("SLIPSTREAM_MAX_OVERFLOW", raising=False)
    assert max_cloud_overflow(2) == 2
    provider = MockCloudProvider()
    cfg = PoolConfig(
        K=2,
        W=0,
        spaces_root=tmp_path / "spaces",
        vault_root=tmp_path / "vault",
        artifacts_root=tmp_path / "artifacts",
        cdp_base_port=19522,
        mock=True,
    )
    pool = BrowserPool(cfg, cloud_provider=provider)
    try:
        pool.lease("a0", "s0")
        pool.lease("a1", "s1")
        pool.lease("a2", "s2")  # overflow 1
        pool.lease("a3", "s3")  # overflow 2 (=K)
        st = pool.status()
        assert st["max_overflow"] == 2
        with pytest.raises(OverflowFullError):
            pool.lease("a4", "s4")
        assert len(provider.created) == 2
    finally:
        pool.shutdown()


def test_max_overflow_env_override(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SLIPSTREAM_CLOUD_OVERFLOW", "always")
    monkeypatch.setenv("SLIPSTREAM_MAX_OVERFLOW", "1")
    provider = MockCloudProvider()
    cfg = PoolConfig(
        K=5,
        W=0,
        spaces_root=tmp_path / "spaces",
        vault_root=tmp_path / "vault",
        artifacts_root=tmp_path / "artifacts",
        cdp_base_port=19622,
        mock=True,
    )
    pool = BrowserPool(cfg, cloud_provider=provider)
    try:
        pool.lease("a0", "s0")
        with pytest.raises(OverflowFullError):
            pool.lease("a1", "s1")
    finally:
        pool.shutdown()


def test_mock_created_released_rings_bounded():
    """ADV-CO-003: mock created/released rings are bounded."""
    p = MockCloudProvider()
    for i in range(MockCloudProvider._RING + 20):
        sess = p.create_session(agent_id="a", space_id=f"s{i}")
        p.release_session(sess.session_id)
    assert len(p.created) == MockCloudProvider._RING
    assert len(p.released) == MockCloudProvider._RING
