"""Optional live smoke — requires real Chrome; skipped unless -m live."""

from __future__ import annotations

import os

import pytest

from ego_pool.cdp_http import navigate_via_json_new, wait_cdp_ready
from ego_pool.config import PoolConfig
from ego_pool.launcher import find_chrome_binary
from ego_pool.pool import BrowserPool
from ego_pool.rss import sample_tree_rss


@pytest.mark.live
def test_live_chrome_lease_navigate_heartbeat_release(tmp_path):
    binary = find_chrome_binary()
    if not binary:
        pytest.skip("No Chrome/Chromium binary on PATH")
    if os.environ.get("EGO_POOL_MOCK") == "1":
        pytest.skip("EGO_POOL_MOCK=1 set")

    cfg = PoolConfig(
        K=1,
        W=1,
        spaces_root=tmp_path / "spaces",
        cdp_base_port=19422,
        mock=False,
        headless=True,
        chrome_binary=binary,
        idle_ttl_seconds=300,
    )
    pool = BrowserPool(cfg)
    try:
        lease = pool.lease("live-agent", "live-space")
        assert lease["status"] == "leased"
        pid = None
        for s in pool.status()["slots"]:
            if s["lease_id"] == lease["lease_id"]:
                pid = s["chromium_pid"]
                break
        assert pid is not None

        version = wait_cdp_ready(lease["cdp_http_url"], timeout=20.0)
        assert "Browser" in version

        snap = navigate_via_json_new(
            lease["cdp_http_url"],
            "https://example.com",
            expect_title_substr="Example Domain",
            settle_timeout=15.0,
        )
        assert snap.get("matched"), f"navigate failed: {snap}"
        assert "Example Domain" in (snap.get("title") or "")

        rss = sample_tree_rss(pid)
        assert rss is None or rss > 0

        hb = pool.heartbeat(lease["lease_id"])
        assert hb.get("ok") is True

        pool.release(lease["lease_id"])

        # Warm reuse path (W=1): same space should come back without relaunch race
        lease2 = pool.lease("live-agent", "live-space")
        assert lease2["status"] == "leased"
        wait_cdp_ready(lease2["cdp_http_url"], timeout=5.0)
        pool.release(lease2["lease_id"])
    finally:
        pool.shutdown()
