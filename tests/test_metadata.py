"""user_metadata tags + q= list/filter (session-metadata-001)."""

from __future__ import annotations

import json
import urllib.error

import pytest

from slipstream.metadata import (
    MetadataValidationError,
    combine_q,
    effective_metadata,
    metadata_matches,
    validate_user_metadata,
)
from slipstream.pool import BrowserPool
from slipstream.config import PoolConfig


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


def test_validate_string_leaves_and_nested():
    meta = validate_user_metadata({"env": "staging", "run": {"id": "r1"}})
    assert meta["env"] == "staging"
    assert meta["run"]["id"] == "r1"


def test_validate_rejects_non_string_leaf():
    with pytest.raises(MetadataValidationError):
        validate_user_metadata({"n": 1})
    with pytest.raises(MetadataValidationError):
        validate_user_metadata({"ok": True})
    with pytest.raises(MetadataValidationError):
        validate_user_metadata({"xs": ["a"]})


def test_validate_rejects_oversize():
    big = {"k": "x" * 600}
    with pytest.raises(MetadataValidationError):
        validate_user_metadata(big)


def test_validate_rejects_secret_keys():
    for bad in (
        "password",
        "cookie",
        "token",
        "jwt",
        "bearer",
        "api_key",
        "accessToken",
        "sessionToken",
        "clientSecret",
        "myPassword",
        "cookieJar",
    ):
        with pytest.raises(MetadataValidationError):
            validate_user_metadata({bad: "nope"})


def test_effective_merge_override_wins():
    eff = effective_metadata(
        {"env": "prod", "team": "a", "run": {"id": "1"}},
        {"env": "staging", "run": {"id": "2"}},
    )
    assert eff == {"env": "staging", "team": "a", "run": {"id": "2"}}


def test_q_exact_dotted_and_substring():
    meta = {"env": "staging", "run": {"id": "abc-123"}, "note": "fleet-west"}
    assert metadata_matches(meta, "env=staging")
    assert metadata_matches(meta, "run.id=abc-123")
    assert metadata_matches(meta, "env=staging run.id=abc-123")
    assert not metadata_matches(meta, "env=prod")
    assert metadata_matches(meta, "fleet")
    assert not metadata_matches(meta, "east")
    assert metadata_matches(meta, "user_metadata['env']:'staging'")
    assert metadata_matches(meta, None)
    assert metadata_matches(meta, "")


def test_combine_q_tags():
    assert combine_q("env=staging", ["team=fleet"]) == "env=staging team=fleet"
    assert combine_q(None, ["env=staging"]) == "env=staging"
    with pytest.raises(MetadataValidationError):
        combine_q(None, ["notag"])


def test_space_metadata_inherited_by_lease(pool: BrowserPool):
    pool.set_space_metadata("s1", {"env": "staging", "team": "fleet"})
    lease = pool.lease("agent-a", "s1")
    assert lease["user_metadata"] == {"env": "staging", "team": "fleet"}


def test_lease_override_merges(pool: BrowserPool):
    pool.set_space_metadata("s1", {"env": "staging", "team": "fleet"})
    lease = pool.lease(
        "agent-a",
        "s1",
        user_metadata={"env": "canary", "run": {"id": "r9"}},
    )
    assert lease["user_metadata"] == {
        "env": "canary",
        "team": "fleet",
        "run": {"id": "r9"},
    }


def test_list_spaces_and_leases_q(pool: BrowserPool):
    pool.set_space_metadata("alpha", {"env": "staging"})
    pool.set_space_metadata("beta", {"env": "prod"})
    pool.lease("a1", "alpha")
    pool.lease("a2", "beta", user_metadata={"lane": "canary"})

    spaces = pool.list_spaces(q="env=staging")
    assert [s["space_id"] for s in spaces["spaces"]] == ["alpha"]

    leases = pool.list_leases(q="env=prod")
    assert len(leases["leases"]) == 1
    assert leases["leases"][0]["space_id"] == "beta"

    leases2 = pool.list_leases(q="lane=canary")
    assert len(leases2["leases"]) == 1

    empty = pool.list_leases(q="env=nope")
    assert empty["leases"] == []


def test_secret_metadata_refused_on_space(pool: BrowserPool):
    with pytest.raises(MetadataValidationError):
        pool.set_space_metadata("s1", {"password": "x"})


def test_api_metadata_flow(tmp_path):
    from slipstream.api import PoolServer

    cfg = PoolConfig(
        K=2,
        W=1,
        spaces_root=tmp_path / "spaces",
        vault_root=tmp_path / "vault",
        mock=True,
        host="127.0.0.1",
        port=18757,
    )
    pool = BrowserPool(cfg)
    server = PoolServer(pool, port=18757)
    server.start(background=True)
    try:
        import urllib.request

        def req(method, url, body=None):
            data = None if body is None else json.dumps(body).encode()
            r = urllib.request.Request(
                url, data=data, method=method, headers={"Content-Type": "application/json"} if data else {}
            )
            try:
                with urllib.request.urlopen(r, timeout=5) as resp:
                    return resp.status, json.loads(resp.read().decode())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read().decode())

        base = server.base_url
        code, body = req(
            "PUT",
            f"{base}/v1/spaces/task-1",
            {"user_metadata": {"env": "staging", "team": "fleet"}},
        )
        assert code == 200
        assert body["user_metadata"]["env"] == "staging"

        code, lease = req(
            "POST",
            f"{base}/v1/leases",
            {"agent_id": "a1", "space_id": "task-1", "user_metadata": {"env": "canary"}},
        )
        assert code == 200
        assert lease["user_metadata"]["env"] == "canary"
        assert lease["user_metadata"]["team"] == "fleet"

        code, listed = req("GET", f"{base}/v1/leases?q=env%3Dcanary")
        assert code == 200
        assert len(listed["leases"]) == 1

        code, spaces = req("GET", f"{base}/v1/spaces?q=team%3Dfleet")
        assert code == 200
        assert any(s["space_id"] == "task-1" for s in spaces["spaces"])

        code, bad = req(
            "PUT",
            f"{base}/v1/spaces/task-2",
            {"user_metadata": {"token": "secret"}},
        )
        assert code == 400
        assert bad["error"] == "invalid_user_metadata"
    finally:
        server.stop()


def test_effective_metadata_size_cap_after_merge():
    left = {"a": "x" * 200, "b": "y" * 200}
    right = {"c": "z" * 200}
    validate_user_metadata(left)
    validate_user_metadata(right)
    with pytest.raises(MetadataValidationError, match="effective"):
        effective_metadata(left, right)
