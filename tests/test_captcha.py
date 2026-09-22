"""CAPTCHA chips — start→finish no need_human; fail/timeout → need_human; secrets refused."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlparse

import pytest

from slipstream.api import PoolServer
from slipstream.captcha import (
    EVENT_FAILED,
    EVENT_FINISHED,
    EVENT_STARTED,
    STATE_ESCALATED,
    STATE_FAILED,
    STATE_SOLVED,
    STATE_SOLVING,
    CaptchaState,
    apply_captcha_event,
    captcha_banner_html,
    captcha_chip_html,
    captcha_may_escalate,
    normalize_captcha_event,
    parse_captcha_request,
    scrub_captcha_detail,
)
from slipstream.alerts import AlertValidationError
from slipstream.cli import _validate_api_request_url
from slipstream.config import PoolConfig
from slipstream.pool import BrowserPool


@pytest.fixture
def api_server(tmp_path):
    cfg = PoolConfig(
        K=5,
        W=1,
        spaces_root=tmp_path / "spaces",
        cdp_base_port=19822,
        mock=True,
        host="127.0.0.1",
        port=18822,
    )
    pool = BrowserPool(cfg)
    server = PoolServer(pool, port=18822)
    server.start(background=True)
    yield server
    server.stop()


def _req(method: str, url: str, body: dict | None = None) -> tuple[int, dict | str]:
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
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if "html" in ctype:
                return resp.status, raw.decode("utf-8")
            return resp.status, json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw.decode("utf-8"))
        except Exception:
            return e.code, raw.decode("utf-8", errors="replace")


def _lease(base: str, space: str = "cap-space") -> str:
    code, lease = _req(
        "POST",
        f"{base}/v1/leases",
        {"agent_id": "agent-cap", "space_id": space},
    )
    assert code == 200, lease
    assert isinstance(lease, dict)
    return lease["lease_id"]


def _token_from_watch_url(watch_url: str) -> str:
    q = parse_qs(urlparse(watch_url).query)
    return q["token"][0]


def test_normalize_event_aliases():
    assert normalize_captcha_event("started") == EVENT_STARTED
    assert normalize_captcha_event("captcha_solving_finished") == EVENT_FINISHED
    assert normalize_captcha_event("FAILED") == EVENT_FAILED
    with pytest.raises(AlertValidationError):
        normalize_captcha_event("solve")


def test_start_finish_no_need_human(api_server: PoolServer):
    base = api_server.base_url
    lid = _lease(base, space="cap-ok")
    code, body = _req(
        "POST",
        f"{base}/v1/leases/{lid}/captcha",
        {"event": "started", "detail": "challenge visible"},
    )
    assert code == 200, body
    assert body["captcha"]["state"] == STATE_SOLVING
    assert body["captcha"]["event"] == EVENT_STARTED
    assert "alert" not in body
    assert "harness" not in body
    assert api_server.pool._leases[lid].status == "leased"

    code, body = _req(
        "POST",
        f"{base}/v1/leases/{lid}/captcha",
        {"event": "finished"},
    )
    assert code == 200, body
    assert body["captcha"]["state"] == STATE_SOLVED
    assert body["captcha"]["event"] == EVENT_FINISHED
    assert "alert" not in body
    assert api_server.pool._leases[lid].status == "leased"

    # Activity feed has captcha rows (via need_human watch mint then events —
    # without watch, feed still exists on lease).
    feed = api_server.pool._activity_feeds.get(lid)
    assert feed is not None
    rows = feed.list_events()
    kinds = [r["kind"] for r in rows]
    assert "captcha" in kinds
    summaries = [r["summary"] for r in rows if r["kind"] == "captcha"]
    assert EVENT_STARTED in summaries
    assert EVENT_FINISHED in summaries


def test_start_fail_raises_need_human(api_server: PoolServer):
    base = api_server.base_url
    lid = _lease(base, space="cap-fail")
    code, body = _req(
        "POST",
        f"{base}/v1/leases/{lid}/captcha",
        {"event": "captcha_solving_started"},
    )
    assert code == 200
    code, body = _req(
        "POST",
        f"{base}/v1/leases/{lid}/captcha",
        {"event": "failed", "detail": "unsolved"},
    )
    assert code == 200, body
    assert body["captcha"]["state"] == STATE_ESCALATED
    assert body["captcha"]["escalated"] is True
    assert "alert" in body
    assert body["alert"]["event"] == "need_human"
    assert body["alert"]["reason"] == "captcha"
    assert "watch_url" in body["alert"]
    assert "token=" in body["alert"]["watch_url"]
    assert body["harness"]["action"] == "pause"
    assert body["harness"]["lease_kept"] is True
    assert api_server.pool._leases[lid].status == "awaiting_human"

    # Watch HTML shows captcha fail chip
    token = _token_from_watch_url(body["alert"]["watch_url"])
    code, html = _req("GET", f"{base}/v1/leases/{lid}/watch?token={token}")
    assert code == 200
    assert isinstance(html, str)
    assert "CAPTCHA" in html
    assert "chip captcha" in html
    assert "need human" in html.lower()

    # Feed events include captcha + alert
    code, ev = _req(
        "GET", f"{base}/v1/leases/{lid}/watch/events?token={token}"
    )
    assert code == 200
    kinds = [e["kind"] for e in ev["events"]]
    assert "captcha" in kinds
    assert "alert" in kinds


def test_timeout_escalates_on_heartbeat(api_server: PoolServer, monkeypatch):
    base = api_server.base_url
    lid = _lease(base, space="cap-to")
    code, body = _req(
        "POST",
        f"{base}/v1/leases/{lid}/captcha",
        {"event": "started", "timeout_s": 5, "detail": "slow"},
    )
    assert code == 200
    # Force started_at into the past
    st = api_server.pool._captcha[lid]
    st.started_at = time.time() - 30
    assert st.is_timed_out()

    code, hb = _req("POST", f"{base}/v1/leases/{lid}/heartbeat", {})
    assert code == 200, hb
    assert hb.get("captcha_escalated") is True
    assert api_server.pool._leases[lid].status == "awaiting_human"
    st2 = api_server.pool._captcha[lid]
    assert st2.escalated is True
    assert st2.state == STATE_ESCALATED


def test_secrets_refused(api_server: PoolServer):
    base = api_server.base_url
    lid = _lease(base, space="cap-sec")
    for bad in (
        {"event": "started", "password": "x"},
        {"event": "started", "cookie": "a=b"},
        {"event": "finished", "token": "leak"},
        {"event": "failed", "authorization": "Bearer x"},
    ):
        code, body = _req("POST", f"{base}/v1/leases/{lid}/captcha", bad)
        assert code == 400, body
        assert body.get("error") == "invalid_captcha"
        assert "secret" in body["detail"].lower() or "refused" in body["detail"].lower()
    # Still leased — no escalate from refused bodies
    assert api_server.pool._leases[lid].status == "leased"


def test_client_state_overrides_refused(api_server: PoolServer):
    base = api_server.base_url
    lid = _lease(base)
    code, body = _req(
        "POST",
        f"{base}/v1/leases/{lid}/captcha",
        {"event": "started", "state": "solved"},
    )
    assert code == 400
    code, body = _req(
        "POST",
        f"{base}/v1/leases/{lid}/captcha",
        {"event": "started", "watch_url": "http://evil"},
    )
    assert code == 400


def test_parse_scrub_detail():
    parsed = parse_captcha_request(
        {"event": "started", "detail": "token=abc123 visible"}
    )
    assert "REDACTED" in parsed["detail"]
    assert parsed["event"] == EVENT_STARTED


def test_chip_html_states():
    from slipstream.captcha import CaptchaState, STATE_FAILED

    assert captcha_chip_html(None) == ""
    assert captcha_chip_html(CaptchaState(state=STATE_SOLVED)) == ""
    solving = CaptchaState(state=STATE_SOLVING, started_at=time.time())
    assert "solving" in captcha_chip_html(solving)
    failed = CaptchaState(state=STATE_FAILED)
    assert "need human" in captcha_chip_html(failed).lower()


def test_watch_chip_while_solving(api_server: PoolServer):
    """After need_human for other reason, captcha started shows solving chip."""
    base = api_server.base_url
    lid = _lease(base, space="cap-chip")
    code, env = _req(
        "POST",
        f"{base}/v1/leases/{lid}/alerts",
        {"event": "need_human", "reason": "stuck", "detail": "iframe"},
    )
    assert code == 200
    token = _token_from_watch_url(env["alert"]["watch_url"])
    code, _ = _req(
        "POST",
        f"{base}/v1/leases/{lid}/captcha",
        {"event": "started", "detail": "recaptcha"},
    )
    assert code == 200
    code, html = _req("GET", f"{base}/v1/leases/{lid}/watch?token={token}")
    assert code == 200
    assert "CAPTCHA solving" in html
    assert "captcha-banner" in html

def test_late_failed_after_finished_no_need_human(api_server: PoolServer):
    """ADV-CAP-001: once SOLVED, late failed must not escalate need_human."""
    base = api_server.base_url
    lid = _lease(base, space="cap-late-fail")
    code, body = _req(
        "POST",
        f"{base}/v1/leases/{lid}/captcha",
        {"event": "started"},
    )
    assert code == 200, body
    code, body = _req(
        "POST",
        f"{base}/v1/leases/{lid}/captcha",
        {"event": "finished"},
    )
    assert code == 200, body
    assert body["captcha"]["state"] == STATE_SOLVED
    assert "alert" not in body

    code, body = _req(
        "POST",
        f"{base}/v1/leases/{lid}/captcha",
        {"event": "failed", "detail": "late noise"},
    )
    assert code == 200, body
    assert body["captcha"]["state"] == STATE_SOLVED
    assert body["captcha"]["escalated"] is False
    assert "alert" not in body
    assert "harness" not in body
    assert api_server.pool._leases[lid].status == "leased"


def test_apply_failed_keeps_solved_unit():
    """ADV-CAP-001 unit: apply_captcha_event does not unwind SOLVED."""
    st = CaptchaState(state=STATE_SOLVED, escalated=False)
    apply_captcha_event(
        st,
        event=EVENT_FAILED,
        detail="late",
        provider=None,
        timeout_s=60,
    )
    assert st.state == STATE_SOLVED
    assert captcha_may_escalate(st) is False


def test_timeout_escalate_skips_when_already_solved(api_server: PoolServer):
    """ADV-CAP-002: timeout path must not escalate if finished→SOLVED won."""
    base = api_server.base_url
    lid = _lease(base, space="cap-to-solved")
    code, body = _req(
        "POST",
        f"{base}/v1/leases/{lid}/captcha",
        {"event": "started", "timeout_s": 5},
    )
    assert code == 200, body
    st = api_server.pool._captcha[lid]
    st.started_at = time.time() - 30
    # Finished wins before timeout escalate.
    code, body = _req(
        "POST",
        f"{base}/v1/leases/{lid}/captcha",
        {"event": "finished"},
    )
    assert code == 200, body
    assert body["captcha"]["state"] == STATE_SOLVED

    env = api_server.pool._escalate_captcha_timeout_if_needed(lid)
    assert env is None
    st2 = api_server.pool._captcha[lid]
    assert st2.state == STATE_SOLVED
    assert st2.escalated is False
    assert api_server.pool._leases[lid].status == "leased"

    code, hb = _req("POST", f"{base}/v1/leases/{lid}/heartbeat", {})
    assert code == 200, hb
    assert hb.get("captcha_escalated") is not True
    assert api_server.pool._leases[lid].status == "leased"


def test_timeout_escalate_recheck_under_lock(api_server: PoolServer):
    """ADV-CAP-002: if state becomes SOLVED under lock before escalate, skip."""
    base = api_server.base_url
    lid = _lease(base, space="cap-to-recheck")
    code, _ = _req(
        "POST",
        f"{base}/v1/leases/{lid}/captcha",
        {"event": "started", "timeout_s": 5},
    )
    assert code == 200
    st = api_server.pool._captcha[lid]
    st.started_at = time.time() - 30
    assert st.is_timed_out()

    # Simulate concurrent finished by flipping to SOLVED while "timed out" bookkeeping
    # would have run — escalate must observe SOLVED and abort.
    st.state = STATE_SOLVED
    env = api_server.pool._escalate_captcha_timeout_if_needed(lid)
    assert env is None
    assert api_server.pool._captcha[lid].state == STATE_SOLVED
    assert api_server.pool._captcha[lid].escalated is False
    assert api_server.pool._leases[lid].status == "leased"


def test_scrub_unlabeled_jwt_and_secrets():
    """ADV-CAP-003: bare JWT / sk- / AKIA scrubbed in captcha detail."""
    jwt = (
        "challenge eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "aaaaaaaaaaaaaaaa."
        "bbbbbbbbbbbbbbbb visible"
    )
    out = scrub_captcha_detail(jwt)
    assert "REDACTED_JWT" in out
    assert "eyJhbGci" not in out

    sk = scrub_captcha_detail("solver sk-abcdefghijklmnopqrstuvwxyz12 done")
    assert "[REDACTED]" in sk
    assert "sk-abcdef" not in sk

    parsed = parse_captcha_request(
        {
            "event": "started",
            "detail": (
                "tok eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
                "ccccccccddddddddeeee."
                "fffffggggghhhhiiiijjjj"
            ),
        }
    )
    assert "REDACTED_JWT" in parsed["detail"]
    assert "eyJ" not in parsed["detail"]


def test_chip_banner_html_escapes_dynamic():
    """ADV-CAP-004: provider/detail in chip/banner titles/body are escaped."""
    evil = CaptchaState(
        state=STATE_SOLVING,
        started_at=time.time(),
        provider='"><img src=x onerror=alert(1)>',
        detail='</span><script>alert(1)</script>',
    )
    chip = captcha_chip_html(evil)
    assert "<script>" not in chip
    # Attribute-quoted: leading \"> must be escaped so markup cannot break out.
    assert "&quot;" in chip or "&#x27;" in chip or "&#39;" in chip
    assert 'title="">' not in chip
    assert "&lt;script&gt;" in chip

    fail = CaptchaState(
        state=STATE_FAILED,
        detail='<b>x</b> & "y"',
        provider="p<script>",
    )
    banner = captcha_banner_html(fail)
    assert "<script>" not in banner
    assert "<b>" not in banner
    assert "&lt;b&gt;" in banner or "&lt;" in banner

