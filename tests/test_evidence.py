"""Watch evidence stills — JPEG keyed by activity-feed seq."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from slipstream.api import PoolServer
from slipstream.cli import _validate_api_request_url
from slipstream.config import PoolConfig
from slipstream.evidence import (
    clear_lease_evidence,
    evidence_auto_kinds,
    list_evidence_markers,
    read_evidence_jpeg,
    scrub_annotation,
    store_evidence_jpeg,
)
from slipstream.pool import BrowserPool


@pytest.fixture
def api_server(tmp_path, monkeypatch):
    monkeypatch.setenv("SLIPSTREAM_EVIDENCE_AUTO", "navigate,confirm,alert")
    cfg = PoolConfig(
        K=5,
        W=1,
        spaces_root=tmp_path / "spaces",
        vault_root=tmp_path / "vault",
        artifacts_root=tmp_path / "artifacts",
        cdp_base_port=19850,
        mock=True,
        host="127.0.0.1",
        port=18892,
    )
    pool = BrowserPool(cfg)
    server = PoolServer(pool, port=18892)
    server.start(background=True)
    yield server
    server.stop()


def _req(method: str, url: str, body: dict | None = None):
    if not url.startswith(("http://127.0.0.1:", "http://localhost:")):
        raise ValueError(f"test helper refuses non-loopback URL: {url!r}")
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"} if data is not None else {}
    request = urllib.request.Request(
        _validate_api_request_url(url),
        data=data,
        method=method,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as resp:  # skylos: ignore[SKY-D216]
            raw = resp.read()
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "json" in ctype:
                return resp.status, json.loads(raw.decode("utf-8"))
            return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return e.code, raw


def _lease(base: str, space: str = "ev-space") -> str:
    code, lease = _req(
        "POST",
        f"{base}/v1/leases",
        {"agent_id": "agent-ev", "space_id": space},
    )
    assert code == 200, lease
    return lease["lease_id"]


def _watch_token(base: str, lease_id: str) -> str:
    code, env = _req(
        "POST",
        f"{base}/v1/leases/{lease_id}/alerts",
        {"event": "need_human", "reason": "login", "detail": "please", "ttl_s": 120},
    )
    assert code == 200, env
    watch = env["alert"]["watch_url"]
    return parse_qs(urlparse(watch).query)["token"][0]


def test_scrub_annotation_drops_secrets():
    out = scrub_annotation(
        {
            "kind": "navigate",
            "summary": "https://ex.test/a",
            "password": "nope",
            "refs": ["nav", {"label": "step-1"}, {"label": "token=leak"}],
            "seq": 3,
        }
    )
    assert out["kind"] == "navigate"
    assert "password" not in out
    assert "password" not in json.dumps(out)
    assert "token=leak" not in json.dumps(out)
    assert "nav" in out.get("refs", [])


def test_store_list_read_prune(tmp_path, monkeypatch):
    monkeypatch.setenv("SLIPSTREAM_EVIDENCE_MAX", "2")
    root = tmp_path / "art"
    root.mkdir(parents=True, exist_ok=True)
    jpeg = bytes((0xFF, 0xD8, 0xFF, 0xD9))
    for seq in (1, 2, 3):
        store_evidence_jpeg(
            root,
            "lease-a",
            seq=seq,
            jpeg=jpeg,
            annotation={"kind": "navigate", "summary": f"s{seq}", "refs": ["r"]},
        )
    listed = list_evidence_markers(root, "lease-a")
    assert listed["count"] == 2
    seqs = [m["seq"] for m in listed["markers"]]
    assert seqs == [2, 3]
    assert read_evidence_jpeg(root, "lease-a", 3)[:2] == b"\xff\xd8"
    clear_lease_evidence(root, "lease-a")
    assert list_evidence_markers(root, "lease-a")["count"] == 0


def test_watch_evidence_api_auth_scrub_and_gone(api_server: PoolServer):
    base = api_server.base_url
    lid = _lease(base)
    token = _watch_token(base, lid)

    code, page = _req("GET", f"{base}/v1/leases/{lid}/watch?token={token}")
    assert code == 200
    assert isinstance(page, (bytes, bytearray))
    assert b"/watch/evidence" in page
    assert b"Evidence" in page

    code, nav = _req(
        "POST",
        f"{base}/v1/leases/{lid}/navigate",
        {"url": "https://example.com/x?secret=1"},
    )
    assert code == 200, nav

    code, ev = _req("GET", f"{base}/v1/leases/{lid}/watch/evidence?token={token}")
    assert code == 200, ev
    assert ev["count"] >= 1
    blob = json.dumps(ev)
    assert "secret=1" not in blob
    assert "password" not in blob
    marker = ev["markers"][-1]
    assert marker["kind"] == "navigate"
    assert "example.com" in marker["summary"]
    seq = marker["seq"]

    code, jpeg = _req(
        "GET", f"{base}/v1/leases/{lid}/watch/evidence?token={token}&seq={seq}"
    )
    assert code == 200
    assert isinstance(jpeg, (bytes, bytearray))
    assert jpeg[:2] == b"\xff\xd8"

    code, bad = _req("GET", f"{base}/v1/leases/{lid}/watch/evidence?token=wrong")
    assert code == 401

    code, done = _req(
        "POST",
        f"{base}/v1/leases/{lid}/alerts",
        {"event": "task_done", "outcome": {"ok": True, "summary": "done"}},
    )
    assert code == 200, done
    code, gone = _req("GET", f"{base}/v1/leases/{lid}/watch/evidence?token={token}")
    assert code == 410


def test_evidence_auto_env_off(monkeypatch):
    monkeypatch.setenv("SLIPSTREAM_EVIDENCE_AUTO", "0")
    assert evidence_auto_kinds() == frozenset()
    monkeypatch.setenv("SLIPSTREAM_EVIDENCE_AUTO", "navigate")
    assert evidence_auto_kinds() == frozenset({"navigate"})


def test_list_skips_sidecar_symlink_leaf(tmp_path):
    """ADV-EV-002: sidecar leaf symlink must not 404 the whole markers list."""
    root = tmp_path / "art"
    root.mkdir(parents=True, exist_ok=True)
    jpeg = bytes((0xFF, 0xD8, 0xFF, 0xD9))
    store_evidence_jpeg(
        root,
        "lease-a",
        seq=1,
        jpeg=jpeg,
        annotation={"kind": "navigate", "summary": "ok"},
    )
    store_evidence_jpeg(
        root,
        "lease-a",
        seq=2,
        jpeg=jpeg,
        annotation={"kind": "confirm", "summary": "ok2"},
    )
    ev_dir = root / "leases" / "lease-a" / "evidence"
    secret = tmp_path / "vault_secret.bin"
    secret.write_bytes(b"SECRET")
    bad = ev_dir / "ev_00000002.json"
    bad.unlink()
    bad.symlink_to(secret)
    listed = list_evidence_markers(root, "lease-a")
    assert listed["count"] == 2
    by_seq = {m["seq"]: m for m in listed["markers"]}
    assert by_seq[1]["kind"] == "navigate"
    # Bad sidecar → marker kept, annotation empty / no secret leak
    assert "kind" not in by_seq[2] or by_seq[2].get("kind") is None
    blob = json.dumps(listed)
    assert "SECRET" not in blob
    assert "vault_secret" not in blob


def test_list_skips_jpeg_symlink_leaf(tmp_path):
    """ADV-EV-002: JPEG leaf symlink skipped as missing marker."""
    root = tmp_path / "art"
    root.mkdir(parents=True, exist_ok=True)
    jpeg = bytes((0xFF, 0xD8, 0xFF, 0xD9))
    store_evidence_jpeg(
        root,
        "lease-a",
        seq=1,
        jpeg=jpeg,
        annotation={"kind": "navigate", "summary": "keep"},
    )
    store_evidence_jpeg(
        root,
        "lease-a",
        seq=2,
        jpeg=jpeg,
        annotation={"kind": "confirm", "summary": "drop"},
    )
    ev_dir = root / "leases" / "lease-a" / "evidence"
    target = tmp_path / "elsewhere.jpg"
    target.write_bytes(jpeg)
    bad = ev_dir / "ev_00000002.jpg"
    bad.unlink()
    bad.symlink_to(target)
    listed = list_evidence_markers(root, "lease-a")
    assert listed["count"] == 1
    assert listed["markers"][0]["seq"] == 1


def test_task_done_clears_evidence_and_inflight_revalidate(api_server: PoolServer):
    """ADV-EV-001/003: task_done clears files; sticky poll 410 (revalidate path)."""
    base = api_server.base_url
    lid = _lease(base, space="ev-clear")
    token = _watch_token(base, lid)
    code, nav = _req(
        "POST",
        f"{base}/v1/leases/{lid}/navigate",
        {"url": "https://example.com/clear-me"},
    )
    assert code == 200, nav
    code, ev = _req("GET", f"{base}/v1/leases/{lid}/watch/evidence?token={token}")
    assert code == 200 and ev["count"] >= 1
    seq = ev["markers"][-1]["seq"]
    art = Path(api_server.pool.config.artifacts_root)
    before = list((art / "leases" / lid / "evidence").glob("ev_*"))
    assert before, "expected evidence files before task_done"

    code, done = _req(
        "POST",
        f"{base}/v1/leases/{lid}/alerts",
        {"event": "task_done", "outcome": {"ok": True, "summary": "done"}},
    )
    assert code == 200, done
    after = list((art / "leases" / lid / "evidence").glob("ev_*"))
    assert after == [], f"ADV-EV-003 expected clear, found {after}"

    code, gone = _req(
        "GET", f"{base}/v1/leases/{lid}/watch/evidence?token={token}&seq={seq}"
    )
    assert code == 410
    code, gone_list = _req(
        "GET", f"{base}/v1/leases/{lid}/watch/evidence?token={token}"
    )
    assert code == 410
