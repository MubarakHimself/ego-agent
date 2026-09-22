#!/usr/bin/env python3
"""Pool speed benchmark — LIVE (real Chrome) and/or MOCK (EGO_POOL_MOCK=1).

Measures wall times for:
  - cold lease (first launch)
  - CDP ready (LIVE only — until /json/version)
  - navigate (LIVE only — PUT /json/new + title check)
  - heartbeat RTT
  - release (explicit DELETE → FREE_WARM when W>=1)
  - warm reuse lease (same space_id after client release)

Usage:
  # Mock only (no Chrome)
  EGO_POOL_MOCK=1 python scripts/bench_pool.py --mode MOCK

  # Live Chrome (system google-chrome / EGO_POOL_CHROME)
  python scripts/bench_pool.py --mode LIVE

  # Both
  python scripts/bench_pool.py --mode BOTH

Writes JSON to stdout; optionally --out benches/latest.json.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Allow running from repo root without install
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from ego_pool.cdp_http import navigate_via_json_new, wait_cdp_ready
from ego_pool.config import PoolConfig
from ego_pool.launcher import find_chrome_binary
from ego_pool.pool import BrowserPool


def _ms(t0: float, t1: float | None = None) -> float:
    end = time.perf_counter() if t1 is None else t1
    return round((end - t0) * 1000.0, 3)


def _run_once(*, mock: bool, chrome_binary: str | None, navigate_url: str) -> dict[str, Any]:
    mode = "MOCK" if mock else "LIVE"
    spaces = Path(tempfile.mkdtemp(prefix=f"ego-bench-{mode.lower()}-"))
    # Distinct CDP base ports so LIVE/MOCK back-to-back do not collide.
    cdp_base = 19622 if mock else 19722
    cfg = PoolConfig(
        K=1,
        W=1,
        spaces_root=spaces / "spaces",
        cdp_base_port=cdp_base,
        mock=mock,
        headless=True,
        chrome_binary=chrome_binary,
        idle_ttl_seconds=300,
        lease_hard_ttl_seconds=1800,
    )
    pool = BrowserPool(cfg)
    timings: dict[str, float | None] = {
        "cold_lease_ms": None,
        "cdp_ready_ms": None,
        "navigate_ms": None,
        "heartbeat_ms": None,
        "release_ms": None,
        "warm_reuse_lease_ms": None,
    }
    meta: dict[str, Any] = {
        "mode": mode,
        "mock": mock,
        "chrome_binary": chrome_binary if not mock else None,
        "navigate_url": navigate_url if not mock else None,
        "space_id": "bench-space",
        "agent_id": "bench-agent",
        "cdp_base_port": cdp_base,
        "navigate_snapshot": None,
        "errors": [],
    }
    try:
        # --- cold lease ---
        t0 = time.perf_counter()
        lease = pool.lease("bench-agent", "bench-space")
        timings["cold_lease_ms"] = _ms(t0)
        assert lease["status"] == "leased", lease

        if not mock:
            t0 = time.perf_counter()
            version = wait_cdp_ready(lease["cdp_http_url"], timeout=20.0)
            timings["cdp_ready_ms"] = _ms(t0)
            meta["browser"] = version.get("Browser")

            t0 = time.perf_counter()
            snap = navigate_via_json_new(
                lease["cdp_http_url"],
                navigate_url,
                expect_title_substr="Example Domain"
                if "example.com" in navigate_url
                else None,
                settle_timeout=15.0,
            )
            timings["navigate_ms"] = _ms(t0)
            meta["navigate_snapshot"] = {
                "title": snap.get("title"),
                "url": snap.get("url"),
                "matched": snap.get("matched"),
                "id": snap.get("id"),
            }
            if not snap.get("matched"):
                meta["errors"].append(
                    f"navigate title/url did not settle: {meta['navigate_snapshot']}"
                )

        # --- heartbeat ---
        t0 = time.perf_counter()
        hb = pool.heartbeat(lease["lease_id"])
        timings["heartbeat_ms"] = _ms(t0)
        assert hb.get("ok") is True, hb

        # --- release (explicit → FREE_WARM when W=1) ---
        t0 = time.perf_counter()
        rel = pool.release(lease["lease_id"], reason="bench_done")
        timings["release_ms"] = _ms(t0)
        status = pool.status()
        warm = status.get("warm", 0)
        meta["warm_after_release"] = warm

        # --- warm reuse (same space) ---
        t0 = time.perf_counter()
        lease2 = pool.lease("bench-agent", "bench-space")
        timings["warm_reuse_lease_ms"] = _ms(t0)
        assert lease2["status"] == "leased", lease2
        meta["warm_reuse_same_cdp"] = (
            lease2.get("cdp_http_url") == lease.get("cdp_http_url")
        )
        # Confirm still reachable in LIVE
        if not mock:
            wait_cdp_ready(lease2["cdp_http_url"], timeout=5.0)

        pool.release(lease2["lease_id"], reason="bench_cleanup")
    except Exception as e:
        meta["errors"].append(f"{type(e).__name__}: {e}")
        raise
    finally:
        pool.shutdown()

    return {
        "mode": mode,
        "timings_ms": timings,
        "meta": meta,
        "ok": len(meta["errors"]) == 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ego pool LIVE/MOCK speed benchmark")
    parser.add_argument(
        "--mode",
        choices=("LIVE", "MOCK", "BOTH"),
        default=os.environ.get("EGO_BENCH_MODE", "BOTH"),
        help="LIVE=real Chrome, MOCK=EGO_POOL_MOCK, BOTH=run each (default BOTH)",
    )
    parser.add_argument(
        "--navigate-url",
        default="https://example.com",
        help="URL for LIVE navigate check (default https://example.com)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write full JSON result (e.g. benches/latest.json)",
    )
    parser.add_argument(
        "--chrome",
        default=os.environ.get("EGO_POOL_CHROME") or None,
        help="Chrome binary path (default: auto-detect / EGO_POOL_CHROME)",
    )
    args = parser.parse_args(argv)

    chrome = args.chrome or find_chrome_binary()
    modes: list[str] = []
    if args.mode in ("MOCK", "BOTH"):
        modes.append("MOCK")
    if args.mode in ("LIVE", "BOTH"):
        modes.append("LIVE")

    results: list[dict[str, Any]] = []
    errors: list[str] = []

    for mode in modes:
        mock = mode == "MOCK"
        if not mock and not chrome:
            errors.append("LIVE requested but no Chrome binary found")
            results.append(
                {
                    "mode": "LIVE",
                    "ok": False,
                    "timings_ms": {},
                    "meta": {"errors": ["no Chrome binary"]},
                }
            )
            continue
        # Clear mock env for LIVE so PoolConfig.from_env side-effects do not leak
        prev = os.environ.get("EGO_POOL_MOCK")
        try:
            if mock:
                os.environ["EGO_POOL_MOCK"] = "1"
            elif "EGO_POOL_MOCK" in os.environ:
                del os.environ["EGO_POOL_MOCK"]
            run = _run_once(
                mock=mock,
                chrome_binary=None if mock else chrome,
                navigate_url=args.navigate_url,
            )
            results.append(run)
            if not run["ok"]:
                errors.extend(run["meta"].get("errors") or [])
        except Exception as e:
            errors.append(f"{mode}: {type(e).__name__}: {e}")
            results.append(
                {
                    "mode": mode,
                    "ok": False,
                    "timings_ms": {},
                    "meta": {"errors": [f"{type(e).__name__}: {e}"]},
                }
            )
        finally:
            if prev is None:
                os.environ.pop("EGO_POOL_MOCK", None)
            else:
                os.environ["EGO_POOL_MOCK"] = prev

    payload = {
        "benchmark": "ego-runtime-livebench-001",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "host": {
            "chrome_binary": chrome,
            "python": sys.version.split()[0],
        },
        "results": results,
        "ok": all(r.get("ok") for r in results) and not errors,
        "errors": errors,
    }

    text = json.dumps(payload, indent=2)
    print(text)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
        print(f"# wrote {args.out}", file=sys.stderr)

    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
