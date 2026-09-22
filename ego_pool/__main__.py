"""CLI entry: python -m ego_pool"""

from __future__ import annotations

import argparse
import signal
import sys

from ego_pool.api import PoolServer
from ego_pool.config import PoolConfig
from ego_pool.pool import BrowserPool


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ego Browser Pool Manager (MVP)")
    parser.add_argument("--host", default=None, help="Bind host (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="Bind port (default 8755)")
    parser.add_argument("--mock", action="store_true", help="Mock Chromium launches (EGO_POOL_MOCK=1)")
    parser.add_argument("--headed", action="store_true", help="Run Chromium headed (not headless)")
    args = parser.parse_args(argv)

    cfg = PoolConfig.from_env()
    if args.mock:
        cfg.mock = True
    if args.headed:
        cfg.headless = False
    if args.host:
        cfg.host = args.host
    if args.port is not None:
        cfg.port = args.port

    pool = BrowserPool(cfg)
    server = PoolServer(pool)

    def _shutdown(signum, frame):  # noqa: ARG001
        print("\nShutting down pool…", flush=True)
        server.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    print(
        f"ego-pool listening on {server.base_url}  "
        f"(K={cfg.K} W={cfg.W} mock={cfg.mock} chrome={pool.launcher.binary})",
        flush=True,
    )
    print("Endpoints: POST /v1/leases  POST /v1/leases/{id}/heartbeat  "
          "DELETE /v1/leases/{id}  GET /v1/pool/status", flush=True)
    server.start(background=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
