"""Permission ladder (confirm-actions) — mock-only.

QA: lease → gated eval → confirmation_required → deny / timeout deny →
confirm allows once; secrets refused; need_human sibling has watch_url.
"""

from __future__ import annotations

import io
import json
import time
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout

import pytest

from slipstream import __main__ as mainmod
from slipstream.actions import (
    CATEGORIES,
    KIND_CONFIRMATION_REQUIRED,
    confirm_ttl_seconds,
    parse_action_request,
)
from slipstream.alerts import AlertValidationError, reject_secret_fields
from slipstream.api import PoolServer
from slipstream.cli import _validate_api_request_url, cmd_act, cmd_confirm, cmd_deny
from slipstream.config import PoolConfig
from slipstream.pool import BrowserPool


@pytest.fixture
def api_server(tmp_path):
    cfg = PoolConfig(
        K=5,
        W=1,
        spaces_root=tmp_path / "spaces",
        vault_root=tmp_path / "vault",
        cdp_base_port=19522,
        mock=True,
        host="127.0.0.1",
        port=18757,
    )
    pool = BrowserPool(cfg)
    server = PoolServer(pool, port=18757)
    server.start(background=True)
    yield server
    server.stop()


def _req(method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
    if not url.startswith(("http://127.0.0.1:", "http://localhost:")):
        raise ValueError(f"test helper refuses non-loopback URL: {url!r}")
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        _validate_api_request_url(url),
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as resp:  # skylos: ignore[SKY-D216]
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def _lease(base: str, space: str = "act-space") -> str:
    code, lease = _req(
        "POST",
        f"{base}/v1/leases",
        {"agent_id": "agent-act", "space_id": space},
    )
    assert code == 200, lease
    return lease["lease_id"]


def test_categories_are_four():
    assert CATEGORIES == frozenset(
        {"eval", "download", "upload", "nav_irreversible"}
    )


def test_gated_eval_returns_confirmation_required(api_server: PoolServer):
    base = api_server.base_url
    lid = _lease(base)
    code, body = _req(
        "POST",
        f"{base}/v1/leases/{lid}/actions",
        {"category": "eval", "summary": "probe document.title"},
    )
    assert code == 202
    assert body["status"] == "confirmation_required"
    assert body["confirm_id"].startswith("c_")
    assert body["category"] == "eval"
    assert body["summary"] == "probe document.title"
    assert body["lease_id"] == lid
    assert body["expires_at"]
    # Sibling need_human on same alerts bus
    alert = body["alert"]
    assert alert["event"] == "need_human"
    assert alert["kind"] == KIND_CONFIRMATION_REQUIRED
    assert alert["confirm_id"] == body["confirm_id"]
    assert alert["reason"] == "confirmation_required"
    assert alert["status"] == "awaiting_human"
    assert alert["watch_url"]
    assert "token=" in alert["watch_url"]
    harness = body["harness"]
    assert harness["action"] == "pause"
    assert harness["agent_paused"] is True
    assert harness["lease_kept"] is True
    assert harness["watch_url"]
    assert harness["takeover_url"]
    assert "confirm|deny" in harness["captain_message"]
    # Lease still warm
    code, hb = _req("POST", f"{base}/v1/leases/{lid}/heartbeat", {})
    assert code == 200 and hb["ok"] is True


def test_confirm_allows_once_then_re_gate(api_server: PoolServer):
    base = api_server.base_url
    lid = _lease(base, space="once-space")
    code, body = _req(
        "POST",
        f"{base}/v1/leases/{lid}/actions",
        {"category": "download", "summary": "save report.pdf"},
    )
    assert code == 202
    cid = body["confirm_id"]

    code, resolved = _req(
        "POST",
        f"{base}/v1/leases/{lid}/confirmations/{cid}",
        {"action": "confirm"},
    )
    assert code == 200
    assert resolved["status"] == "confirmed"
    assert resolved["decision"] == "allow"
    assert resolved["confirm_id"] == cid

    # Same confirm_id cannot be reused
    code, again = _req(
        "POST",
        f"{base}/v1/leases/{lid}/confirmations/{cid}",
        {"action": "confirm"},
    )
    assert code == 410
    assert again["error"] == "confirmation_gone"

    # Next gated act re-gates
    code, body2 = _req(
        "POST",
        f"{base}/v1/leases/{lid}/actions",
        {"category": "upload", "summary": "attach receipt"},
    )
    assert code == 202
    assert body2["status"] == "confirmation_required"
    assert body2["confirm_id"] != cid


def test_deny_fails_closed(api_server: PoolServer):
    base = api_server.base_url
    lid = _lease(base, space="deny-space")
    code, body = _req(
        "POST",
        f"{base}/v1/leases/{lid}/actions",
        {"category": "nav_irreversible", "summary": "submit checkout form"},
    )
    assert code == 202
    cid = body["confirm_id"]
    code, resolved = _req(
        "POST",
        f"{base}/v1/confirmations/{cid}",
        {"action": "deny"},
    )
    assert code == 200
    assert resolved["status"] == "denied"
    assert resolved["decision"] == "deny"


def test_timeout_auto_denies(api_server: PoolServer, monkeypatch):
    monkeypatch.setenv("SLIPSTREAM_CONFIRM_TTL", "1")
    # confirm_ttl_seconds reads env at call time
    assert confirm_ttl_seconds() == 1
    base = api_server.base_url
    lid = _lease(base, space="ttl-space")
    code, body = _req(
        "POST",
        f"{base}/v1/leases/{lid}/actions",
        {"category": "eval", "summary": "short ttl"},
    )
    assert code == 202
    cid = body["confirm_id"]
    # Force expiry via pool sweep
    pool = api_server.pool
    future = time.time() + 5
    with pool._lock:
        pool._expire_confirmations_locked(future)
    code, gone = _req(
        "POST",
        f"{base}/v1/leases/{lid}/confirmations/{cid}",
        {"action": "confirm"},
    )
    assert code == 410
    assert gone["error"] == "confirmation_gone"


def test_secrets_refused_in_action_body(api_server: PoolServer):
    base = api_server.base_url
    lid = _lease(base, space="secret-space")
    for bad in (
        {"category": "eval", "summary": "x", "password": "x"},
        {"category": "eval", "summary": "x", "cookie": "a=b"},
        {"category": "eval", "summary": "x", "token": "t"},
        {"category": "eval", "summary": "x", "jwt": "j"},
        {"category": "eval", "summary": "x", "bearer": "b"},
        {"category": "eval", "summary": "x", "private_key": "k"},
        {"category": "eval", "summary": "x", "access_key": "a"},
    ):
        code, body = _req("POST", f"{base}/v1/leases/{lid}/actions", bad)
        assert code == 400, bad
        assert body["error"] == "invalid_action"


def test_summary_scrub_and_reject_empty(api_server: PoolServer):
    base = api_server.base_url
    lid = _lease(base, space="scrub-space")
    code, body = _req(
        "POST",
        f"{base}/v1/leases/{lid}/actions",
        {"category": "eval", "summary": "run probe password=supersecret"},
    )
    assert code == 202
    assert "[REDACTED]" in body["summary"]
    assert "supersecret" not in json.dumps(body)

    code, bad = _req(
        "POST",
        f"{base}/v1/leases/{lid}/actions",
        {"category": "eval", "summary": "   "},
    )
    assert code == 400


def test_unknown_category_400(api_server: PoolServer):
    base = api_server.base_url
    lid = _lease(base, space="cat-space")
    code, body = _req(
        "POST",
        f"{base}/v1/leases/{lid}/actions",
        {"category": "click", "summary": "soft browse stays free"},
    )
    assert code == 400
    assert body["error"] == "invalid_action"


def test_parse_action_request_helper():
    out = parse_action_request(
        {"category": "eval", "summary": "ok"}
    )
    assert out == {"category": "eval", "summary": "ok"}
    with pytest.raises(Exception):
        parse_action_request({"category": "click", "summary": "x"})
    with pytest.raises(AlertValidationError):
        reject_secret_fields({"password": "x"})


def test_cli_act_confirm_deny(api_server: PoolServer):
    base = api_server.base_url
    lid = _lease(base, space="cli-space")

    out = io.StringIO()
    with redirect_stdout(out):
        code = cmd_act(
            lease_id=lid,
            category="eval",
            summary="cli probe",
            url=base,
        )
    assert code == 0
    body = json.loads(out.getvalue())
    assert body["status"] == "confirmation_required"
    cid = body["confirm_id"]

    out = io.StringIO()
    with redirect_stdout(out):
        code = cmd_confirm(confirm_id=cid, url=base)
    assert code == 0
    resolved = json.loads(out.getvalue())
    assert resolved["decision"] == "allow"

    # Fresh act then deny via main()
    out = io.StringIO()
    with redirect_stdout(out):
        code = cmd_act(
            lease_id=lid, category="upload", summary="cli upload", url=base
        )
    assert code == 0
    cid2 = json.loads(out.getvalue())["confirm_id"]
    out = io.StringIO()
    with redirect_stdout(out):
        code = cmd_deny(confirm_id=cid2, url=base)
    assert code == 0
    assert json.loads(out.getvalue())["decision"] == "deny"


def test_confirm_interactive_non_tty_denies(api_server: PoolServer, monkeypatch):
    base = api_server.base_url
    lid = _lease(base, space="nontty-space")

    class _FakeStdin:
        def isatty(self) -> bool:
            return False

        def readline(self) -> str:
            return "y\n"

    monkeypatch.setattr("sys.stdin", _FakeStdin())
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cmd_act(
            lease_id=lid,
            category="eval",
            summary="nontty",
            url=base,
            confirm_interactive=True,
        )
    assert code == 1
    body = json.loads(out.getvalue())
    assert body["decision"] == "deny"
    assert body["status"] == "denied"


def test_main_act_confirm_wire(api_server: PoolServer):
    base = api_server.base_url
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mainmod.main(
            [
                "lease",
                "--agent-id",
                "m1",
                "--space-id",
                "main-act",
                "--url",
                base,
            ]
        )
    assert code == 0
    lid = json.loads(out.getvalue())["lease_id"]

    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mainmod.main(
            [
                "act",
                "--lease-id",
                lid,
                "--category",
                "eval",
                "--summary",
                "via main",
                "--url",
                base,
            ]
        )
    assert code == 0
    body = json.loads(out.getvalue())
    cid = body["confirm_id"]

    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mainmod.main(["confirm", cid, "--url", base])
    assert code == 0
    assert json.loads(out.getvalue())["decision"] == "allow"


def test_smoke_lease_eval_confirm_path(api_server: PoolServer):
    """CTO gate smoke: lease → gated eval → confirmation_required → confirm once."""
    base = api_server.base_url
    lid = _lease(base, space="smoke-space")
    code, body = _req(
        "POST",
        f"{base}/v1/leases/{lid}/actions",
        {"category": "eval", "summary": "smoke eval"},
    )
    assert code == 202 and body["status"] == "confirmation_required"
    assert body["alert"]["watch_url"]
    cid = body["confirm_id"]
    code, ok = _req(
        "POST",
        f"{base}/v1/leases/{lid}/confirmations/{cid}",
        {"action": "confirm"},
    )
    assert code == 200 and ok["decision"] == "allow"
    # re-gate
    code, body2 = _req(
        "POST",
        f"{base}/v1/leases/{lid}/actions",
        {"category": "eval", "summary": "smoke again"},
    )
    assert code == 202
    code, denied = _req(
        "POST",
        f"{base}/v1/leases/{lid}/confirmations/{body2['confirm_id']}",
        {"action": "deny"},
    )
    assert code == 200 and denied["decision"] == "deny"
