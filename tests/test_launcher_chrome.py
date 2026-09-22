"""find_chrome_binary explicit-path fail-closed."""

from __future__ import annotations

from pathlib import Path

from slipstream.launcher import find_chrome_binary


def test_explicit_missing_path_no_fallthrough(tmp_path, monkeypatch):
    missing = tmp_path / "no-such-chrome"
    # Ensure PATH still has real chrome candidates; explicit must still return None
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert find_chrome_binary(str(missing)) is None


def test_explicit_non_executable_no_fallthrough(tmp_path):
    f = tmp_path / "chrome-not-exec"
    f.write_text("#!/bin/sh\n")
    f.chmod(0o644)
    assert find_chrome_binary(str(f)) is None


def test_explicit_executable_ok(tmp_path):
    f = tmp_path / "chrome-ok"
    f.write_text("#!/bin/sh\n")
    f.chmod(0o755)
    assert find_chrome_binary(str(f)) == str(f)


def test_none_still_autodetects():
    # Smoke: None uses PATH/common paths; may or may not find chrome — just no crash
    result = find_chrome_binary(None)
    assert result is None or isinstance(result, str)
