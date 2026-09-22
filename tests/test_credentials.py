"""Credential vault + fill API — mock vault + mock CDP inject; no plaintext to agent."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

import pytest

from slipstream import __main__ as mainmod
from slipstream.api import PoolServer
from slipstream.cdp_inject import MockCdpInjector
from slipstream.config import PoolConfig
from slipstream.cli import _validate_api_request_url
from tests.conftest import grant_ladder
from slipstream.pool import BrowserPool
from slipstream.vault import (
    CredNotFoundError,
    CredVault,
    VaultUnavailableError,
    VaultValidationError,
    material_for_field,
    origins_match,
    parse_fill_body,
)


@pytest.fixture
def api_server(tmp_path):
    cfg = PoolConfig(
        K=3,
        W=1,
        spaces_root=tmp_path / "spaces",
        vault_root=tmp_path / "vault",
        cdp_base_port=19622,
        mock=True,
        host="127.0.0.1",
        port=18790,
    )
    pool = BrowserPool(cfg)
    pool._cdp_injector = MockCdpInjector(current_url="https://github.com/login")
    server = PoolServer(pool, port=18790)
    server.start(background=True)
    yield server
    server.stop()


def _req(method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
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


def test_vault_outside_spaces_root(tmp_path):
    spaces = tmp_path / "spaces"
    vault = tmp_path / "vault"
    cfg = PoolConfig(spaces_root=spaces, vault_root=vault, mock=True, K=1)
    pool = BrowserPool(cfg)
    assert pool.config.vault_root.resolve() != pool.config.spaces_root.resolve()
    space_path = pool.config.space_path("s1")
    assert vault.resolve() not in space_path.resolve().parents


def test_vault_bind_list_unbind_no_plaintext(tmp_path):
    v = CredVault(tmp_path / "v", mock=True)
    ack = v.bind(
        "space-a",
        label="work-gh",
        origin="https://github.com",
        username="alice",
        secret="hunter2",
    )
    assert ack["bound"] is True
    assert "secret" not in ack and "username" not in ack
    listing = v.list_metadata("space-a")
    assert listing["items"][0]["has_secret"] is True
    assert "hunter2" not in json.dumps(listing)
    assert "alice" not in json.dumps(listing)
    unlocked = v.unlock_for_fill("space-a", ack["cred_id"])
    assert unlocked["username"] == "alice" and unlocked["secret"] == "hunter2"
    v.unbind("space-a", ack["cred_id"])
    with pytest.raises(CredNotFoundError):
        v.unlock_for_fill("space-a", ack["cred_id"])


def test_vault_refuse_free_read(tmp_path):
    v = CredVault(tmp_path / "v", mock=True)
    with pytest.raises(VaultValidationError, match="free-read"):
        v.refuse_free_read()
    with pytest.raises(VaultValidationError, match="cookie"):
        v.refuse_cookie_dump()


def test_parse_fill_refuses_secret_in_body():
    with pytest.raises(VaultValidationError, match="password"):
        parse_fill_body(
            {"cred_id": "c1", "password": "x", "fields": {"username": "#u"}}
        )
    parsed = parse_fill_body(
        {"cred_id": "c1", "fields": {"username": "#user", "password": "#pass"}}
    )
    assert parsed["fields"]["password"] == "#pass"
    assert material_for_field("password", {"username": "a", "secret": "b"}) == "b"


def test_http_bind_list_fill_unbind(api_server: PoolServer):
    base = api_server.base_url
    code, bind = _req(
        "POST",
        f"{base}/v1/spaces/s1/credentials/bind",
        {
            "label": "gh",
            "origin": "https://github.com",
            "username": "bob",
            "secret": "sekrit",
        },
    )
    assert code == 200
    assert bind["bound"] is True
    assert "sekrit" not in json.dumps(bind)

    code, listing = _req("GET", f"{base}/v1/spaces/s1/credentials")
    assert code == 200
    assert listing["items"][0]["cred_id"] == bind["cred_id"]
    assert "sekrit" not in json.dumps(listing)
    assert "bob" not in json.dumps(listing)

    code, lease = _req(
        "POST", f"{base}/v1/leases", {"agent_id": "agent-1", "space_id": "s1"}
    )
    assert code == 200
    lid = lease["lease_id"]

    grant_ladder(base, lid, "fill", "fill login fields")
    code, fill = _req(
        "POST",
        f"{base}/v1/leases/{lid}/credentials/fill",
        {
            "cred_id": bind["cred_id"],
            "fields": {"username": "#login_field", "password": "#pass_field"},
        },
    )
    assert code == 200
    assert fill["ok"] is True
    assert fill["filled"] == ["username", "password"]
    assert "sekrit" not in json.dumps(fill)

    inj = api_server.pool._cdp_injector
    assert isinstance(inj, MockCdpInjector)
    assert len(inj.calls) == 2
    assert inj.calls[0]["selector"] == "#login_field"
    assert inj.calls[0]["value_len"] == 3  # bob
    assert inj.calls[1]["value_len"] == 6  # sekrit
    assert "sekrit" not in json.dumps(inj.calls)

    code, refused = _req("POST", f"{base}/v1/spaces/s1/credentials/secret", {})
    assert code == 404
    assert refused["error"] == "refused"

    code, cookies = _req("GET", f"{base}/v1/spaces/s1/credentials/cookies")
    assert code == 404
    assert cookies["error"] == "refused"

    code, ub = _req(
        "POST",
        f"{base}/v1/spaces/s1/credentials/{bind['cred_id']}/unbind",
        {},
    )
    assert code == 200
    assert ub["unbound"] is True


def test_fill_unknown_cred_404(api_server: PoolServer):
    base = api_server.base_url
    code, lease = _req(
        "POST", f"{base}/v1/leases", {"agent_id": "a", "space_id": "sx"}
    )
    assert code == 200
    lid = lease["lease_id"]
    # Ladder refuses before vault when no confirm
    code, err = _req(
        "POST",
        f"{base}/v1/leases/{lid}/credentials/fill",
        {"cred_id": "cred_missing", "fields": {"username": "#u"}},
    )
    assert code == 403
    assert err["error"] == "confirmation_required"
    grant_ladder(base, lid, "fill", "attempt missing cred")
    code, err = _req(
        "POST",
        f"{base}/v1/leases/{lid}/credentials/fill",
        {"cred_id": "cred_missing", "fields": {"username": "#u"}},
    )
    assert code == 404
    assert err["error"] == "cred_not_found"


def test_cli_cred_bind_list_fill(api_server: PoolServer):
    base = api_server.base_url

    def run(argv: list[str]) -> tuple[int, str, str]:
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = mainmod.main(argv)
        return code, out.getvalue(), err.getvalue()

    import os

    os.environ["SLIPSTREAM_BIND_SECRET"] = "p"
    code, out, err = run(
        [
            "cred",
            "bind",
            "--url",
            base,
            "--space-id",
            "cli-space",
            "--label",
            "l",
            "--origin",
            "https://github.com",
            "--username",
            "u",
            "--secret-env",
            "SLIPSTREAM_BIND_SECRET",
        ]
    )
    assert code == 0, err
    bind = json.loads(out)
    assert bind["bound"] is True

    code, out, err = run(["cred", "list", "--url", base, "--space-id", "cli-space"])
    assert code == 0, err
    assert json.loads(out)["items"][0]["cred_id"] == bind["cred_id"]

    code, out, err = run(
        [
            "lease",
            "--url",
            base,
            "--agent-id",
            "cli-agent",
            "--space-id",
            "cli-space",
        ]
    )
    assert code == 0, err
    lease = json.loads(out)
    grant_ladder(base, lease["lease_id"], "fill", "cli fill")

    fields = json.dumps({"username": "#u", "password": "#p"})
    code, out, err = run(
        [
            "cred",
            "fill",
            "--url",
            base,
            "--lease-id",
            lease["lease_id"],
            "--cred-id",
            bind["cred_id"],
            "--fields",
            fields,
        ]
    )
    assert code == 0, err
    fill = json.loads(out)
    assert fill["ok"] is True
    assert fill["filled"] == ["username", "password"]

    code, out, err = run(
        [
            "cred",
            "unbind",
            "--url",
            base,
            "--space-id",
            "cli-space",
            "--cred-id",
            bind["cred_id"],
        ]
    )
    assert code == 0, err
    assert json.loads(out)["unbound"] is True


def test_need_human_login_path(api_server: PoolServer):
    """Login/2FA → need_human(reason=login); no secrets in alert payload."""
    base = api_server.base_url
    code, lease = _req(
        "POST", f"{base}/v1/leases", {"agent_id": "a", "space_id": "login-space"}
    )
    assert code == 200
    code, env = _req(
        "POST",
        f"{base}/v1/leases/{lease['lease_id']}/alerts",
        {"event": "need_human", "reason": "login", "detail": "2FA prompt visible"},
    )
    assert code == 200
    assert env["alert"]["reason"] == "login"
    assert env["harness"]["action"] == "pause"
    blob = json.dumps(env).lower()
    assert "password=" not in blob
    assert "sekrit" not in blob


def test_vault_inside_spaces_fails(tmp_path):
    """ADV-002: vault_root under spaces_root fail-closed."""
    spaces = tmp_path / "spaces"
    spaces.mkdir()
    vault = spaces / "vault"
    cfg = PoolConfig(spaces_root=spaces, vault_root=vault, mock=True, K=1)
    with pytest.raises(ValueError, match="outside"):
        BrowserPool(cfg)


def test_vault_posix_permissions(tmp_path):
    """ADV-004: vault dir 0o700; bindings 0o600 (POSIX)."""
    import os
    import stat

    if os.name == "nt":
        pytest.skip("POSIX modes")
    v = CredVault(tmp_path / "v", mock=True)
    v.bind(
        "s",
        label="l",
        origin="https://example.com",
        username="u",
        secret="x",
    )
    mode_dir = stat.S_IMODE(v.vault_root.stat().st_mode)
    assert mode_dir == 0o700
    mode_meta = stat.S_IMODE(v._meta_path.stat().st_mode)
    assert mode_meta == 0o600


def test_parse_fill_recursive_secret_reject():
    """ADV-007: nested secret-like keys refused."""
    with pytest.raises(VaultValidationError, match="secret"):
        parse_fill_body(
            {
                "cred_id": "c1",
                "fields": {"username": "#u"},
                "extra": {"token": "leak"},
            }
        )
    with pytest.raises(VaultValidationError, match="authorization|refused"):
        parse_fill_body(
            {"cred_id": "c1", "authorization": "Bearer x", "fields": {"username": "#u"}}
        )


def test_vault_key_refused_outside_mock(tmp_path, monkeypatch):
    """ADV-011: SLIPSTREAM_VAULT_KEY refused unless mock or allow."""
    monkeypatch.setenv("SLIPSTREAM_VAULT_KEY", "dGVzdC1rZXktbm90LXJlYWwtZmVybmV0LWtleSE=")
    monkeypatch.delenv("SLIPSTREAM_ALLOW_VAULT_KEY", raising=False)
    monkeypatch.delenv("SLIPSTREAM_MOCK", raising=False)
    with pytest.raises(VaultUnavailableError, match="VAULT_KEY refused"):
        CredVault(tmp_path / "v", mock=False)


def test_origins_match_helper():
    assert origins_match("https://github.com", "https://github.com/login")
    assert not origins_match("https://github.com", "https://evil.example/login")


def test_fill_origin_mismatch_400(api_server: PoolServer):
    """ADV-010: fill refuses when page origin != cred.origin."""
    base = api_server.base_url
    inj = api_server.pool._cdp_injector
    assert isinstance(inj, MockCdpInjector)
    code, bind = _req(
        "POST",
        f"{base}/v1/spaces/orig/credentials/bind",
        {
            "label": "gh",
            "origin": "https://github.com",
            "username": "bob",
            "secret": "sekrit",
        },
    )
    assert code == 200
    code, lease = _req(
        "POST", f"{base}/v1/leases", {"agent_id": "a", "space_id": "orig"}
    )
    assert code == 200
    inj.current_url = "https://evil.example/phishing"
    grant_ladder(base, lease["lease_id"], "fill", "origin mismatch attempt")
    code, err = _req(
        "POST",
        f"{base}/v1/leases/{lease['lease_id']}/credentials/fill",
        {
            "cred_id": bind["cred_id"],
            "fields": {"username": "#u", "password": "#p"},
        },
    )
    assert code == 400
    assert err["error"] == "invalid_credentials"
    assert "origin mismatch" in err["detail"]


def test_cli_refuses_bare_secret_argv(api_server: PoolServer, monkeypatch):
    """ADV-005: bare --secret refused without allow flag."""
    monkeypatch.delenv("SLIPSTREAM_ALLOW_SECRET_ARGV", raising=False)
    out, err = __import__("io").StringIO(), __import__("io").StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mainmod.main(
            [
                "cred",
                "bind",
                "--url",
                api_server.base_url,
                "--space-id",
                "x",
                "--label",
                "l",
                "--origin",
                "https://github.com",
                "--username",
                "u",
                "--secret",
                "nope",
            ]
        )
    assert code == 2
    assert "refusing bare --secret" in err.getvalue()
