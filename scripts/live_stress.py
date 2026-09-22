#!/usr/bin/env python3
"""Live out-of-box stress / edge cases for Slipstream — REAL Chrome only.

Requires google-chrome / chromium (or SLIPSTREAM_CHROME).
Refuses SLIPSTREAM_MOCK=1. Skip only when Chrome binary is missing.

Usage:
  python scripts/live_stress.py
  python scripts/live_stress.py --json /tmp/live_stress.json
  pytest -q -m live_stress
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from slipstream.api import PoolServer
from slipstream.cdp_http import navigate_via_json_new, wait_cdp_ready
from slipstream.config import PoolConfig
from slipstream.launcher import find_chrome_binary
from slipstream.pool import (
    BrowserPool,
    LeaseExpiredError,
    LeaseNotFoundError,
    PoolFullError,
    SpaceInUseError,
)


@dataclass
class CaseResult:
    name: str
    status: str  # PASS | FAIL | SKIP
    ms: float
    detail: str = ""
    error: str = ""


@dataclass
class StressReport:
    environment: dict[str, Any] = field(default_factory=dict)
    cases: list[CaseResult] = field(default_factory=list)
    bugs_found: list[str] = field(default_factory=list)
    fixes_applied: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.status in ("PASS", "SKIP") for c in self.cases) and any(
            c.status == "PASS" for c in self.cases
        )


def _chrome_version(binary: str) -> str:
    try:
        return subprocess.check_output(
            [binary, "--version"], text=True, timeout=10
        ).strip()
    except Exception as e:  # noqa: BLE001
        return f"unknown ({e})"


def _require_chrome() -> str:
    if os.environ.get("SLIPSTREAM_MOCK") == "1":
        raise RuntimeError(
            "SLIPSTREAM_MOCK=1 set — live_stress refuses mock validation"
        )
    binary = find_chrome_binary(os.environ.get("SLIPSTREAM_CHROME"))
    if not binary:
        binary = find_chrome_binary("/usr/bin/google-chrome")
    if not binary:
        raise FileNotFoundError("No Chrome/Chromium binary found")
    return binary


def _make_pool(
    tmp: Path,
    *,
    binary: str,
    K: int = 5,
    W: int = 1,
    idle_ttl_seconds: int = 300,
    lease_hard_ttl_seconds: int = 1800,
    cdp_base_port: int = 19522,
) -> BrowserPool:
    cfg = PoolConfig(
        K=K,
        W=W,
        idle_ttl_seconds=idle_ttl_seconds,
        lease_hard_ttl_seconds=lease_hard_ttl_seconds,
        spaces_root=tmp / "spaces",
        cdp_base_port=cdp_base_port,
        mock=False,
        headless=True,
        chrome_binary=binary,
    )
    return BrowserPool(cfg)


def _slot_pid(pool: BrowserPool, lease_id: str) -> int | None:
    # Internal slot handle — public status omits chromium_pid unless EXPOSE_RAW_CDP.
    slot = pool._find_slot_by_lease(lease_id)
    return None if slot is None else slot.chromium_pid


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False



def _validate_api_request_url(url: str) -> str:
    """Loopback allowlist for stress harness HTTP (Skylos SKY-D216 sanitizer name)."""
    if not isinstance(url, str) or not url.startswith(
        ("http://127.0.0.1:", "http://localhost:", "http://[::1]:")
    ):
        raise ValueError(f"live_stress refuses non-loopback URL: {url!r}")
    return url


def _resolve_policy_path(path: Path) -> Path:
    """Resolve output path (Skylos PATH_SANITIZERS-recognized name)."""
    return Path(path).expanduser().resolve()


def _write_text_nofollow(path: Path, text: str, *, mode: int = 0o644) -> None:
    """Write report path without following symlinks."""
    path = _resolve_policy_path(path)
    if path.exists() and path.is_symlink():
        raise SystemExit(f"refusing symlink output path: {path}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    data = text.encode("utf-8")
    fd = os.open(path, flags, mode)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def _http_json(
    method: str, url: str, body: dict | None = None, timeout: float = 90.0
) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    safe_url = _validate_api_request_url(url)
    req = urllib.request.Request(safe_url)
    req.method = method
    if data is not None:
        req.data = data
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {"raw": raw}
        return e.code, parsed


def run_case(name: str, fn: Callable[[], str]) -> CaseResult:
    t0 = time.perf_counter()
    try:
        detail = fn() or ""
        return CaseResult(
            name=name,
            status="PASS",
            ms=(time.perf_counter() - t0) * 1000,
            detail=detail,
        )
    except Exception as e:  # noqa: BLE001
        return CaseResult(
            name=name,
            status="FAIL",
            ms=(time.perf_counter() - t0) * 1000,
            error=f"{type(e).__name__}: {e}",
            detail=traceback.format_exc(limit=12),
        )


def case_fill_k5(binary: str, base_port: int) -> str:
    with tempfile.TemporaryDirectory(prefix="ss-k5-") as td:
        pool = _make_pool(Path(td), binary=binary, K=5, cdp_base_port=base_port)
        try:
            leases = []
            for i in range(5):
                lease = pool.lease(f"agent-{i}", f"space-{i}")
                assert lease["status"] == "leased", lease
                ver = wait_cdp_ready(lease["cdp_http_url"], timeout=25.0)
                assert "Browser" in ver, ver
                leases.append(lease)
            st = pool.status()
            assert st["leased"] == 5 and st["live"] == 5, st
            urls = {L["cdp_http_url"] for L in leases}
            pids = {_slot_pid(pool, L["lease_id"]) for L in leases}
            assert len(urls) == 5, urls
            assert None not in pids and len(pids) == 5, pids
            return f"leased=5 distinct_cdp={len(urls)} distinct_pid={len(pids)}"
        finally:
            pool.shutdown()


def case_pool_full(binary: str, base_port: int) -> str:
    with tempfile.TemporaryDirectory(prefix="ss-full-") as td:
        pool = _make_pool(
            Path(td) / "ip", binary=binary, K=5, cdp_base_port=base_port
        )
        try:
            for i in range(5):
                lease = pool.lease(f"a{i}", f"s{i}")
                wait_cdp_ready(lease["cdp_http_url"], timeout=25.0)
            try:
                pool.lease("ax", "sx")
                raise AssertionError("expected PoolFullError")
            except PoolFullError as e:
                detail = str(e)
        finally:
            pool.shutdown()
        # Let Chromium process groups fully reap before opening another K=5 pool.
        time.sleep(1.0)

        cfg = PoolConfig(
            K=5,
            W=1,
            spaces_root=Path(td) / "http" / "spaces",
            cdp_base_port=base_port + 10,
            mock=False,
            headless=True,
            chrome_binary=binary,
            host="127.0.0.1",
            port=18766,
        )
        pool2 = BrowserPool(cfg)
        server = PoolServer(pool2, host="127.0.0.1", port=18766)
        server.start(background=True)
        try:
            for i in range(5):
                code, body = _http_json(
                    "POST",
                    f"{server.base_url}/v1/leases",
                    {"agent_id": f"h{i}", "space_id": f"hs{i}"},
                )
                assert code == 200, (code, body)
                wait_cdp_ready(body["cdp_http_url"], timeout=25.0)
            code6, body6 = _http_json(
                "POST",
                f"{server.base_url}/v1/leases",
                {"agent_id": "h6", "space_id": "hs6"},
            )
            assert code6 == 503, (code6, body6)
            assert body6.get("error") == "pool_full", body6
            return f"PoolFullError + HTTP 503 pool_full ({detail!r})"
        finally:
            server.stop()


def case_space_exclusive(binary: str, base_port: int) -> str:
    with tempfile.TemporaryDirectory(prefix="ss-excl-") as td:
        pool = _make_pool(
            Path(td) / "ip", binary=binary, K=3, cdp_base_port=base_port
        )
        try:
            a = pool.lease("agent-a", "shared")
            wait_cdp_ready(a["cdp_http_url"], timeout=25.0)
            try:
                pool.lease("agent-b", "shared")
                raise AssertionError("expected SpaceInUseError")
            except SpaceInUseError as e:
                detail = str(e)
        finally:
            pool.shutdown()

        cfg = PoolConfig(
            K=3,
            W=1,
            spaces_root=Path(td) / "http" / "spaces",
            cdp_base_port=base_port + 10,
            mock=False,
            headless=True,
            chrome_binary=binary,
            host="127.0.0.1",
            port=18767,
        )
        pool2 = BrowserPool(cfg)
        server = PoolServer(pool2, host="127.0.0.1", port=18767)
        server.start(background=True)
        try:
            c1, b1 = _http_json(
                "POST",
                f"{server.base_url}/v1/leases",
                {"agent_id": "agent-a", "space_id": "http-shared"},
            )
            assert c1 == 200, (c1, b1)
            wait_cdp_ready(b1["cdp_http_url"], timeout=25.0)
            c2, b2 = _http_json(
                "POST",
                f"{server.base_url}/v1/leases",
                {"agent_id": "agent-b", "space_id": "http-shared"},
            )
            assert c2 == 409, (c2, b2)
            assert b2.get("error") == "space_in_use", b2
            return f"SpaceInUseError + HTTP 409 ({detail!r})"
        finally:
            server.stop()


def case_heartbeat_and_ttl(binary: str, base_port: int) -> str:
    notes: list[str] = []
    with tempfile.TemporaryDirectory(prefix="ss-ttl-") as td:
        pool = _make_pool(
            Path(td) / "idle",
            binary=binary,
            K=2,
            idle_ttl_seconds=2,
            lease_hard_ttl_seconds=600,
            cdp_base_port=base_port,
        )
        try:
            lease = pool.lease("hb-agent", "hb-space", ttl_seconds=600)
            wait_cdp_ready(lease["cdp_http_url"], timeout=25.0)
            lid = lease["lease_id"]
            pid = _slot_pid(pool, lid)
            for _ in range(4):
                time.sleep(1.0)
                hb = pool.heartbeat(lid)
                assert hb["ok"] is True
            assert pool.evict_idle() == []
            assert lid in pool._leases  # noqa: SLF001
            notes.append("heartbeat_keeps_alive")

            time.sleep(2.5)
            evicted = pool.evict_idle()
            assert lid in evicted, evicted
            assert lid not in pool._leases
            st = pool.status()
            assert st["leased"] == 0 and st["warm"] == 0
            if pid is not None:
                deadline = time.time() + 5
                while time.time() < deadline and _pid_alive(pid):
                    time.sleep(0.2)
                assert not _pid_alive(pid), f"pid {pid} still alive after idle evict"
            notes.append("idle_ttl_evict_stops_chrome")
        finally:
            pool.shutdown()

    with tempfile.TemporaryDirectory(prefix="ss-hard-") as td:
        pool = _make_pool(
            Path(td),
            binary=binary,
            K=1,
            idle_ttl_seconds=300,
            lease_hard_ttl_seconds=2,
            cdp_base_port=base_port + 5,
        )
        try:
            lease = pool.lease("hard-agent", "hard-space", ttl_seconds=2)
            wait_cdp_ready(lease["cdp_http_url"], timeout=25.0)
            lid = lease["lease_id"]
            time.sleep(2.2)
            try:
                pool.heartbeat(lid)
                raise AssertionError("expected LeaseExpiredError after hard TTL")
            except LeaseExpiredError:
                pass
            assert lid not in pool._leases

            lease2 = pool.lease("hard-agent2", "hard-space2", ttl_seconds=2)
            wait_cdp_ready(lease2["cdp_http_url"], timeout=25.0)
            lid2 = lease2["lease_id"]
            time.sleep(2.2)
            ev = pool.evict_idle()
            assert lid2 in ev
            notes.append("hard_ttl_heartbeat_expired")
            notes.append("hard_ttl_evict")
        finally:
            pool.shutdown()
    return "; ".join(notes)


def case_warm_reuse(binary: str, base_port: int) -> str:
    with tempfile.TemporaryDirectory(prefix="ss-warm-") as td:
        pool = _make_pool(
            Path(td), binary=binary, K=2, W=1, cdp_base_port=base_port
        )
        try:
            lease1 = pool.lease("warm-agent", "warm-space")
            wait_cdp_ready(lease1["cdp_http_url"], timeout=25.0)
            pid1 = _slot_pid(pool, lease1["lease_id"])
            url1 = lease1["cdp_http_url"]
            rel = pool.release(lease1["lease_id"])
            assert rel["kept_warm"] is True, rel
            assert pool.status()["warm"] == 1
            lease2 = pool.lease("warm-agent", "warm-space")
            pid2 = _slot_pid(pool, lease2["lease_id"])
            assert pid2 == pid1, (pid1, pid2)
            assert lease2["cdp_http_url"] == url1
            wait_cdp_ready(lease2["cdp_http_url"], timeout=5.0)
            pool.release(lease2["lease_id"])
            return f"same_pid={pid1} same_cdp={url1}"
        finally:
            pool.shutdown()


def case_thrash(binary: str, base_port: int) -> str:
    cycles = 8
    with tempfile.TemporaryDirectory(prefix="ss-thrash-") as td:
        pool = _make_pool(
            Path(td), binary=binary, K=3, W=1, cdp_base_port=base_port
        )
        try:
            times: list[float] = []
            last_pid = None
            for i in range(cycles):
                t0 = time.perf_counter()
                space = f"thrash-{'a' if i % 2 == 0 else 'b'}"
                lease = pool.lease("thrash-agent", space)
                wait_cdp_ready(lease["cdp_http_url"], timeout=25.0)
                pid = _slot_pid(pool, lease["lease_id"])
                assert pid is not None
                pool.release(lease["lease_id"])
                times.append((time.perf_counter() - t0) * 1000)
                last_pid = pid
            assert pool.status()["leased"] == 0
            return (
                f"cycles={cycles} last_pid={last_pid} "
                f"ms_min={min(times):.1f} ms_max={max(times):.1f} "
                f"ms_avg={sum(times)/len(times):.1f}"
            )
        finally:
            pool.shutdown()


def case_bad_ids(binary: str, base_port: int) -> str:
    bad = ["", ".", "..", "a/b", "foo..bar", "x\\y"]
    with tempfile.TemporaryDirectory(prefix="ss-bad-") as td:
        pool = _make_pool(
            Path(td) / "ip", binary=binary, K=1, cdp_base_port=base_port
        )
        try:
            for b in bad:
                try:
                    pool.lease("agent", b)
                    raise AssertionError(f"expected ValueError for {b!r}")
                except ValueError:
                    pass
        finally:
            pool.shutdown()

        cfg = PoolConfig(
            K=1,
            W=1,
            spaces_root=Path(td) / "http" / "spaces",
            cdp_base_port=base_port + 2,
            mock=False,
            headless=True,
            chrome_binary=binary,
            host="127.0.0.1",
            port=18768,
        )
        pool2 = BrowserPool(cfg)
        server = PoolServer(pool2, host="127.0.0.1", port=18768)
        server.start(background=True)
        try:
            for b in ["", ".", "..", "a/b", "foo..bar"]:
                code, body = _http_json(
                    "POST",
                    f"{server.base_url}/v1/leases",
                    {"agent_id": "a", "space_id": b},
                )
                assert code == 400, (b, code, body)
            return f"rejected={bad} ValueError+HTTP400"
        finally:
            server.stop()


def case_double_release(binary: str, base_port: int) -> str:
    with tempfile.TemporaryDirectory(prefix="ss-dbl-") as td:
        pool = _make_pool(
            Path(td) / "ip", binary=binary, K=1, cdp_base_port=base_port
        )
        try:
            lease = pool.lease("dbl-agent", "dbl-space")
            wait_cdp_ready(lease["cdp_http_url"], timeout=25.0)
            lid = lease["lease_id"]
            pool.release(lid)
            try:
                pool.release(lid)
                raise AssertionError("expected LeaseNotFoundError on double release")
            except LeaseNotFoundError:
                pass
        finally:
            pool.shutdown()

        cfg = PoolConfig(
            K=1,
            W=1,
            spaces_root=Path(td) / "http" / "spaces",
            cdp_base_port=base_port + 3,
            mock=False,
            headless=True,
            chrome_binary=binary,
            host="127.0.0.1",
            port=18769,
        )
        pool2 = BrowserPool(cfg)
        server = PoolServer(pool2, host="127.0.0.1", port=18769)
        server.start(background=True)
        try:
            code, body = _http_json(
                "POST",
                f"{server.base_url}/v1/leases",
                {"agent_id": "a", "space_id": "http-dbl"},
            )
            assert code == 200, (code, body)
            wait_cdp_ready(body["cdp_http_url"], timeout=25.0)
            lid2 = body["lease_id"]
            c1, b1 = _http_json("DELETE", f"{server.base_url}/v1/leases/{lid2}")
            assert c1 == 200, (c1, b1)
            c2, b2 = _http_json("DELETE", f"{server.base_url}/v1/leases/{lid2}")
            assert c2 in (404, 410), (c2, b2)
            return f"LeaseNotFoundError + HTTP second={c2} {b2}"
        finally:
            server.stop()


def case_navigate(binary: str, base_port: int) -> str:
    with tempfile.TemporaryDirectory(prefix="ss-nav-") as td:
        pool = _make_pool(Path(td), binary=binary, K=1, cdp_base_port=base_port)
        try:
            lease = pool.lease("nav-agent", "nav-space")
            wait_cdp_ready(lease["cdp_http_url"], timeout=25.0)
            snap = navigate_via_json_new(
                lease["cdp_http_url"],
                "https://example.com",
                expect_title_substr="Example Domain",
                settle_timeout=20.0,
            )
            assert snap.get("matched"), snap
            pool.release(lease["lease_id"])
            return f"title={snap.get('title')!r} url={snap.get('url')!r}"
        finally:
            pool.shutdown()


def run_all(*, base_port: int = 19522) -> StressReport:
    report = StressReport()
    try:
        binary = _require_chrome()
    except FileNotFoundError as e:
        report.environment = {
            "chrome": None,
            "error": str(e),
            "os": platform.platform(),
        }
        report.cases.append(
            CaseResult(
                name="chrome_required",
                status="SKIP",
                ms=0,
                detail="Chrome binary missing — skip only when binary absent",
            )
        )
        return report
    except RuntimeError as e:
        report.environment = {"error": str(e)}
        report.cases.append(
            CaseResult(name="mock_refused", status="FAIL", ms=0, error=str(e))
        )
        return report

    report.environment = {
        "chrome_binary": binary,
        "chrome_version": _chrome_version(binary),
        "os": platform.platform(),
        "python": sys.version.split()[0],
        "cdp_base_port": base_port,
        "mock": False,
        "cwd": str(_ROOT),
    }

    cases: list[tuple[str, Callable[[], str]]] = [
        ("1_fill_k5_real_cdp", lambda: case_fill_k5(binary, base_port)),
        ("2_pool_full_503", lambda: case_pool_full(binary, base_port + 20)),
        ("3_space_exclusive_409", lambda: case_space_exclusive(binary, base_port + 50)),
        ("4_heartbeat_idle_hard_ttl", lambda: case_heartbeat_and_ttl(binary, base_port + 80)),
        ("5_warm_reuse_same_pid_cdp", lambda: case_warm_reuse(binary, base_port + 100)),
        ("6_thrash_lease_release", lambda: case_thrash(binary, base_port + 110)),
        ("7_bad_space_ids_400", lambda: case_bad_ids(binary, base_port + 130)),
        ("8_double_release_404", lambda: case_double_release(binary, base_port + 140)),
        ("9_navigate_cdp_http", lambda: case_navigate(binary, base_port + 150)),
    ]

    for name, fn in cases:
        print(f"==> {name} …", flush=True)
        result = run_case(name, fn)
        report.cases.append(result)
        extra = result.error or result.detail
        print(f"    {result.status}  {result.ms:.0f}ms  {extra[:240]}", flush=True)

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Slipstream live stress (real Chrome)")
    parser.add_argument("--json", type=Path, default=None, help="Write JSON report")
    parser.add_argument("--cdp-base-port", type=int, default=19522)
    args = parser.parse_args(argv)

    report = run_all(base_port=args.cdp_base_port)
    payload = {
        "ok": report.ok,
        "environment": report.environment,
        "cases": [asdict(c) for c in report.cases],
        "bugs_found": report.bugs_found,
        "fixes_applied": report.fixes_applied,
    }
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        _write_text_nofollow(args.json, json.dumps(payload, indent=2) + "\n")
        print(f"wrote {args.json}", flush=True)

    print("\n=== SUMMARY ===", flush=True)
    for c in report.cases:
        print(f"  {c.status:4}  {c.ms:8.1f}ms  {c.name}", flush=True)
    print(f"ok={report.ok}", flush=True)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
