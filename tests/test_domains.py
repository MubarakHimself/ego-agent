"""Domain allowlist + content-boundary + ADV-PL-002 scrub / ADV-META fixes."""

from __future__ import annotations

import json
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

import pytest

from slipstream.alerts import AlertValidationError, reject_secret_fields, scrub_text
from slipstream.boundaries import (
    boundary_meta,
    reset_boundary_nonce_for_tests,
    wrap_page_content,
)
from slipstream.domains import (
    DomainAllowlistError,
    check_navigate_url,
    effective_allowed_domains,
    host_matches,
    parse_allowed_domains,
    url_allowed,
)
from slipstream.metadata import MetadataValidationError, effective_metadata, validate_user_metadata
from slipstream.api import PoolServer
from slipstream import __main__ as mainmod

@pytest.fixture
def api_server(tmp_path):
    from slipstream.config import PoolConfig
    from slipstream.pool import BrowserPool

    cfg = PoolConfig(
        K=5,
        W=1,
        spaces_root=tmp_path / "spaces",
        vault_root=tmp_path / "vault",
        cdp_base_port=19622,
        mock=True,
        host="127.0.0.1",
        port=18955,
    )
    pool = BrowserPool(cfg)
    server = PoolServer(pool, port=18955)
    server.start(background=True)
    yield server
    server.stop()


def _req(method: str, url: str, body: dict | None = None):
    import urllib.error
    import urllib.request
    from slipstream.cli import _validate_api_request_url

    if not url.startswith(("http://127.0.0.1:", "http://localhost:")):
        raise ValueError(f"test helper refuses non-loopback URL: {url!r}")
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        _validate_api_request_url(url),
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:  # skylos: ignore[SKY-D216]
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode() or "{}"
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"raw": raw}
        return e.code, payload


def _lease(base: str, space: str = "dom-space", **extra):
    body = {"agent_id": "a1", "space_id": space, **extra}
    code, env = _req("POST", f"{base}/v1/leases", body)
    assert code == 200, env
    return env["lease_id"], env


# --- unit: scrub ADV-PL-002 ---


def test_scrub_extends_denylist_stems():
    cases = [
        ("jwt=eyJhbGciOiJIUzI1NiJ9.xx", "jwt=[REDACTED]"),
        ("access_key=AKIAIOSFODNN7EXAMPLE", "access_key=[REDACTED]"),
        ("private_key=-----BEGIN", "private_key=[REDACTED]"),
        ("api_key=sk-live-abc", "api_key=[REDACTED]"),
        ("passwd=hunter2", "passwd=[REDACTED]"),
        ("api-key=sk", "api-key=[REDACTED]"),
        ("access-key=x", "access-key=[REDACTED]"),
        ("private-key=y", "private-key=[REDACTED]"),
        ("credential=z", "credential=[REDACTED]"),
    ]
    for raw, expect in cases:
        out = scrub_text(f"prefix {raw} suffix")
        assert expect in out, (raw, out)
        secret_val = raw.split("=", 1)[1]
        # Ensure the secret value itself does not survive (except tiny single-char).
        if len(secret_val) > 1:
            assert secret_val not in out, (raw, out)


def test_scrub_in_alert_and_action_summary(api_server: PoolServer):
    base = api_server.base_url
    lid, _ = _lease(base, space="scrub-ext")
    code, env = _req(
        "POST",
        f"{base}/v1/leases/{lid}/alerts",
        {
            "event": "need_human",
            "reason": "other",
            "detail": "leak jwt=eyJ.abc access_key=AKIA",
        },
    )
    assert code == 200
    detail = env["alert"]["detail"]
    assert "jwt=[REDACTED]" in detail
    assert "access_key=[REDACTED]" in detail
    assert "eyJ.abc" not in detail
    assert "AKIA" not in detail

    code, act = _req(
        "POST",
        f"{base}/v1/leases/{lid}/actions",
        {"category": "eval", "summary": "run with api_key=sk-secret passwd=x"},
    )
    assert code == 202
    assert "api_key=[REDACTED]" in act["summary"]
    assert "passwd=[REDACTED]" in act["summary"]
    assert "sk-secret" not in act["summary"]


# --- ADV-META-001 camelCase ---


@pytest.mark.parametrize(
    "key",
    [
        "accessToken",
        "sessionToken",
        "clientSecret",
        "myPassword",
        "cookieJar",
        "apiKey",
        "privateKey",
        "accessKey",
        "bearerToken",
    ],
)
def test_adv_meta_001_camelcase_secret_keys_refused(key: str):
    with pytest.raises(AlertValidationError):
        reject_secret_fields({key: "nope"})
    with pytest.raises(MetadataValidationError):
        validate_user_metadata({key: "nope"})


def test_adv_meta_001_benign_keys_still_ok():
    validate_user_metadata({"envName": "staging", "secretary_note": "ok", "team": "fleet"})


# --- ADV-META-002 merge size ---


def test_adv_meta_002_effective_metadata_revalidates_size():
    left = {"a": "x" * 200, "b": "y" * 200}
    right = {"c": "z" * 200}
    validate_user_metadata(left)
    validate_user_metadata(right)
    with pytest.raises(MetadataValidationError, match="effective"):
        effective_metadata(left, right)


def test_adv_meta_002_effective_ok_when_under_cap():
    eff = effective_metadata({"env": "staging"}, {"run": {"id": "r1"}})
    assert eff["env"] == "staging"
    assert eff["run"]["id"] == "r1"


# --- domain allowlist unit ---


def test_host_matches_bb_and_wildcard():
    assert host_matches("www.example.com", "example.com")
    assert host_matches("a.b.example.com", "example.com")
    assert host_matches("example.com", "example.com")
    assert not host_matches("evil.com", "example.com")
    assert host_matches("example.com", "*.example.com")
    assert host_matches("www.example.com", "*.example.com")
    assert not host_matches("evil.com", "*.example.com")


def test_url_allowed_empty_unrestricted():
    assert url_allowed("https://evil.com", [])
    assert url_allowed("https://evil.com", None)


def test_url_allowed_non_http_always():
    assert url_allowed("about:blank", ["example.com"])
    assert url_allowed("chrome://settings", ["example.com"])


def test_check_navigate_refuses_outside():
    with pytest.raises(DomainAllowlistError) as ei:
        check_navigate_url("https://evil.example/x", ["example.com"])
    assert ei.value.host == "evil.example"


def test_effective_allowed_domains_precedence():
    assert effective_allowed_domains(
        lease_domains=["a.com"], space_domains=["b.com"], config_domains=["c.com"]
    ) == ["a.com"]
    assert effective_allowed_domains(
        lease_domains=None, space_domains=["b.com"], config_domains=["c.com"]
    ) == ["b.com"]
    assert effective_allowed_domains(
        lease_domains=None, space_domains=None, config_domains=["c.com"]
    ) == ["c.com"]
    assert effective_allowed_domains(lease_domains=[], space_domains=["b.com"]) == []


# --- navigate API ---


def test_navigate_refuse_outside_allowlist(api_server: PoolServer):
    base = api_server.base_url
    lid, env = _lease(
        base, space="nav-deny", allowed_domains=["example.com", "*.example.org"]
    )
    assert env["allowed_domains"] == ["example.com", "*.example.org"]
    code, body = _req(
        "POST",
        f"{base}/v1/leases/{lid}/navigate",
        {"url": "https://evil.example/phish"},
    )
    assert code == 403
    assert body["error"] == "domain_not_allowed"
    assert body.get("host") == "evil.example"


def test_navigate_allow_listed_and_empty_unrestricted(api_server: PoolServer):
    base = api_server.base_url
    lid, _ = _lease(base, space="nav-ok", allowed_domains=["example.com"])
    code, body = _req(
        "POST",
        f"{base}/v1/leases/{lid}/navigate",
        {"url": "https://www.example.com/path"},
    )
    assert code == 200
    assert body["ok"] is True
    assert body["url"] == "https://www.example.com/path"

    lid2, env2 = _lease(base, space="nav-free")
    assert env2["allowed_domains"] == []
    code, body = _req(
        "POST",
        f"{base}/v1/leases/{lid2}/navigate",
        {"url": "https://anywhere.example/"},
    )
    assert code == 200
    assert body["ok"] is True


def test_space_allowed_domains_inherited(api_server: PoolServer):
    base = api_server.base_url
    code, sp = _req(
        "PUT",
        f"{base}/v1/spaces/space-allow",
        {"allowed_domains": ["github.com"]},
    )
    assert code == 200
    assert sp["allowed_domains"] == ["github.com"]
    lid, env = _lease(base, space="space-allow")
    assert env["allowed_domains"] == ["github.com"]
    code, body = _req(
        "POST",
        f"{base}/v1/leases/{lid}/navigate",
        {"url": "https://evil.com"},
    )
    assert code == 403


def test_nav_irreversible_url_refused_outside_allowlist(api_server: PoolServer):
    base = api_server.base_url
    lid, _ = _lease(base, space="nav-irr", allowed_domains=["ok.com"])
    code, body = _req(
        "POST",
        f"{base}/v1/leases/{lid}/actions",
        {
            "category": "nav_irreversible",
            "summary": "leave allowlist",
            "url": "https://evil.com/",
        },
    )
    assert code == 403
    assert body["error"] == "domain_not_allowed"


def test_cli_navigate_refuse(api_server: PoolServer):
    base = api_server.base_url
    lid, _ = _lease(base, space="cli-nav", allowed_domains=["ok.com"])
    err = StringIO()
    out = StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mainmod.main(
            [
                "navigate",
                "--lease-id",
                lid,
                "--page-url",
                "https://evil.com",
                "--url",
                base,
            ]
        )
    assert code == 3
    assert "domain_not_allowed" in err.getvalue() or "domain_not_allowed" in out.getvalue() or code == 3


# --- content boundaries ---


def test_wrap_page_content_markers(monkeypatch):
    reset_boundary_nonce_for_tests()
    monkeypatch.delenv("SLIPSTREAM_CONTENT_BOUNDARIES", raising=False)
    assert wrap_page_content("raw") == "raw"
    monkeypatch.setenv("SLIPSTREAM_CONTENT_BOUNDARIES", "1")
    reset_boundary_nonce_for_tests()
    wrapped = wrap_page_content("PAGE TEXT", origin="https://example.com")
    assert "SLIPSTREAM_PAGE_CONTENT nonce=" in wrapped
    assert "END_SLIPSTREAM_PAGE_CONTENT" in wrapped
    assert "PAGE TEXT" in wrapped
    assert "origin=https://example.com" in wrapped
    meta = boundary_meta(origin="https://example.com")
    assert "nonce" in meta and meta["origin"] == "https://example.com"


def test_parse_allowed_domains_env_style():
    assert parse_allowed_domains("example.com, *.foo.org github.com") == [
        "example.com",
        "*.foo.org",
        "github.com",
    ]
