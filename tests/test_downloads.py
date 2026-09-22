"""Downloads as session artifacts — list/get, path safety, mock recorder, upload drop."""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from slipstream.api import PoolServer
from slipstream.cli import _validate_api_request_url
from slipstream.config import PoolConfig
from slipstream.downloads import (
    ArtifactNotFoundError,
    DownloadValidationError,
    artifact_id_for_filename,
    is_denied_filename,
    list_artifacts,
    read_artifact,
    sanitize_upload_filename,
)
from slipstream.pool import BrowserPool


@pytest.fixture
def api_server(tmp_path):
    cfg = PoolConfig(
        K=5,
        W=1,
        spaces_root=tmp_path / "spaces",
        vault_root=tmp_path / "vault",
        artifacts_root=tmp_path / "artifacts",
        cdp_base_port=19922,
        mock=True,
        host="127.0.0.1",
        port=18922,
    )
    pool = BrowserPool(cfg)
    server = PoolServer(pool, port=18922)
    server.start(background=True)
    yield server, pool
    server.stop()


def _req(method: str, url: str, body: dict | None = None, headers: dict | None = None):
    if not url.startswith(("http://127.0.0.1:", "http://localhost:")):
        raise ValueError(f"test helper refuses non-loopback URL: {url!r}")
    data = None if body is None else json.dumps(body).encode("utf-8")
    hdrs = {"Content-Type": "application/json"} if data else {}
    if headers:
        hdrs.update(headers)
    request = urllib.request.Request(
        _validate_api_request_url(url),
        data=data,
        method=method,
        headers=hdrs,
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as resp:  # skylos: ignore[SKY-D216]
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if "octet" in ctype or "image" in ctype:
                return resp.status, raw, dict(resp.headers)
            if "html" in ctype:
                return resp.status, raw.decode("utf-8"), dict(resp.headers)
            return resp.status, json.loads(raw.decode("utf-8")), dict(resp.headers)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw.decode("utf-8")), {}
        except Exception:
            return e.code, raw, {}


def _lease(base: str, space: str = "dl-space", agent: str = "agent-dl") -> str:
    code, lease, _ = _req(
        "POST",
        f"{base}/v1/leases",
        {"agent_id": agent, "space_id": space},
    )
    assert code == 200, lease
    assert isinstance(lease, dict)
    return lease["lease_id"]


def test_denied_filenames():
    assert is_denied_filename("password.txt")
    assert is_denied_filename("id_rsa")
    assert is_denied_filename(".env")
    assert is_denied_filename("foo.pem")
    assert not is_denied_filename("report.pdf")
    with pytest.raises(DownloadValidationError):
        sanitize_upload_filename("../etc/passwd")
    with pytest.raises(DownloadValidationError):
        sanitize_upload_filename("secret-token.txt")


def test_mock_recorder_and_list_get(api_server):
    server, pool = api_server
    base = server.base_url
    lid = _lease(base, space="dl-ok")

    # Mock CDP setDownloadBehavior recorded on lease
    assert pool._download_recorder.calls, "expected mock setDownloadBehavior record"
    call = pool._download_recorder.calls[-1]
    assert call["method"] == "Browser.setDownloadBehavior"
    assert call["behavior"] == "allow"
    assert str(lid) in call["download_path"]
    assert "downloads" in call["download_path"]
    # Absolute path must not leak via list API
    assert not call["download_path"].startswith("downloads/")

    body = b"hello-slipstream-download\n"
    meta = pool.record_mock_download(lid, "report.pdf", body)
    assert meta["filename"] == "report.pdf"
    assert meta["bytes"] == len(body)
    assert meta["sha256"] == hashlib.sha256(body).hexdigest()
    assert meta["id"].startswith("dl_")
    assert "path" not in meta or not meta["path"].startswith("/")

    code, listing, _ = _req("GET", f"{base}/v1/leases/{lid}/downloads")
    assert code == 200
    assert listing["total"] == 1
    item = listing["downloads"][0]
    assert item["filename"] == "report.pdf"
    assert item["bytes"] == len(body)
    assert item["sha256"] == meta["sha256"]
    assert item["id"] == meta["id"]
    # No absolute secret paths
    blob = json.dumps(listing)
    assert "/artifacts/" not in blob
    assert str(pool.config.artifacts_root) not in blob

    # lease-relative optional
    code, listing2, _ = _req(
        "GET", f"{base}/v1/leases/{lid}/downloads?rel_path=1"
    )
    assert code == 200
    assert listing2["downloads"][0]["path"] == "downloads/report.pdf"

    # fetch bytes
    code, raw, hdrs = _req(
        "GET", f"{base}/v1/leases/{lid}/downloads/{meta['id']}"
    )
    assert code == 200
    assert raw == body
    assert hdrs.get("X-Slipstream-Sha256") == meta["sha256"] or hdrs.get(
        "x-slipstream-sha256"
    ) == meta["sha256"]

    # metadata via Accept
    code, meta_json, _ = _req(
        "GET",
        f"{base}/v1/leases/{lid}/downloads/{meta['id']}",
        headers={"Accept": "application/json"},
    )
    assert code == 200
    assert meta_json["download"]["filename"] == "report.pdf"


def test_agent_ownership_and_not_found(api_server):
    server, pool = api_server
    base = server.base_url
    lid = _lease(base, space="dl-own", agent="owner-1")
    pool.record_mock_download(lid, "ok.txt", b"x")
    aid = artifact_id_for_filename("ok.txt", kind="downloads")

    code, err, _ = _req(
        "GET", f"{base}/v1/leases/{lid}/downloads?agent_id=other-agent"
    )
    assert code == 403

    code, err, _ = _req(
        "GET", f"{base}/v1/leases/{lid}/downloads/{aid}?agent_id=owner-1"
    )
    assert code == 200

    code, err, _ = _req(
        "GET", f"{base}/v1/leases/{lid}/downloads/dl_deadbeefdeadbeef"
    )
    assert code == 404


def test_symlink_and_path_escape_refused(api_server, tmp_path):
    server, pool = api_server
    base = server.base_url
    lid = _lease(base, space="dl-escape")
    ddir = pool.config.artifacts_root / "leases" / lid / "downloads"
    ddir.mkdir(parents=True, exist_ok=True)
    # Symlink to /etc/hosts (or tmp secret) — must not be listed or served
    target = tmp_path / "outside-secret.txt"
    target.write_text("secret-outside")
    link = ddir / "innocent.txt"
    link.symlink_to(target)
    code, listing, _ = _req("GET", f"{base}/v1/leases/{lid}/downloads")
    assert code == 200
    assert listing["total"] == 0

    # Denied filename dropped on disk — omitted from list
    (ddir / "password.txt").write_bytes(b"nope")
    code, listing, _ = _req("GET", f"{base}/v1/leases/{lid}/downloads")
    assert code == 200
    assert listing["total"] == 0


def test_upload_drop(api_server):
    server, _pool = api_server
    base = server.base_url
    lid = _lease(base, space="up-ok")
    import base64

    payload = b"upload-bytes-001"
    code, out, _ = _req(
        "POST",
        f"{base}/v1/leases/{lid}/uploads",
        {
            "filename": "drop.bin",
            "content_b64": base64.b64encode(payload).decode("ascii"),
            "agent_id": "agent-dl",
        },
    )
    assert code == 201, out
    assert out["upload"]["filename"] == "drop.bin"
    assert out["upload"]["bytes"] == len(payload)
    assert out["upload"]["id"].startswith("up_")

    code, listing, _ = _req("GET", f"{base}/v1/leases/{lid}/uploads")
    assert code == 200
    assert listing["total"] == 1

    aid = out["upload"]["id"]
    code, raw, _ = _req("GET", f"{base}/v1/leases/{lid}/uploads/{aid}")
    assert code == 200
    assert raw == payload

    # secret filename refused
    code, err, _ = _req(
        "POST",
        f"{base}/v1/leases/{lid}/uploads",
        {
            "filename": "api_key.txt",
            "content_b64": base64.b64encode(b"x").decode("ascii"),
        },
    )
    assert code == 400


def test_unit_read_path_containment(tmp_path):
    root = tmp_path / "downloads"
    root.mkdir()
    f = root / "a.txt"
    f.write_bytes(b"abc")
    items = list_artifacts(root, kind="downloads")
    assert len(items) == 1
    meta, data = read_artifact(root, items[0]["id"], kind="downloads")
    assert data == b"abc"
    with pytest.raises(ArtifactNotFoundError):
        read_artifact(root, "dl_0000000000000000", kind="downloads")
