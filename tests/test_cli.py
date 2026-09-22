"""CLI command tests — mock HTTP via PoolServer (no Chrome)."""

from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout

import pytest

from slipstream import __main__ as mainmod
from slipstream.api import PoolServer
from slipstream.cli import (
    CliError,
    cmd_heartbeat,
    cmd_lease,
    cmd_release,
    cmd_status,
    resolve_base_url,
)
from slipstream.config import PoolConfig
from slipstream.pool import BrowserPool


@pytest.fixture
def api_server(tmp_path):
    cfg = PoolConfig(
        K=5,
        W=1,
        spaces_root=tmp_path / "spaces",
        cdp_base_port=19422,
        mock=True,
        host="127.0.0.1",
        port=18756,
    )
    pool = BrowserPool(cfg)
    server = PoolServer(pool, port=18756)
    server.start(background=True)
    yield server
    server.stop()


def _run_main(argv: list[str]) -> tuple[int, str, str]:
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mainmod.main(argv)
    return code, out.getvalue(), err.getvalue()


def test_resolve_base_url_precedence(monkeypatch):
    monkeypatch.delenv("SLIPSTREAM_URL", raising=False)
    assert resolve_base_url(None) == "http://127.0.0.1:8755"
    monkeypatch.setenv("SLIPSTREAM_URL", "http://127.0.0.1:9999/")
    assert resolve_base_url(None) == "http://127.0.0.1:9999"
    assert resolve_base_url("http://127.0.0.1:1111/") == "http://127.0.0.1:1111"


def test_cli_lease_heartbeat_release_status(api_server: PoolServer):
    base = api_server.base_url

    out = io.StringIO()
    with redirect_stdout(out):
        code = cmd_lease(agent_id="a1", space_id="s1", url=base)
    assert code == 0
    lease = json.loads(out.getvalue())
    assert lease["agent_id"] == "a1"
    assert lease["space_id"] == "s1"
    assert "lease_id" in lease
    assert "cdp_http_url" not in lease
    lid = lease["lease_id"]

    out = io.StringIO()
    with redirect_stdout(out):
        code = cmd_heartbeat(lease_id=lid, url=base)
    assert code == 0
    hb = json.loads(out.getvalue())
    assert hb.get("ok") is True

    out = io.StringIO()
    with redirect_stdout(out):
        code = cmd_status(url=base)
    assert code == 0
    status = json.loads(out.getvalue())
    assert status["K"] == 5
    assert status["leased"] >= 1

    out = io.StringIO()
    with redirect_stdout(out):
        code = cmd_release(lease_id=lid, url=base)
    assert code == 0
    rel = json.loads(out.getvalue())
    assert rel.get("released") is True


def test_cli_http_error_nonzero(api_server: PoolServer):
    base = api_server.base_url
    with pytest.raises(CliError) as ei:
        cmd_heartbeat(lease_id="does-not-exist", url=base)
    assert ei.value.exit_code == 1
    assert "404" in str(ei.value) or "lease_not_found" in str(ei.value)


def test_main_subcommands(api_server: PoolServer, monkeypatch):
    monkeypatch.setenv("SLIPSTREAM_URL", api_server.base_url)

    code, out, err = _run_main(
        ["lease", "--agent-id", "cli-a", "--space-id", "cli-s"]
    )
    assert code == 0, err
    lease = json.loads(out)
    lid = lease["lease_id"]

    code, out, err = _run_main(["heartbeat", "--lease-id", lid])
    assert code == 0, err
    assert json.loads(out).get("ok") is True

    code, out, err = _run_main(["status"])
    assert code == 0, err
    assert json.loads(out)["K"] == 5

    code, out, err = _run_main(["release", "--lease-id", lid, "--reason", "done"])
    assert code == 0, err
    assert json.loads(out).get("released") is True


def test_main_http_error_exit(api_server: PoolServer):
    code, out, err = _run_main(
        ["heartbeat", "--lease-id", "missing", "--url", api_server.base_url]
    )
    assert code != 0
    assert out == ""
    assert "HTTP" in err or "lease_not_found" in err


def test_main_no_command_help():
    code, out, err = _run_main([])
    assert code == 2
    assert "serve" in err or "usage" in err.lower()


def test_build_parser_has_expected_subcommands():
    parser = mainmod.build_parser()
    # Ensure subparsers registered
    choice = None
    for action in parser._actions:
        if getattr(action, "dest", None) == "command":
            choice = action.choices
            break
    assert choice is not None
    for name in ("serve", "lease", "heartbeat", "release", "status", "doctor"):
        assert name in choice


def test_connection_failure():
    with pytest.raises(CliError) as ei:
        cmd_status(url="http://127.0.0.1:1")
    assert ei.value.exit_code == 1
    assert "connection failed" in str(ei.value).lower() or "failed" in str(ei.value).lower()


def test_space_in_use_via_cli(api_server: PoolServer):
    base = api_server.base_url
    assert cmd_lease(agent_id="a1", space_id="shared", url=base) == 0
    with pytest.raises(CliError) as ei:
        cmd_lease(agent_id="a2", space_id="shared", url=base)
    assert "409" in str(ei.value) or "space_in_use" in str(ei.value)


def test_resolve_base_url_rejects_remote(monkeypatch):
    monkeypatch.delenv("SLIPSTREAM_ALLOW_REMOTE_URL", raising=False)
    monkeypatch.delenv("SLIPSTREAM_URL", raising=False)
    with pytest.raises(CliError) as ei:
        resolve_base_url("http://example.com:8755")
    assert ei.value.exit_code == 2
    assert "loopback" in str(ei.value).lower() or "refusing" in str(ei.value).lower()


def test_resolve_base_url_allows_remote_with_escape(monkeypatch):
    monkeypatch.setenv("SLIPSTREAM_ALLOW_REMOTE_URL", "1")
    assert resolve_base_url("http://example.com:8755") == "http://example.com:8755"


def test_resolve_base_url_allows_localhost(monkeypatch):
    monkeypatch.delenv("SLIPSTREAM_ALLOW_REMOTE_URL", raising=False)
    assert resolve_base_url("http://localhost:8755") == "http://localhost:8755"
    assert resolve_base_url("http://127.0.0.1:9999/") == "http://127.0.0.1:9999"
