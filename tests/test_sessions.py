"""Ops session list (slipstream-session-list-001)."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

import pytest

from slipstream.api import PoolServer
from slipstream.config import PoolConfig
from slipstream.pool import BrowserPool
from slipstream.sessions import (
    alive_watch_url,
    duration_seconds,
    format_sessions_table,
    render_sessions_html,
    session_row,
)


@pytest.fixture
def pool(tmp_path):
    cfg = PoolConfig(
        K=3,
        W=1,
        spaces_root=tmp_path / "spaces",
        vault_root=tmp_path / "vault",
        mock=True,
        host="127.0.0.1",
        port=0,
    )
    p = BrowserPool(cfg)
    yield p
    p.shutdown()


@pytest.fixture
def api_server(tmp_path):
    cfg = PoolConfig(
        K=3,
        W=1,
        spaces_root=tmp_path / "spaces",
        vault_root=tmp_path / "vault",
        cdp_base_port=19722,
        mock=True,
        host="127.0.0.1",
        port=18781,
    )
    pool = BrowserPool(cfg)
    server = PoolServer(pool, port=18781)
    server.start(background=True)
    yield server
    server.stop()


def _req(method: str, url: str, body: dict | None = None):
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"} if body is not None else {}
    r = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if "json" in ctype:
                return resp.status, json.loads(raw.decode()), ctype
            return resp.status, raw.decode(), ctype
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"raw": raw}
        return e.code, payload, "application/json"


def test_helpers_duration_and_watch_url():
    assert duration_seconds(100.0, now=142.7) == 42
    assert duration_seconds(None) is None
    assert (
        alive_watch_url(
            lease_id="L1",
            token="tok",
            revoked=False,
            expires_at=9999999999,
            base_url="http://127.0.0.1:8755",
            now=1.0,
        )
        is not None
    )
    assert (
        alive_watch_url(
            lease_id="L1",
            token="tok",
            revoked=True,
            expires_at=9999999999,
            base_url="http://127.0.0.1:8755",
            now=1.0,
        )
        is None
    )
    assert (
        alive_watch_url(
            lease_id="L1",
            token="tok",
            revoked=False,
            expires_at=10.0,
            base_url="http://127.0.0.1:8755",
            now=10.0,
        )
        is None
    )
    row = session_row(
        space_id="s",
        lease_id="l",
        agent_id="a",
        status="leased",
        leased_at=1.0,
        user_metadata={"env": "dev"},
        signed_in=False,
        now=5.0,
    )
    assert row["duration_s"] == 4
    assert "watch_url" not in row
    html = render_sessions_html([row])
    assert "Open Watch" not in html
    assert "s" in html
    table = format_sessions_table([row])
    assert "SPACE" in table and "s" in table


def test_list_sessions_duration_and_watch(pool: BrowserPool):
    lease = pool.lease("agent-1", "space-a")
    lid = lease["lease_id"] if isinstance(lease, dict) else lease.lease_id
    out = pool.list_sessions()
    assert len(out["sessions"]) == 1
    row = out["sessions"][0]
    assert row["space_id"] == "space-a"
    assert row["lease_id"] == lid
    assert row["status"] == "leased"
    assert isinstance(row["duration_s"], int)
    assert "watch_url" not in row

    env = pool.raise_alert(
        lid,
        {"event": "need_human", "reason": "login", "detail": "sign in", "ttl_s": 120},
    )
    watch = env["harness"]["watch_url"]
    assert "token=" in watch
    out2 = pool.list_sessions()
    row2 = out2["sessions"][0]
    assert row2["status"] == "awaiting_human"
    assert row2.get("watch_url") == watch

    leases = pool.list_leases()
    assert leases["leases"][0].get("watch_url") == watch
    assert "duration_s" in leases["leases"][0]


def test_ops_api_json_and_html(api_server: PoolServer):
    base = api_server.base_url
    code, lease, _ = _req(
        "POST",
        f"{base}/v1/leases",
        {"agent_id": "a1", "space_id": "ops1"},
    )
    assert code == 200
    lid = lease["lease_id"]
    code, alert, _ = _req(
        "POST",
        f"{base}/v1/leases/{lid}/alerts",
        {"event": "need_human", "reason": "login", "detail": "hi", "ttl_s": 90},
    )
    assert code == 200
    watch = alert["harness"]["watch_url"]

    code, payload, ctype = _req("GET", f"{base}/v1/ops/sessions")
    assert code == 200
    assert "json" in ctype
    assert payload["sessions"][0]["watch_url"] == watch
    assert payload["sessions"][0]["duration_s"] >= 0

    code, html, ctype = _req("GET", f"{base}/v1/ops/")
    assert code == 200
    assert "html" in ctype
    assert "Open Watch" in html
    assert "token=" in html  # button href carries secret URL (HTML only)


def test_cli_sessions_table(api_server: PoolServer, capsys):
    from slipstream.cli import cmd_sessions

    base = api_server.base_url
    code, lease, _ = _req(
        "POST", f"{base}/v1/leases", {"agent_id": "a1", "space_id": "cli1"}
    )
    assert code == 200
    lid = lease["lease_id"]
    rc = cmd_sessions(url=base)
    assert rc == 0
    out = capsys.readouterr().out
    assert "SPACE" in out
    assert "cli1" in out
    assert "token=" not in out  # table must not leak secret URL

    code, _, _ = _req(
        "POST",
        f"{base}/v1/leases/{lid}/alerts",
        {"event": "need_human", "reason": "stuck", "detail": "help", "ttl_s": 60},
    )
    assert code == 200
    rc = cmd_sessions(url=base, as_json=True)
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["sessions"][0].get("watch_url")
    # table still hides token
    rc = cmd_sessions(url=base)
    assert rc == 0
    out2 = capsys.readouterr().out
    assert "token=" not in out2
    assert "yes" in out2  # watch presence column
