"""Mock OOB smoke — covers scripts/oob_smoke.py (no Chrome, no MCP)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "scripts"))
import oob_smoke  # noqa: E402  # skylos: ignore[SKY-D222] local scripts/oob_smoke.py


def test_oob_smoke_mock_lease_cycle(tmp_path, monkeypatch):
    monkeypatch.setenv("SLIPSTREAM_MOCK", "1")
    monkeypatch.delenv("SLIPSTREAM_URL", raising=False)
    monkeypatch.setenv("SLIPSTREAM_CHROME", str(tmp_path / "no-chrome-here"))

    summary = oob_smoke.run_oob_smoke(
        spaces_root=tmp_path / "spaces",
        agent_id="test-oob",
        space_id="demo-space",
        quiet=True,
    )
    assert summary["ok"] is True
    assert summary["lease_id"]
    assert summary["doctor_pre"] == 0
    assert summary["doctor_post"] == 0
    assert summary["base_url"].startswith("http://127.0.0.1:")


def test_oob_smoke_main_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SLIPSTREAM_MOCK", "1")
    monkeypatch.delenv("SLIPSTREAM_URL", raising=False)
    monkeypatch.setenv("SLIPSTREAM_CHROME", str(tmp_path / "no-chrome-here"))
    monkeypatch.setenv("SLIPSTREAM_SPACES_ROOT", str(tmp_path / "spaces-main"))

    code = oob_smoke.main(["--json"])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert data["lease_id"]
