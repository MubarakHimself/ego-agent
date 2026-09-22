"""CLI entry: ``slipstream`` / ``python -m slipstream``.

Subcommands:
  serve       Start the pool HTTP server (lease/heartbeat/release API)
  lease       POST /v1/leases — acquire a CDP slot
  heartbeat   POST /v1/leases/{id}/heartbeat
  release     DELETE /v1/leases/{id}
  status      GET /v1/pool/status
  doctor     Preflight: Chrome, CDP, pool healthz, spaces, skill

Client commands talk to a running pool (SLIPSTREAM_URL or --url).
HTTP remains the primary surface — there is no MCP server.
"""

from __future__ import annotations

import argparse
import signal
import sys

from slipstream.api import PoolServer
from slipstream.cli import (
    CliError,
    DEFAULT_URL,
    cmd_alert,
    cmd_heartbeat,
    cmd_lease,
    cmd_release,
    cmd_status,
)
from slipstream.doctor import cmd_doctor
from slipstream.config import PoolConfig
from slipstream.pool import BrowserPool


def _run_serve(args: argparse.Namespace) -> int:
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

    def _shutdown(_signum, _frame) -> None:
        print("\nShutting down pool…", flush=True)
        server.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    print(
        f"slipstream listening on {server.base_url}  "
        f"(K={cfg.K} W={cfg.W} mock={cfg.mock} chrome={pool.launcher.binary})",
        flush=True,
    )
    print(
        "Endpoints: POST /v1/leases  POST /v1/leases/{id}/heartbeat  "
        "POST /v1/leases/{id}/alerts  DELETE /v1/leases/{id}  GET /v1/pool/status",
        flush=True,
    )
    print(
        "Agent CLI: slipstream lease|heartbeat|alert|release|status|doctor  "
        "(see skills/slipstream/SKILL.md)",
        flush=True,
    )
    server.start(background=False)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="slipstream",
        description=(
            "Slipstream browser pool — serve, lease/heartbeat/alert/release/status, "
            "or doctor (preflight) against Chrome + pool."
        ),
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    # --- serve ---
    p_serve = sub.add_parser(
        "serve",
        help="Start the pool HTTP server (agents lease via CLI/HTTP, not MCP)",
    )
    p_serve.add_argument("--host", default=None, help="Bind host (default 127.0.0.1)")
    p_serve.add_argument(
        "--port", type=int, default=None, help="Bind port (default 8755)"
    )
    p_serve.add_argument(
        "--mock",
        action="store_true",
        help="Mock Chromium launches (same as SLIPSTREAM_MOCK=1)",
    )
    p_serve.add_argument(
        "--headed",
        action="store_true",
        help="Run Chromium headed (not headless)",
    )
    p_serve.set_defaults(_handler="serve")

    # Shared client options
    def _add_url(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--url",
            default=None,
            help=f"Pool base URL (env SLIPSTREAM_URL, default {DEFAULT_URL})",
        )

    # --- lease ---
    p_lease = sub.add_parser("lease", help="Acquire a lease (prints lease JSON)")
    _add_url(p_lease)
    p_lease.add_argument("--agent-id", required=True, help="Calling agent id")
    p_lease.add_argument("--space-id", required=True, help="Space / user-data-dir id")
    p_lease.add_argument(
        "--ttl-seconds",
        type=int,
        default=None,
        help="Optional lease TTL (clamped to pool hard TTL)",
    )
    p_lease.set_defaults(_handler="lease")

    # --- heartbeat ---
    p_hb = sub.add_parser("heartbeat", help="Renew a lease soft-idle window")
    _add_url(p_hb)
    p_hb.add_argument("--lease-id", required=True, help="Lease id from lease JSON")
    p_hb.set_defaults(_handler="heartbeat")

    # --- release ---
    p_rel = sub.add_parser("release", help="Release a lease (DELETE)")
    _add_url(p_rel)
    p_rel.add_argument("--lease-id", required=True, help="Lease id from lease JSON")
    p_rel.add_argument(
        "--reason",
        default=None,
        help="Optional release reason (JSON body)",
    )
    p_rel.set_defaults(_handler="release")

    # --- status ---
    p_st = sub.add_parser("status", help="Print pool status JSON")
    _add_url(p_st)
    p_st.set_defaults(_handler="status")

    # --- alert ---
    p_alert = sub.add_parser(
        "alert",
        help="Raise need_human (pause, keep lease) or task_done (notify + release)",
    )
    _add_url(p_alert)
    p_alert.add_argument(
        "kind",
        choices=["need-human", "done"],
        help="need-human: pause agent, keep Chromium; done: notify once then release",
    )
    p_alert.add_argument("--lease-id", required=True, help="Lease id from lease JSON")
    p_alert.add_argument(
        "--reason",
        default=None,
        help="need_human reason: captcha|login|ambiguous_ui|stuck|other",
    )
    p_alert.add_argument("--detail", default=None, help="Short human-safe detail")
    p_alert.add_argument("--task-id", default=None, help="Optional harness task id")
    p_alert.add_argument(
        "--ttl-s",
        type=int,
        default=None,
        help="Watch URL TTL seconds (default 300)",
    )
    p_alert.add_argument(
        "--fail",
        action="store_true",
        help="task_done: outcome.ok=false (default is success)",
    )
    p_alert.add_argument(
        "--summary",
        default=None,
        help="task_done: short outcome summary (no secrets)",
    )
    p_alert.set_defaults(_handler="alert")

    # --- doctor ---
    p_doc = sub.add_parser(
        "doctor",
        help="Preflight: Chrome, CDP probe, pool healthz, spaces root, skill path",
    )
    _add_url(p_doc)
    p_doc.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit machine-readable JSON instead of human text",
    )
    p_doc.set_defaults(_handler="doctor")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "command", None):
        parser.print_help(sys.stderr)
        return 2

    try:
        if args._handler == "serve":
            return _run_serve(args)
        if args._handler == "lease":
            return cmd_lease(
                agent_id=args.agent_id,
                space_id=args.space_id,
                ttl_seconds=args.ttl_seconds,
                url=args.url,
            )
        if args._handler == "heartbeat":
            return cmd_heartbeat(lease_id=args.lease_id, url=args.url)
        if args._handler == "release":
            return cmd_release(
                lease_id=args.lease_id,
                reason=args.reason,
                url=args.url,
            )
        if args._handler == "status":
            return cmd_status(url=args.url)
        if args._handler == "alert":
            ok_flag = False if args.fail else True
            return cmd_alert(
                kind=args.kind,
                lease_id=args.lease_id,
                reason=args.reason,
                detail=args.detail,
                task_id=args.task_id,
                ttl_s=args.ttl_s,
                ok=ok_flag if args.kind == "done" else None,
                summary=args.summary,
                url=args.url,
            )
        if args._handler == "doctor":
            return cmd_doctor(url=args.url, as_json=args.as_json)
    except CliError as e:
        print(e, file=sys.stderr)
        return e.exit_code

    parser.print_help(sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
