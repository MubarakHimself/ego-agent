"""Doctor command tests — mock subprocess / HTTP where needed."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from slipstream import __main__ as mainmod
from slipstream.api import PoolServer
from slipstream.config import PoolConfig
from slipstream.doctor import (
    find_watch_skill,
    watch_compose_status,
    CheckResult,
    DoctorReport,
    cmd_doctor,
    find_skill_path,
    format_human,
    run_doctor,
)
from slipstream.pool import BrowserPool


@pytest.fixture
def api_server(tmp_path):
    cfg = PoolConfig(
        K=5,
        W=1,
        spaces_root=tmp_path / "spaces",
        cdp_base_port=19432,
        mock=True,
        host="127.0.0.1",
        port=18766,
    )
    pool = BrowserPool(cfg)
    server = PoolServer(pool, port=18766)
    server.start(background=True)
    yield server
    server.stop()


def _run_main(argv: list[str]) -> tuple[int, str, str]:
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mainmod.main(argv)
    return code, out.getvalue(), err.getvalue()


def test_find_skill_path_in_repo():
    path = find_skill_path()
    assert path is not None
    assert path.name == "SKILL.md"
    assert "slipstream" in str(path)
    text = path.read_text(encoding="utf-8")
    assert "name: slipstream" in text
    assert "slipstream-browser" in text


def test_doctor_with_pool_up_mocked_chrome(api_server, tmp_path, monkeypatch):
    monkeypatch.delenv("SLIPSTREAM_MOCK", raising=False)
    monkeypatch.setenv("SLIPSTREAM_SPACES_ROOT", str(tmp_path / "spaces-doc"))
    fake_bin = tmp_path / "fake-chrome"
    fake_bin.write_text("#!/bin/sh\nexit 0\n")
    fake_bin.chmod(0o755)
    monkeypatch.setenv("SLIPSTREAM_CHROME", str(fake_bin))

    ok_cdp = CheckResult(
        name="cdp_probe",
        status="ok",
        message="CDP ready (mock)",
        detail={"browser": "mock"},
    )
    with patch("slipstream.doctor._probe_cdp_with_chrome", return_value=ok_cdp):
        report = run_doctor(url=api_server.base_url)

    names = {c.name: c for c in report.checks}
    assert names["chrome"].status == "ok"
    assert names["cdp_probe"].status == "ok"
    assert names["pool_healthz"].status == "ok"
    assert names["spaces_root"].status == "ok"
    assert names["skill_path"].status == "ok"
    assert names["mock"].status == "ok"
    assert report.ok is True


def test_doctor_pool_down_is_warn(tmp_path, monkeypatch):
    monkeypatch.delenv("SLIPSTREAM_MOCK", raising=False)
    monkeypatch.setenv("SLIPSTREAM_SPACES_ROOT", str(tmp_path / "spaces-doc2"))
    fake_bin = tmp_path / "fake-chrome"
    fake_bin.write_text("#!/bin/sh\nexit 0\n")
    fake_bin.chmod(0o755)
    monkeypatch.setenv("SLIPSTREAM_CHROME", str(fake_bin))

    ok_cdp = CheckResult(
        name="cdp_probe",
        status="ok",
        message="CDP ready",
        detail={},
    )
    with patch("slipstream.doctor._probe_cdp_with_chrome", return_value=ok_cdp):
        report = run_doctor(url="http://127.0.0.1:1")

    names = {c.name: c for c in report.checks}
    assert names["pool_healthz"].status == "warn"
    assert report.ok is True  # warn does not fail


def test_doctor_missing_chrome_fails(tmp_path, monkeypatch):
    monkeypatch.delenv("SLIPSTREAM_MOCK", raising=False)
    monkeypatch.setenv("SLIPSTREAM_SPACES_ROOT", str(tmp_path / "spaces-doc3"))
    monkeypatch.setenv("SLIPSTREAM_CHROME", str(tmp_path / "missing-chrome-bin"))
    with patch("slipstream.doctor.find_chrome_binary", return_value=None):
        report = run_doctor(url="http://127.0.0.1:1")
    names = {c.name: c for c in report.checks}
    assert names["chrome"].status == "fail"
    assert names["cdp_probe"].status == "skip"
    assert report.ok is False


def test_doctor_mock_warn(tmp_path, monkeypatch):
    monkeypatch.setenv("SLIPSTREAM_MOCK", "1")
    monkeypatch.setenv("SLIPSTREAM_SPACES_ROOT", str(tmp_path / "spaces-doc4"))
    with patch("slipstream.doctor.find_chrome_binary", return_value="/usr/bin/google-chrome"):
        report = run_doctor(url="http://127.0.0.1:1")
    names = {c.name: c for c in report.checks}
    assert names["mock"].status == "warn"
    assert names["cdp_probe"].status == "skip"


def test_cmd_doctor_json(api_server, tmp_path, monkeypatch):
    monkeypatch.delenv("SLIPSTREAM_MOCK", raising=False)
    monkeypatch.setenv("SLIPSTREAM_SPACES_ROOT", str(tmp_path / "spaces-doc5"))
    fake_bin = tmp_path / "fake-chrome"
    fake_bin.write_text("#!/bin/sh\nexit 0\n")
    fake_bin.chmod(0o755)
    monkeypatch.setenv("SLIPSTREAM_CHROME", str(fake_bin))
    ok_cdp = CheckResult(name="cdp_probe", status="ok", message="ok", detail={})
    out = io.StringIO()
    with patch("slipstream.doctor._probe_cdp_with_chrome", return_value=ok_cdp):
        with redirect_stdout(out):
            code = cmd_doctor(url=api_server.base_url, as_json=True)
    assert code == 0
    payload = json.loads(out.getvalue())
    assert payload["ok"] is True
    assert "checks" in payload
    assert payload["version"]


def test_main_doctor_subcommand(api_server, tmp_path, monkeypatch):
    monkeypatch.delenv("SLIPSTREAM_MOCK", raising=False)
    monkeypatch.setenv("SLIPSTREAM_SPACES_ROOT", str(tmp_path / "spaces-doc6"))
    fake_bin = tmp_path / "fake-chrome"
    fake_bin.write_text("#!/bin/sh\nexit 0\n")
    fake_bin.chmod(0o755)
    monkeypatch.setenv("SLIPSTREAM_CHROME", str(fake_bin))
    ok_cdp = CheckResult(name="cdp_probe", status="ok", message="ok", detail={})
    with patch("slipstream.doctor._probe_cdp_with_chrome", return_value=ok_cdp):
        code, out, err = _run_main(
            ["doctor", "--url", api_server.base_url, "--json"]
        )
    assert code == 0, err
    assert json.loads(out)["ok"] is True


def test_build_parser_includes_doctor():
    parser = mainmod.build_parser()
    choice = None
    for action in parser._actions:
        if getattr(action, "dest", None) == "command":
            choice = action.choices
            break
    assert choice is not None
    assert "doctor" in choice


def test_format_human_contains_marks():
    report = DoctorReport(
        ok=True,
        version="0.1.0",
        mock=False,
        base_url="http://127.0.0.1:8755",
        checks=[
            CheckResult(name="chrome", status="ok", message="found"),
            CheckResult(name="pool_healthz", status="warn", message="down"),
        ],
    )
    text = format_human(report)
    assert "[OK" in text
    assert "[WARN" in text
    assert "PASS" in text


def test_doctor_watch_compose_warn_when_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("SLIPSTREAM_MOCK", raising=False)
    monkeypatch.delenv("SLIPSTREAM_WATCH_SKILL", raising=False)
    monkeypatch.setenv("SLIPSTREAM_SPACES_ROOT", str(tmp_path / "spaces-watch"))
    fake_bin = tmp_path / "fake-chrome"
    fake_bin.write_text("#!/bin/sh\nexit 0\n")
    fake_bin.chmod(0o755)
    monkeypatch.setenv("SLIPSTREAM_CHROME", str(fake_bin))
    # Force no watch skill by pointing home-ish paths away via env empty + cwd without skills/watch
    ok_cdp = CheckResult(name="cdp_probe", status="ok", message="ok", detail={})
    with patch("slipstream.doctor._probe_cdp_with_chrome", return_value=ok_cdp):
        with patch("slipstream.doctor.find_watch_skill", return_value=None):
            report = run_doctor(url="http://127.0.0.1:1")
    names = {c.name: c for c in report.checks}
    assert names["watch_compose"].status == "warn"
    assert report.ok is True


def test_doctor_watch_compose_ok_when_present(tmp_path, monkeypatch):
    monkeypatch.delenv("SLIPSTREAM_MOCK", raising=False)
    watch = tmp_path / "watch-SKILL.md"
    watch.write_text("# watch\n")
    monkeypatch.setenv("SLIPSTREAM_WATCH_SKILL", str(watch))
    monkeypatch.setenv("SLIPSTREAM_SPACES_ROOT", str(tmp_path / "spaces-watch2"))
    fake_bin = tmp_path / "fake-chrome"
    fake_bin.write_text("#!/bin/sh\nexit 0\n")
    fake_bin.chmod(0o755)
    monkeypatch.setenv("SLIPSTREAM_CHROME", str(fake_bin))
    ok_cdp = CheckResult(name="cdp_probe", status="ok", message="ok", detail={})
    with patch("slipstream.doctor._probe_cdp_with_chrome", return_value=ok_cdp):
        report = run_doctor(url="http://127.0.0.1:1")
    names = {c.name: c for c in report.checks}
    assert names["watch_compose"].status == "ok"
    assert report.ok is True


def test_pool_healthz_http_503_fails_and_exit_1(tmp_path, monkeypatch):
    """HTTPError (e.g. 503) must fail doctor (not WARN) and exit 1."""
    import urllib.error

    monkeypatch.delenv("SLIPSTREAM_MOCK", raising=False)
    monkeypatch.setenv("SLIPSTREAM_SPACES_ROOT", str(tmp_path / "spaces-503"))
    fake_bin = tmp_path / "fake-chrome"
    fake_bin.write_text("#!/bin/sh\nexit 0\n")
    fake_bin.chmod(0o755)
    monkeypatch.setenv("SLIPSTREAM_CHROME", str(fake_bin))

    def boom(*_a, **_k):
        raise urllib.error.HTTPError(
            url="http://127.0.0.1:8755/healthz",
            code=503,
            msg="Service Unavailable",
            hdrs=None,
            fp=None,
        )

    ok_cdp = CheckResult(name="cdp_probe", status="ok", message="ok", detail={})
    with patch("slipstream.doctor._probe_cdp_with_chrome", return_value=ok_cdp):
        with patch("urllib.request.urlopen", side_effect=boom):
            report = run_doctor(url="http://127.0.0.1:8755")
            out = io.StringIO()
            with redirect_stdout(out):
                code = cmd_doctor(url="http://127.0.0.1:8755", as_json=False)

    names = {c.name: c for c in report.checks}
    assert names["pool_healthz"].status == "fail"
    assert "503" in names["pool_healthz"].message
    assert report.ok is False
    assert code == 1


def test_ephemeral_loopback_port_is_free_int():
    from slipstream.doctor import _ephemeral_loopback_port

    p1 = _ephemeral_loopback_port()
    p2 = _ephemeral_loopback_port()
    assert isinstance(p1, int) and 0 < p1 < 65536
    assert isinstance(p2, int) and 0 < p2 < 65536


def test_find_watch_skill_validates_content(tmp_path, monkeypatch):
    monkeypatch.delenv("SLIPSTREAM_WATCH_SKILL", raising=False)
    decoy = tmp_path / "skills" / "watch"
    decoy.mkdir(parents=True)
    bad = decoy / "SKILL.md"
    bad.write_text("# unrelated skill\nname: other\n")
    # Isolate candidates to this tmp skill only (ignore host-installed /watch)
    monkeypatch.setattr(
        "slipstream.doctor._watch_skill_candidates",
        lambda: [bad],
    )
    assert find_watch_skill() is None

    bad.write_text("---\nname: watch\ndescription: video\n---\n# /watch\n")
    found = find_watch_skill()
    assert found is not None
    assert found == bad.resolve()


def test_watch_compose_status_ok_and_warn(tmp_path, monkeypatch):
    monkeypatch.delenv("SLIPSTREAM_WATCH_SKILL", raising=False)
    monkeypatch.setattr("slipstream.doctor._watch_skill_candidates", lambda: [])
    st = watch_compose_status()
    assert st["ok"] is False
    assert st["status"] == "warn"
    assert st["required"] is False
    assert "bradautomates/claude-video" in str(st["upstream"])

    skill = tmp_path / "watch-SKILL.md"
    skill.write_text("---\nname: watch\n---\ncompose bradautomates/claude-video /watch\n")
    monkeypatch.setattr(
        "slipstream.doctor._watch_skill_candidates",
        lambda: [skill],
    )
    st2 = watch_compose_status()
    assert st2["ok"] is True
    assert st2["status"] == "ok"
    assert st2["path"]


def test_main_watch_status_subcommand(tmp_path, monkeypatch):
    monkeypatch.delenv("SLIPSTREAM_WATCH_SKILL", raising=False)
    monkeypatch.setattr("slipstream.doctor._watch_skill_candidates", lambda: [])
    code, out, err = _run_main(["watch-status", "--json"])
    assert code == 0, err
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["status"] == "warn"

    skill = tmp_path / "w.md"
    skill.write_text("---\nname: watch\n---\n")
    monkeypatch.setattr(
        "slipstream.doctor._watch_skill_candidates",
        lambda: [skill],
    )
    code, out, err = _run_main(["watch-status"])
    assert code == 0, err
    assert "OK" in out or "watch_compose" in out

