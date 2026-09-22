"""Alert API tests — need_human keeps lease; task_done releases once; secrets refused."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout

import pytest

from slipstream import __main__ as mainmod
from slipstream.alerts import (
    format_captain_one_liner,
    parse_alert_request,
    reject_secret_fields,
    scrub_text,
    AlertValidationError,
)
from slipstream.api import PoolServer
from slipstream.config import PoolConfig
from slipstream.pool import BrowserPool
from slipstream.cli import _validate_api_request_url


@pytest.fixture
def api_server(tmp_path):
    cfg = PoolConfig(
        K=5,
        W=1,
        spaces_root=tmp_path / "spaces",
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
        with urllib.request.urlopen(request, timeout=5) as resp:  # skylos: ignore[SKY-D216] loopback test helper; URL via _validate_api_request_url
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def _lease(base: str, space: str = "alert-space") -> str:
    code, lease = _req(
        "POST",
        f"{base}/v1/leases",
        {"agent_id": "agent-alert", "space_id": space},
    )
    assert code == 200, lease
    return lease["lease_id"]


def test_need_human_keeps_lease(api_server: PoolServer):
    base = api_server.base_url
    lid = _lease(base)
    code, env = _req(
        "POST",
        f"{base}/v1/leases/{lid}/alerts",
        {"event": "need_human", "reason": "captcha", "detail": "challenge"},
    )
    assert code == 200
    assert env["alert"]["event"] == "need_human"
    assert env["alert"]["status"] == "awaiting_human"
    assert env["alert"]["lease_id"] == lid
    assert env["alert"]["watch_url"]
    assert "cookie" not in json.dumps(env).lower() or "cookie" not in str(
        env["alert"].keys()
    )
    assert env["harness"]["action"] == "pause"
    assert env["harness"]["agent_paused"] is True
    assert env["harness"]["lease_kept"] is True
    assert env["harness"]["lease_released"] is False
    assert "Need human" in env["harness"]["captain_message"]
    assert "Watch" in env["harness"]["captain_message"]
    assert "Take-over" in env["harness"]["captain_message"]

    # Lease still alive — heartbeat works; status shows leased
    code, hb = _req("POST", f"{base}/v1/leases/{lid}/heartbeat", {})
    assert code == 200 and hb["ok"] is True
    code, st = _req("GET", f"{base}/v1/pool/status")
    assert code == 200
    assert st["leased"] == 1
    slot = next(s for s in st["slots"] if s["lease_id"] == lid)
    assert slot["status"] == "leased"


def test_task_done_releases_once(api_server: PoolServer):
    base = api_server.base_url
    lid = _lease(base, space="done-space")
    code, env = _req(
        "POST",
        f"{base}/v1/leases/{lid}/alerts",
        {
            "event": "task_done",
            "outcome": {"ok": True, "summary": "filled form"},
        },
    )
    assert code == 200
    assert env["alert"]["event"] == "task_done"
    assert env["alert"]["status"] == "done"
    assert env["alert"]["outcome"]["ok"] is True
    assert env["harness"]["action"] == "continue"
    assert env["harness"]["lease_released"] is True
    assert env["harness"]["lease_kept"] is False
    assert env["harness"]["idempotent"] is False
    assert "Task done" in env["harness"]["captain_message"]

    code, st = _req("GET", f"{base}/v1/pool/status")
    assert st["leased"] == 0
    # Warm may keep the space
    assert st["leased"] == 0

    # Heartbeat on released lease → 404
    code, body = _req("POST", f"{base}/v1/leases/{lid}/heartbeat", {})
    assert code == 404


def test_double_task_done_idempotent(api_server: PoolServer):
    base = api_server.base_url
    lid = _lease(base, space="idem-space")
    body = {"event": "task_done", "outcome": {"ok": True, "summary": "once"}}
    code1, env1 = _req("POST", f"{base}/v1/leases/{lid}/alerts", body)
    assert code1 == 200
    event_id = env1["alert"]["event_id"]

    code2, env2 = _req("POST", f"{base}/v1/leases/{lid}/alerts", body)
    assert code2 == 200
    assert env2["harness"]["idempotent"] is True
    assert env2["alert"]["event_id"] == event_id
    assert env2["harness"]["lease_released"] is True

    # Still only warm/cold — not double-leased mess
    code, st = _req("GET", f"{base}/v1/pool/status")
    assert st["leased"] == 0


def test_reject_secret_like_fields(api_server: PoolServer):
    base = api_server.base_url
    lid = _lease(base, space="secret-space")
    for bad in (
        {"event": "need_human", "reason": "other", "cookie": "a=b"},
        {"event": "need_human", "reason": "other", "cookies": {"x": 1}},
        {"event": "need_human", "reason": "other", "password": "x"},
        {"event": "need_human", "reason": "other", "token": "x"},
        {"event": "need_human", "reason": "other", "authorization": "Bearer x"},
        {"event": "need_human", "reason": "other", "credentials": {}},
        {"event": "need_human", "reason": "other", "private_key": "x"},
        {"event": "need_human", "reason": "other", "jwt": "x"},
        {"event": "need_human", "reason": "other", "bearer": "x"},
        {"event": "need_human", "reason": "other", "access_key": "x"},
        {
            "event": "task_done",
            "outcome": {"ok": True, "summary": "x", "api_key": "leak"},
        },
        {
            "event": "need_human",
            "reason": "other",
            "detail": "ok",
            "nested": {"session_token": "x"},
        },
    ):
        code, body = _req("POST", f"{base}/v1/leases/{lid}/alerts", bad)
        assert code == 400, bad
        assert body["error"] == "invalid_alert"
        assert "secret" in body["detail"].lower() or "refused" in body["detail"].lower()

    # Lease still held after rejected alerts
    code, hb = _req("POST", f"{base}/v1/leases/{lid}/heartbeat", {})
    assert code == 200


def test_reject_secret_fields_helper():
    with pytest.raises(AlertValidationError):
        reject_secret_fields({"cookie": "x"})
    with pytest.raises(AlertValidationError):
        reject_secret_fields({"meta": {"auth_token": "x"}})
    with pytest.raises(AlertValidationError):
        reject_secret_fields({"private_key": "x"})
    with pytest.raises(AlertValidationError):
        reject_secret_fields({"access_key": "x"})
    # Token-boundary: secretary_note must NOT false-positive on "secret"
    reject_secret_fields(
        {"event": "need_human", "reason": "captcha", "detail": "ok", "secretary_note": "hi"}
    )
    reject_secret_fields({"event": "need_human", "reason": "captcha", "detail": "ok"})


def test_secretary_note_allowed_via_api(api_server: PoolServer):
    base = api_server.base_url
    lid = _lease(base, space="secretary-space")
    code, env = _req(
        "POST",
        f"{base}/v1/leases/{lid}/alerts",
        {
            "event": "need_human",
            "reason": "other",
            "detail": "ask secretary",
            "secretary_note": "benign",
        },
    )
    assert code == 200, env
    assert env["alert"]["status"] == "awaiting_human"


def test_client_watch_url_and_status_rejected(api_server: PoolServer):
    base = api_server.base_url
    lid = _lease(base, space="override-space")
    code, body = _req(
        "POST",
        f"{base}/v1/leases/{lid}/alerts",
        {
            "event": "need_human",
            "reason": "other",
            "watch_url": "http://evil.example/watch",
        },
    )
    assert code == 400
    assert body["error"] == "invalid_alert"
    code, body = _req(
        "POST",
        f"{base}/v1/leases/{lid}/alerts",
        {"event": "need_human", "reason": "other", "status": "done"},
    )
    assert code == 400
    assert body["error"] == "invalid_alert"


def test_scrub_detail_and_summary(api_server: PoolServer):
    assert "password=[REDACTED]" in scrub_text("user password=s3cret ok")
    assert "cookie=[REDACTED]" in scrub_text("cookie=abc; path=/")
    assert "token=[REDACTED]" in scrub_text("token=xyz")
    assert "secret=[REDACTED]" in scrub_text("secret=hunter2")
    assert "authorization=[REDACTED]" in scrub_text("authorization=Bearer abc")
    assert "bearer=[REDACTED]" in scrub_text("bearer=xyz")
    parsed = parse_alert_request(
        {
            "event": "need_human",
            "reason": "other",
            "detail": "see password=leak here",
        }
    )
    assert "password=[REDACTED]" in parsed["detail"]
    base = api_server.base_url
    lid = _lease(base, space="scrub-space")
    code, env = _req(
        "POST",
        f"{base}/v1/leases/{lid}/alerts",
        {
            "event": "task_done",
            "outcome": {"ok": True, "summary": "done token=abc123"},
        },
    )
    assert code == 200
    assert "token=[REDACTED]" in env["alert"]["outcome"]["summary"]


def test_need_human_after_task_done_is_404(api_server: PoolServer):
    base = api_server.base_url
    lid = _lease(base, space="post-done-space")
    code, _ = _req(
        "POST",
        f"{base}/v1/leases/{lid}/alerts",
        {"event": "task_done", "outcome": {"ok": True, "summary": "bye"}},
    )
    assert code == 200
    code, body = _req(
        "POST",
        f"{base}/v1/leases/{lid}/alerts",
        {"event": "need_human", "reason": "other"},
    )
    assert code == 404
    assert body["error"] == "lease_not_found"


def test_captain_one_liner_fields():
    msg = format_captain_one_liner(
        event="need_human",
        reason="login",
        detail="2FA",
        outcome=None,
        watch_url="http://127.0.0.1:8755/v1/leases/abc/watch",
        takeover_url="http://127.0.0.1:8755/v1/leases/abc/watch?mode=takeover",
        lease_id="abcdefgh-ijkl",
    )
    assert "Need human (login)" in msg
    assert "Watch" in msg and "Take-over" in msg
    assert "2FA" in msg


def test_cli_alert_need_human_and_done(api_server: PoolServer):
    base = api_server.base_url
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mainmod.main(
            [
                "lease",
                "--agent-id",
                "cli-a",
                "--space-id",
                "cli-alert",
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
                "alert",
                "need-human",
                "--lease-id",
                lid,
                "--reason",
                "ambiguous_ui",
                "--detail",
                "which button?",
                "--url",
                base,
            ]
        )
    assert code == 0
    env = json.loads(out.getvalue())
    assert env["harness"]["lease_kept"] is True

    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mainmod.main(
            [
                "alert",
                "done",
                "--lease-id",
                lid,
                "--summary",
                "human finished",
                "--url",
                base,
            ]
        )
    assert code == 0
    env = json.loads(out.getvalue())
    assert env["harness"]["lease_released"] is True


def test_unknown_lease_need_human_404(api_server: PoolServer):
    base = api_server.base_url
    code, body = _req(
        "POST",
        f"{base}/v1/leases/not-a-real-lease/alerts",
        {"event": "need_human", "reason": "other"},
    )
    assert code == 404
    assert body["error"] == "lease_not_found"


def test_bad_reason_400(api_server: PoolServer):
    base = api_server.base_url
    lid = _lease(base, space="bad-reason")
    code, body = _req(
        "POST",
        f"{base}/v1/leases/{lid}/alerts",
        {"event": "need_human", "reason": "explode"},
    )
    assert code == 400
    assert body["error"] == "invalid_alert"
