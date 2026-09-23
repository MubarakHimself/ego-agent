#!/usr/bin/env python3
"""Thin out-of-box mock smoke: doctor → serve → lease → heartbeat → release.

Requires an editable/package install (`slipstream` on PATH or python -m).
Forces SLIPSTREAM_MOCK=1. No Chrome, no MCP, no Electron.
Exit 0 on success.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path

from slipstream.api import PoolServer
from slipstream.cli import cmd_heartbeat, cmd_lease, cmd_release
from slipstream.config import PoolConfig
from slipstream.doctor import format_human, run_doctor
from slipstream.pool import BrowserPool


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


def _wait_healthz(url: str, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    health = f"{url.rstrip('/')}/healthz"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(health, timeout=0.5) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            time.sleep(0.05)
    raise RuntimeError(f"pool healthz not ready at {health}: {last}")


def _doctor(url: str, *, quiet: bool) -> int:
    report = run_doctor(url=url)
    if not quiet:
        sys.stdout.write(format_human(report))
    return 0 if report.ok else 1


def run_oob_smoke(
    *,
    port: int | None = None,
    spaces_root: Path | None = None,
    agent_id: str = "oob-smoke",
    space_id: str = "oob-demo",
    quiet: bool = False,
) -> dict:
    """Run mock doctor + in-process serve + lease cycle. Returns summary dict."""
    os.environ["SLIPSTREAM_MOCK"] = "1"
    bind_port = int(port) if port is not None else _free_port()
    root = Path(spaces_root) if spaces_root is not None else Path(
        tempfile.mkdtemp(prefix="slipstream-oob-spaces-")
    )
    root.mkdir(parents=True, exist_ok=True)
    os.environ["SLIPSTREAM_SPACES_ROOT"] = str(root)

    base_url = f"http://127.0.0.1:{bind_port}"
    os.environ["SLIPSTREAM_URL"] = base_url

    # Doctor before serve: pool_healthz WARN is fine (exit 0).
    pre = _doctor(base_url, quiet=quiet)
    if pre != 0:
        raise SystemExit(f"doctor pre-serve failed with exit {pre}")

    cfg = PoolConfig(
        K=2,
        W=1,
        spaces_root=root,
        cdp_base_port=19000 + (bind_port % 1000),
        mock=True,
        host="127.0.0.1",
        port=bind_port,
    )
    pool = BrowserPool(cfg)
    server = PoolServer(pool, host="127.0.0.1", port=bind_port)
    server.start(background=True)
    try:
        _wait_healthz(base_url)
        post = _doctor(base_url, quiet=quiet)
        if post != 0:
            raise SystemExit(f"doctor post-serve failed with exit {post}")

        out = io.StringIO()
        with redirect_stdout(out):
            code = cmd_lease(agent_id=agent_id, space_id=space_id, url=base_url)
        if code != 0:
            raise SystemExit(f"lease failed exit {code}")
        lease = json.loads(out.getvalue())
        lease_id = lease["lease_id"]

        with redirect_stdout(io.StringIO()):
            code = cmd_heartbeat(lease_id=lease_id, url=base_url)
        if code != 0:
            raise SystemExit(f"heartbeat failed exit {code}")

        with redirect_stdout(io.StringIO()):
            code = cmd_release(lease_id=lease_id, url=base_url)
        if code != 0:
            raise SystemExit(f"release failed exit {code}")

        return {
            "ok": True,
            "base_url": base_url,
            "lease_id": lease_id,
            "agent_id": agent_id,
            "space_id": space_id,
            "doctor_pre": pre,
            "doctor_post": post,
        }
    finally:
        server.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Slipstream mock OOB smoke")
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Bind port (default: ephemeral free port)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print summary JSON on success (suppress doctor human text)",
    )
    args = parser.parse_args(argv)
    summary = run_oob_smoke(port=args.port, quiet=bool(args.json))
    if args.json:
        sys.stdout.write(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(
            f"oob_smoke: OK  url={summary['base_url']}  "
            f"lease_id={summary['lease_id']}\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
