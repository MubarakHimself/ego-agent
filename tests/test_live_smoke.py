"""Optional live smoke — requires real Chrome; skipped unless --live / mark selected."""

from __future__ import annotations

import os
import urllib.request

import pytest

from ego_pool.config import PoolConfig
from ego_pool.launcher import find_chrome_binary
from ego_pool.pool import BrowserPool
from ego_pool.rss import sample_tree_rss


@pytest.mark.live
def test_live_chrome_lease_and_cdp(tmp_path):
    binary = find_chrome_binary()
    if not binary:
        pytest.skip("No Chrome/Chromium binary on PATH")
    if os.environ.get("EGO_POOL_MOCK") == "1":
        pytest.skip("EGO_POOL_MOCK=1 set")

    cfg = PoolConfig(
        K=1,
        W=0,
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
        # CDP HTTP should respond (may take a moment)
        url = lease["cdp_http_url"] + "/json/version"
        ok = False
        import time

        for _ in range(20):
            try:
                with urllib.request.urlopen(url, timeout=1) as resp:
                    if resp.status == 200:
                        ok = True
                        break
            except Exception:
                time.sleep(0.25)
        assert ok, f"CDP not reachable at {url}"

        rss = sample_tree_rss(pid)
        # Document hook works on live Linux
        assert rss is None or rss > 0

        pool.release(lease["lease_id"])
    finally:
        pool.shutdown()
