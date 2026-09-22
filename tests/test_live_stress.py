"""Live stress suite — real Chrome required (skip only if binary missing)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from slipstream.launcher import find_chrome_binary

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "scripts"))
import live_stress  # noqa: E402


@pytest.mark.live_stress
def test_live_stress_suite():
    if os.environ.get("SLIPSTREAM_MOCK") == "1":
        pytest.fail("SLIPSTREAM_MOCK=1 must not be used for live_stress validation")
    binary = find_chrome_binary(os.environ.get("SLIPSTREAM_CHROME"))
    if not binary:
        pytest.skip("No Chrome/Chromium binary on PATH")

    report = live_stress.run_all(base_port=20522)
    failed = [c for c in report.cases if c.status == "FAIL"]
    assert not failed, "\n".join(f"{c.name}: {c.error}" for c in failed)
    assert report.ok
