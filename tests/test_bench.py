"""Optional bench marker — thin wrapper around scripts/bench_pool.py."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "scripts" / "bench_pool.py"


@pytest.mark.bench
def test_bench_mock_timings(tmp_path):
    out = tmp_path / "bench.json"
    env = os.environ.copy()
    env["SLIPSTREAM_MOCK"] = "1"
    proc = subprocess.run(
        [sys.executable, str(BENCH), "--mode", "MOCK", "--out", str(out)],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["ok"] is True
    mock = next(r for r in data["results"] if r["mode"] == "MOCK")
    t = mock["timings_ms"]
    assert t["cold_lease_ms"] is not None
    assert t["warm_reuse_lease_ms"] is not None
    assert t["heartbeat_ms"] is not None
    assert t["release_ms"] is not None


@pytest.mark.bench
def test_bench_live_timings(tmp_path):
    if os.environ.get("SLIPSTREAM_MOCK") == "1":
        pytest.skip("SLIPSTREAM_MOCK=1 set")
    out = tmp_path / "bench-live.json"
    env = {k: v for k, v in os.environ.items() if k != "SLIPSTREAM_MOCK"}
    proc = subprocess.run(
        [sys.executable, str(BENCH), "--mode", "LIVE", "--out", str(out)],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["ok"] is True
    live = next(r for r in data["results"] if r["mode"] == "LIVE")
    t = live["timings_ms"]
    assert t["cold_lease_ms"] is not None and t["cold_lease_ms"] > 0
    assert t["cdp_ready_ms"] is not None
    assert t["navigate_ms"] is not None
    assert t["warm_reuse_lease_ms"] is not None
