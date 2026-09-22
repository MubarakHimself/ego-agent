"""Downloads as session artifacts — ADV-DL-001..005 + ADV-DL-010/011/013/014/015 path safety."""

from __future__ import annotations

import base64
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
    content_disposition_attachment,
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


def _aq(agent: str = "agent-dl") -> str:
    return f"agent_id={agent}"


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


def test_content_disposition_sanitized():
    hdr = content_disposition_attachment('evil"\r\nName.pdf')
    assert "\r" not in hdr and "\n" not in hdr
    assert "attachment" in hdr
    assert 'filename="' in hdr
    assert "filename*=UTF-8''" in hdr
    # quoted filename must not re-introduce raw quote/CRLF
    quoted = hdr.split('filename="', 1)[1].split('"', 1)[0]
    assert '"' not in quoted and "\r" not in quoted and "\n" not in quoted


def test_mock_recorder_and_list_get(api_server):
    server, pool = api_server
    base = server.base_url
    lid = _lease(base, space="dl-ok")

    assert pool._download_recorder.calls, "expected mock setDownloadBehavior record"
    call = pool._download_recorder.calls[-1]
    assert call["method"] == "Browser.setDownloadBehavior"
    assert call["behavior"] == "allow"
    assert str(lid) in call["download_path"]
    assert "downloads" in call["download_path"]

    body = b"hello-slipstream-download\n"
    meta = pool.record_mock_download(lid, "report.pdf", body)
    assert meta["filename"] == "report.pdf"
    assert meta["bytes"] == len(body)
    assert meta["sha256"] == hashlib.sha256(body).hexdigest()
    assert meta["id"].startswith("dl_")

    code, listing, _ = _req("GET", f"{base}/v1/leases/{lid}/downloads?{_aq()}")
    assert code == 200
    assert listing["total"] == 1
    item = listing["downloads"][0]
    assert item["filename"] == "report.pdf"
    assert item["sha256"] == meta["sha256"]
    blob = json.dumps(listing)
    assert "/artifacts/" not in blob
    assert str(pool.config.artifacts_root) not in blob

    code, listing2, _ = _req(
        "GET", f"{base}/v1/leases/{lid}/downloads?{_aq()}&rel_path=1"
    )
    assert code == 200
    assert listing2["downloads"][0]["path"] == "downloads/report.pdf"

    code, raw, hdrs = _req(
        "GET", f"{base}/v1/leases/{lid}/downloads/{meta['id']}?{_aq()}"
    )
    assert code == 200
    assert raw == body
    assert hdrs.get("X-Slipstream-Sha256") == meta["sha256"] or hdrs.get(
        "x-slipstream-sha256"
    ) == meta["sha256"]
    cd = hdrs.get("Content-Disposition") or hdrs.get("content-disposition") or ""
    assert "attachment" in cd
    assert "\r" not in cd and "\n" not in cd

    code, meta_json, _ = _req(
        "GET",
        f"{base}/v1/leases/{lid}/downloads/{meta['id']}?{_aq()}",
        headers={"Accept": "application/json"},
    )
    assert code == 200
    assert meta_json["download"]["filename"] == "report.pdf"


def test_adv_dl_001_agent_id_required(api_server):
    server, pool = api_server
    base = server.base_url
    lid = _lease(base, space="dl-own", agent="owner-1")
    pool.record_mock_download(lid, "ok.txt", b"x")
    aid = artifact_id_for_filename("ok.txt", kind="downloads")

    code, err, _ = _req("GET", f"{base}/v1/leases/{lid}/downloads")
    assert code in (401, 403), err

    code, err, _ = _req(
        "GET", f"{base}/v1/leases/{lid}/downloads?agent_id=other-agent"
    )
    assert code == 403

    code, ok, _ = _req(
        "GET", f"{base}/v1/leases/{lid}/downloads/{aid}?agent_id=owner-1"
    )
    assert code == 200, ok

    code, err, _ = _req(
        "GET",
        f"{base}/v1/leases/{lid}/downloads/dl_deadbeefdeadbeef?agent_id=owner-1",
    )
    assert code == 404

    code, err, _ = _req(
        "POST",
        f"{base}/v1/leases/{lid}/uploads",
        {
            "filename": "x.bin",
            "content_b64": base64.b64encode(b"z").decode("ascii"),
        },
    )
    assert code in (401, 403), err


def test_symlink_file_and_denied_name_refused(api_server, tmp_path):
    server, pool = api_server
    base = server.base_url
    lid = _lease(base, space="dl-escape")
    ddir = pool.config.artifacts_root / "leases" / lid / "downloads"
    ddir.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "outside-secret.txt"
    target.write_text("outside-data")
    (ddir / "innocent.txt").symlink_to(target)
    code, listing, _ = _req("GET", f"{base}/v1/leases/{lid}/downloads?{_aq()}")
    assert code == 200
    assert listing["total"] == 0

    (ddir / "password.txt").write_bytes(b"nope")
    code, listing, _ = _req("GET", f"{base}/v1/leases/{lid}/downloads?{_aq()}")
    assert code == 200
    assert listing["total"] == 0


def test_adv_dl_002_directory_symlink_containment(api_server, tmp_path):
    server, pool = api_server
    base = server.base_url
    lid_a = _lease(base, space="dl-a", agent="agent-a")
    lid_b = _lease(base, space="dl-b", agent="agent-b")
    pool.record_mock_download(lid_b, "peer-b.pdf", b"from-b")

    art = pool.config.artifacts_root
    vault = pool.config.vault_root
    vault.mkdir(parents=True, exist_ok=True)
    (vault / "creds.db").write_bytes(b"vault-secret")

    ddir_a = art / "leases" / lid_a / "downloads"
    udir_a = art / "leases" / lid_a / "uploads"
    ddir_b = art / "leases" / lid_b / "downloads"

    def _replace_with_symlink(path: Path, target: Path) -> None:
        if path.is_symlink():
            path.unlink()
        elif path.is_dir():
            for child in path.iterdir():
                if child.is_file() or child.is_symlink():
                    child.unlink()
                elif child.is_dir():
                    os.rmdir(child)
            path.rmdir()
        path.symlink_to(target)

    _replace_with_symlink(ddir_a, ddir_b)
    code, err, _ = _req(
        "GET", f"{base}/v1/leases/{lid_a}/downloads?agent_id=agent-a"
    )
    assert code in (400, 403), err

    _replace_with_symlink(ddir_a, vault)
    code, err, _ = _req(
        "GET", f"{base}/v1/leases/{lid_a}/downloads?agent_id=agent-a"
    )
    assert code in (400, 403), err

    _replace_with_symlink(udir_a, vault)
    code, err, _ = _req(
        "POST",
        f"{base}/v1/leases/{lid_a}/uploads",
        {
            "filename": "pwn.bin",
            "content_b64": base64.b64encode(b"pwn").decode("ascii"),
            "agent_id": "agent-a",
        },
    )
    assert code in (400, 403), err
    assert not (vault / "pwn.bin").exists()


def test_adv_dl_003_status_hides_artifacts_root(api_server):
    server, pool = api_server
    st = pool.status()
    assert "artifacts_root" not in st
    assert st.get("artifacts_configured") is True
    code, body, _ = _req("GET", f"{server.base_url}/v1/pool/status")
    assert code == 200
    assert "artifacts_root" not in body
    assert str(pool.config.artifacts_root) not in json.dumps(body)


def test_adv_dl_004_upload_excl_unique(api_server):
    server, _pool = api_server
    base = server.base_url
    lid = _lease(base, space="up-excl")
    payload = b"upload-bytes-001"
    body = {
        "filename": "drop.bin",
        "content_b64": base64.b64encode(payload).decode("ascii"),
        "agent_id": "agent-dl",
    }
    code, out, _ = _req("POST", f"{base}/v1/leases/{lid}/uploads", body)
    assert code == 201, out
    assert out["upload"]["filename"] == "drop.bin"

    code, out2, _ = _req(
        "POST",
        f"{base}/v1/leases/{lid}/uploads",
        {
            "filename": "drop.bin",
            "content_b64": base64.b64encode(b"other").decode("ascii"),
            "agent_id": "agent-dl",
        },
    )
    assert code == 201, out2
    assert out2["upload"]["filename"] != "drop.bin"
    assert out2["upload"]["filename"].startswith("drop-")
    assert out2["upload"]["id"] != out["upload"]["id"]


def test_upload_drop(api_server):
    server, _pool = api_server
    base = server.base_url
    lid = _lease(base, space="up-ok")

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

    code, listing, _ = _req("GET", f"{base}/v1/leases/{lid}/uploads?{_aq()}")
    assert code == 200
    assert listing["total"] == 1

    aid = out["upload"]["id"]
    code, raw, _ = _req("GET", f"{base}/v1/leases/{lid}/uploads/{aid}?{_aq()}")
    assert code == 200
    assert raw == payload

    code, err, _ = _req(
        "POST",
        f"{base}/v1/leases/{lid}/uploads",
        {
            "filename": "api_key.txt",
            "content_b64": base64.b64encode(b"x").decode("ascii"),
            "agent_id": "agent-dl",
        },
    )
    assert code == 400


def test_unit_read_path_containment(tmp_path):
    art = tmp_path / "artifacts"
    art.mkdir()
    lease = "lease-unit1"
    from slipstream.downloads import ensure_lease_artifact_dirs, write_upload

    ensure_lease_artifact_dirs(art, lease)
    write_upload(
        filename="a.txt",
        data=b"abc",
        kind="downloads",
        artifacts_root=art,
        lease_id=lease,
    )
    items = list_artifacts(
        kind="downloads", artifacts_root=art, lease_id=lease
    )
    assert len(items) == 1
    meta, data = read_artifact(
        artifact_id=items[0]["id"],
        kind="downloads",
        artifacts_root=art,
        lease_id=lease,
    )
    assert data == b"abc"
    with pytest.raises(ArtifactNotFoundError):
        read_artifact(
            artifact_id="dl_0000000000000000",
            kind="downloads",
            artifacts_root=art,
            lease_id=lease,
        )


def test_adv_dl_010_lease_id_symlink_refused(api_server, tmp_path):
    """ADV-DL-010: leases/{id} symlink → vault / other lease refused on list/get/put."""
    server, pool = api_server
    base = server.base_url
    lid_a = _lease(base, space="dl-sym-a", agent="agent-a")
    lid_b = _lease(base, space="dl-sym-b", agent="agent-b")
    pool.record_mock_download(lid_b, "peer-b.pdf", b"from-b")

    art = pool.config.artifacts_root
    vault = pool.config.vault_root
    vault.mkdir(parents=True, exist_ok=True)
    (vault / "creds.db").write_bytes(b"vault-secret")

    lease_a = art / "leases" / lid_a
    lease_b = art / "leases" / lid_b

    def _replace_dir_with_symlink(path: Path, target: Path) -> None:
        if path.is_symlink():
            path.unlink()
        elif path.is_dir():
            for child in list(path.iterdir()):
                if child.is_symlink() or child.is_file():
                    child.unlink()
                elif child.is_dir():
                    for gc in list(child.iterdir()):
                        if gc.is_file() or gc.is_symlink():
                            gc.unlink()
                        elif gc.is_dir():
                            os.rmdir(gc)
                    os.rmdir(child)
            path.rmdir()
        path.symlink_to(target)

    # Cross-lease: leases/{a} → leases/{b}
    _replace_dir_with_symlink(lease_a, lease_b)
    code, err, _ = _req(
        "GET", f"{base}/v1/leases/{lid_a}/downloads?agent_id=agent-a"
    )
    assert code in (400, 403), err

    # Re-create real lease_a then point at vault
    if lease_a.is_symlink():
        lease_a.unlink()
    # ensure recreates real dirs
    from slipstream.downloads import ensure_lease_artifact_dirs

    # vault symlink case: plant after ensure
    ensure_lease_artifact_dirs(art, lid_a)
    _replace_dir_with_symlink(lease_a, vault)
    code, err, _ = _req(
        "GET", f"{base}/v1/leases/{lid_a}/downloads?agent_id=agent-a"
    )
    assert code in (400, 403), err

    # get should also refuse
    code, err, _ = _req(
        "GET",
        f"{base}/v1/leases/{lid_a}/downloads/dl_deadbeefdeadbeef?agent_id=agent-a",
    )
    assert code in (400, 403, 404), err
    assert code != 200

    # put must not write into vault via lease_id symlink
    code, err, _ = _req(
        "POST",
        f"{base}/v1/leases/{lid_a}/uploads",
        {
            "filename": "pwn.bin",
            "content_b64": base64.b64encode(b"pwn").decode("ascii"),
            "agent_id": "agent-a",
        },
    )
    assert code in (400, 403), err
    assert not (vault / "pwn.bin").exists()
    assert not (vault / "uploads" / "pwn.bin").exists()


def test_adv_dl_011_leases_dir_symlink_refused(api_server, tmp_path):
    """ADV-DL-011: artifacts/leases → /tmp symlink refused on list/get/put."""
    server, pool = api_server
    base = server.base_url
    lid = _lease(base, space="dl-leases-sym", agent="agent-dl")

    art = pool.config.artifacts_root
    leases = art / "leases"
    escape = tmp_path / "outside-leases"
    escape.mkdir()

    # Move real leases aside and plant symlink to /tmp-ish outside tree
    real = art / "leases.real"
    if leases.exists() and not leases.is_symlink():
        leases.rename(real)
    elif leases.is_symlink():
        leases.unlink()
    leases.symlink_to(escape)

    code, err, _ = _req(
        "GET", f"{base}/v1/leases/{lid}/downloads?{_aq()}"
    )
    assert code in (400, 403), err

    code, err, _ = _req(
        "POST",
        f"{base}/v1/leases/{lid}/uploads",
        {
            "filename": "escape.bin",
            "content_b64": base64.b64encode(b"nope").decode("ascii"),
            "agent_id": "agent-dl",
        },
    )
    assert code in (400, 403), err
    assert not (escape / lid / "uploads" / "escape.bin").exists()
    assert not list(escape.rglob("escape.bin"))


def _replace_dir_with_symlink(path: Path, target: Path) -> None:
    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        for child in list(path.iterdir()):
            if child.is_symlink() or child.is_file():
                child.unlink()
            elif child.is_dir():
                for gc in list(child.iterdir()):
                    if gc.is_file() or gc.is_symlink():
                        gc.unlink()
                    elif gc.is_dir():
                        os.rmdir(gc)
                os.rmdir(child)
        path.rmdir()
    path.symlink_to(target)


def test_adv_dl_013_post_walk_symlink_swap_refused(api_server, tmp_path):
    """ADV-DL-013: intermediate symlink after walk must not cross-lease read/write."""
    from slipstream.downloads import (
        ensure_lease_artifact_dirs,
        list_artifacts,
        read_artifact,
        write_upload,
        DownloadValidationError,
    )
    from slipstream.artifact_walk import walk_lease_kind_dir

    server, pool = api_server
    base = server.base_url
    lid_a = _lease(base, space="dl-toctou-a", agent="agent-a")
    lid_b = _lease(base, space="dl-toctou-b", agent="agent-b")
    pool.record_mock_download(lid_b, "peer-b.pdf", b"PEER-BYTES")

    art = pool.config.artifacts_root
    vault = pool.config.vault_root
    vault.mkdir(parents=True, exist_ok=True)
    (vault / "creds.db").write_bytes(b"vault-secret")

    # Hold kind_fd for A; rename A aside + plant symlink → B (fd keeps inode)
    handle = walk_lease_kind_dir(art, lid_a, "downloads", create=True)
    try:
        lease_a = art / "leases" / lid_a
        lease_b = art / "leases" / lid_b
        aside = art / "leases" / f"{lid_a}.aside"
        lease_a.rename(aside)
        lease_a.symlink_to(lease_b)
        # pathname under leases/A would see peer; held fd must NOT
        names = handle.listdir()
        assert "peer-b.pdf" not in names
        # fresh walk after swap must refuse
        with pytest.raises(DownloadValidationError):
            walk_lease_kind_dir(art, lid_a, "downloads", create=False).close()
    finally:
        handle.close()

    # HTTP list/get after swap refuse (no peer bytes)
    code, err, _ = _req(
        "GET", f"{base}/v1/leases/{lid_a}/downloads?agent_id=agent-a"
    )
    assert code in (400, 403), err

    # Recreate A, swap to vault, write must not land in vault
    if (art / "leases" / lid_a).is_symlink():
        (art / "leases" / lid_a).unlink()
    ensure_lease_artifact_dirs(art, lid_a)
    _replace_dir_with_symlink(art / "leases" / lid_a, vault)
    code, err, _ = _req(
        "POST",
        f"{base}/v1/leases/{lid_a}/uploads",
        {
            "filename": "pwn.bin",
            "content_b64": base64.b64encode(b"pwn").decode("ascii"),
            "agent_id": "agent-a",
        },
    )
    assert code in (400, 403), err
    assert not (vault / "pwn.bin").exists()
    assert not (vault / "uploads" / "pwn.bin").exists()

    # Direct API TOCTOU: walk A, swap to B mid-flight still uses held fd
    if (art / "leases" / lid_a).is_symlink():
        (art / "leases" / lid_a).unlink()
    ensure_lease_artifact_dirs(art, lid_a)
    write_upload(
        filename="own.txt",
        data=b"OWN",
        kind="downloads",
        artifacts_root=art,
        lease_id=lid_a,
    )
    # Simulate concurrent swap during read path by swapping before API call
    # (API re-walks and must refuse rather than serve peer)
    _replace_dir_with_symlink(art / "leases" / lid_a, art / "leases" / lid_b)
    with pytest.raises(DownloadValidationError):
        list_artifacts(kind="downloads", artifacts_root=art, lease_id=lid_a)
    with pytest.raises(DownloadValidationError):
        read_artifact(
            artifact_id="dl_0000000000000000",
            kind="downloads",
            artifacts_root=art,
            lease_id=lid_a,
        )


def test_adv_dl_014_cdp_path_nofollow_after_swap(api_server, tmp_path):
    """ADV-DL-014: CDP downloadPath must not Path.resolve into vault after swap."""
    from slipstream.downloads import ensure_lease_artifact_dirs
    from slipstream.download_cdp import (
        MockDownloadRecorder,
        configure_chrome_download_behavior,
    )
    from slipstream.downloads import DownloadValidationError

    server, pool = api_server
    base = server.base_url
    lid = _lease(base, space="dl-cdp-swap", agent="agent-dl")
    art = pool.config.artifacts_root
    vault = pool.config.vault_root
    vault.mkdir(parents=True, exist_ok=True)
    (vault / "downloads").mkdir(exist_ok=True)

    ensure_lease_artifact_dirs(art, lid)
    # Plant lease → vault after ensure (classic TOCTOU)
    _replace_dir_with_symlink(art / "leases" / lid, vault)

    rec = MockDownloadRecorder()
    with pytest.raises(DownloadValidationError):
        configure_chrome_download_behavior(
            "http://127.0.0.1:9",
            artifacts_root=art,
            lease_id=lid,
            mock=True,
            recorder=rec,
        )
    assert not rec.calls
    # No recorded path under vault
    for c in rec.calls:
        assert str(vault) not in c["download_path"]


def test_adv_dl_015_dir_path_only_refused(tmp_path):
    """ADV-DL-015: dir_path-only list/read/write skip ancestor walk — refused."""
    from slipstream.downloads import (
        DownloadValidationError,
        ensure_lease_artifact_dirs,
        list_artifacts,
        read_artifact,
        write_upload,
    )

    art = tmp_path / "artifacts"
    art.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    lease = "leaseC"
    ensure_lease_artifact_dirs(art, lease)
    # Plant leases → outside
    leases = art / "leases"
    real = art / "leases.real"
    leases.rename(real)
    leases.symlink_to(outside)

    # Walk-based ensure refuses
    with pytest.raises(DownloadValidationError):
        ensure_lease_artifact_dirs(art, lease)

    # dir_path-only must also refuse (not write under outside)
    planted = art / "leases" / lease / "uploads"
    # recreate path appearance via the symlink
    planted.mkdir(parents=True, exist_ok=True)
    with pytest.raises(DownloadValidationError):
        write_upload(
            planted,
            filename="via-dirpath.bin",
            data=b"nope",
        )
    assert not (outside / lease / "uploads" / "via-dirpath.bin").exists()
    with pytest.raises(DownloadValidationError):
        list_artifacts(planted, kind="uploads")
    with pytest.raises(DownloadValidationError):
        read_artifact(planted, "up_0000000000000000", kind="uploads")
