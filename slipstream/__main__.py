"""CLI entry: ``slipstream`` / ``python -m slipstream``.

Subcommands:
  serve       Start the pool HTTP server (lease/heartbeat/release API)
  lease       POST /v1/leases — acquire a CDP slot
  heartbeat   POST /v1/leases/{id}/heartbeat
  release     DELETE /v1/leases/{id}
  status      GET /v1/pool/status
  act         POST /v1/leases/{id}/actions — gate fill|eval|download|upload|nav_irreversible
  navigate    POST /v1/leases/{id}/navigate — top-frame nav (ladder + domain allowlist)
  eval        POST /v1/leases/{id}/eval — Runtime.evaluate (ladder-enforced)
  confirm     POST /v1/confirmations/{id} {action: confirm}
  deny        POST /v1/confirmations/{id} {action: deny}
  doctor     Preflight: Chrome, CDP, pool healthz, spaces, skill, watch_compose
  downloads  List/get lease session download artifacts
  uploads    List/put thin lease upload drop
  watch-status  Compose /watch detection (optional; never hard-fail)

Client commands talk to a running pool (SLIPSTREAM_URL or --url).
HTTP remains the primary surface — there is no MCP server.
"""

from __future__ import annotations

_STORE_TRUE = "store_true"
_OPT_LEASE_ID = "--lease-id"
_OPT_AGENT_ID = "--agent-id"
_HELP_AGENT_ID_REQUIRED = "Lease owner agent id (required)"

# Shared CLI literals (SKY-L027 claw)
_CMD_SERVE = "serve"
_CMD_LEASE = "lease"
_CMD_HEARTBEAT = "heartbeat"
_CMD_RELEASE = "release"
_CMD_STATUS = "status"
_CMD_ALERT = "alert"
_CMD_CONFIRM = "confirm"
_CMD_NAVIGATE = "navigate"
_CMD_DOCTOR = "doctor"
_OPT_HOST = "--host"
_HELP_LEASE_ID = "Lease id from lease JSON"
_HELP_SPACE_ID = "Space id"
_REDACTED_SECRET = "[REDACTED_SECRET]"
_ACTION_APPEND = "append"
_OPT_SPACE_ID = "--space-id"
_OPT_TAG = "--tag"
_OPT_JSON = "--json"
_DEST_AS_JSON = "as_json"
_HANDLER_SESSIONS = "sessions"
_HANDLER_CAPTCHA = "captcha"
_HANDLER_DOWNLOADS = "downloads"
_HANDLER_UPLOADS = "uploads"
_OPT_DETAIL = "--detail"
_HELP_DETAIL = "Short non-secret detail"

import argparse
import signal
import sys

from slipstream.api import PoolServer
from slipstream.cli import (
    CliError,
    DEFAULT_URL,
    cmd_act,
    cmd_alert,
    cmd_captcha,
    cmd_downloads_get,
    cmd_downloads_list,
    cmd_uploads_list,
    cmd_uploads_put,
    cmd_confirm,
    cmd_cred_bind,
    cmd_cred_fill,
    cmd_cred_list,
    cmd_cred_unbind,
    cmd_deny,
    cmd_heartbeat,
    cmd_lease,
    cmd_leases_list,
    cmd_sessions,
    cmd_navigate,
    cmd_evaluate,
    cmd_spaces_list,
    cmd_spaces_login_once,
    cmd_spaces_set,
    cmd_spaces_signed_in,
    cmd_release,
    cmd_status,
    resolve_secret,
)
from slipstream.doctor import cmd_doctor, cmd_watch_status
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
        "POST /v1/leases/{id}/alerts  POST …/credentials/*  DELETE /v1/leases/{id}  GET /v1/pool/status",
        flush=True,
    )
    print(
        "Agent CLI: slipstream lease|heartbeat|alert|act|confirm|deny|release|status|doctor  "
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
            "doctor (preflight), or watch-status (compose /watch)."
        ),
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    # --- serve ---
    p_serve = sub.add_parser(
        "serve",
        help="Start the pool HTTP server (agents lease via CLI/HTTP, not MCP)",
    )
    p_serve.add_argument(_OPT_HOST, default=None, help="Bind host (default 127.0.0.1)")
    p_serve.add_argument(
        "--port", type=int, default=None, help="Bind port (default 8755)"
    )
    p_serve.add_argument(
        "--mock",
        action=_STORE_TRUE,
        help="Mock Chromium launches (same as SLIPSTREAM_MOCK=1)",
    )
    p_serve.add_argument(
        "--headed",
        action=_STORE_TRUE,
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
    p_lease = sub.add_parser(_CMD_LEASE, help="Acquire a lease (prints lease JSON)")
    _add_url(p_lease)
    p_lease.add_argument(_OPT_AGENT_ID, required=True, help="Calling agent id")
    p_lease.add_argument(_OPT_SPACE_ID, required=True, help="Space / user-data-dir id")
    p_lease.add_argument(
        "--ttl-seconds",
        type=int,
        default=None,
        help="Optional lease TTL (clamped to pool hard TTL)",
    )
    p_lease.add_argument(
        "--metadata",
        default=None,
        help='Optional user_metadata JSON object, e.g. \'{"env":"staging"}\'',
    )
    p_lease.add_argument(
        _OPT_TAG,
        action=_ACTION_APPEND,
        default=None,
        help="Lease metadata tag key=value (repeatable; overrides/extends --metadata)",
    )
    p_lease.add_argument(
        "--allowed-domains",
        default=None,
        help="Comma-separated top-frame host allowlist for this lease (empty=unrestricted)",
    )
    p_lease.set_defaults(_handler="lease")

    # --- heartbeat ---
    p_hb = sub.add_parser(_CMD_HEARTBEAT, help="Renew a lease soft-idle window")
    _add_url(p_hb)
    p_hb.add_argument(_OPT_LEASE_ID, required=True, help=_HELP_LEASE_ID)
    p_hb.set_defaults(_handler="heartbeat")

    # --- release ---
    p_rel = sub.add_parser(_CMD_RELEASE, help="Release a lease (DELETE)")
    _add_url(p_rel)
    p_rel.add_argument(_OPT_LEASE_ID, required=True, help=_HELP_LEASE_ID)
    p_rel.add_argument(
        "--reason",
        default=None,
        help="Optional release reason (JSON body)",
    )
    p_rel.set_defaults(_handler="release")

    # --- status ---
    p_st = sub.add_parser(_CMD_STATUS, help="Print pool status JSON")
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
    p_alert.add_argument(_OPT_LEASE_ID, required=True, help=_HELP_LEASE_ID)
    p_alert.add_argument(
        "--reason",
        default=None,
        help="need_human reason: captcha|login|ambiguous_ui|stuck|other",
    )
    p_alert.add_argument(_OPT_DETAIL, default=None, help=_HELP_DETAIL)
    p_alert.add_argument("--task-id", default=None, help="Optional harness task id")
    p_alert.add_argument(
        "--ttl-s",
        type=int,
        default=None,
        help="Watch URL TTL seconds (default 300)",
    )
    p_alert.add_argument(
        "--fail",
        action=_STORE_TRUE,
        help="task_done: outcome.ok=false (default is success)",
    )
    p_alert.add_argument(
        "--summary",
        default=None,
        help="task_done: short outcome summary (no secrets)",
    )
    p_alert.set_defaults(_handler="alert")

    p_cap = sub.add_parser(
        _HANDLER_CAPTCHA,
        help="CAPTCHA chip: started|finished|failed (escalate unsolved → need_human)",
    )
    p_cap.add_argument(
        "event",
        choices=["started", "finished", "failed"],
        help="solving lifecycle (aliases: captcha_solving_*)",
    )
    p_cap.add_argument(_OPT_LEASE_ID, required=True, help=_HELP_LEASE_ID)
    p_cap.add_argument(_OPT_DETAIL, default=None, help=_HELP_DETAIL)
    p_cap.add_argument(
        "--provider",
        default=None,
        help="Optional stub/detect label (not a paid SaaS integration)",
    )
    p_cap.add_argument(
        "--timeout-s",
        type=int,
        default=None,
        help="Unsolved escalate timeout (default SLIPSTREAM_CAPTCHA_TIMEOUT=60)",
    )
    _add_url(p_cap)
    p_cap.set_defaults(_handler=_HANDLER_CAPTCHA)


    # --- downloads (session artifacts) ---
    p_dl = sub.add_parser(
        _HANDLER_DOWNLOADS,
        help="List/get lease-scoped download artifacts (no absolute paths)",
    )
    dl_sub = p_dl.add_subparsers(dest="downloads_cmd", metavar="DL_CMD")
    p_dll = dl_sub.add_parser("list", help="GET /v1/leases/{id}/downloads")
    _add_url(p_dll)
    p_dll.add_argument(_OPT_LEASE_ID, required=True)
    p_dll.add_argument(_OPT_AGENT_ID, required=True, help=_HELP_AGENT_ID_REQUIRED)
    p_dll.add_argument(
        "--rel-path",
        action=_STORE_TRUE,
        help="Include lease-relative path (never absolute)",
    )
    p_dll.set_defaults(_handler="downloads_list")
    p_dlg = dl_sub.add_parser("get", help="GET /v1/leases/{id}/downloads/{artifact_id}")
    _add_url(p_dlg)
    p_dlg.add_argument(_OPT_LEASE_ID, required=True)
    p_dlg.add_argument("--artifact-id", required=True)
    p_dlg.add_argument("-o", "--output", default=None, help="Write bytes to file")
    p_dlg.add_argument(_OPT_AGENT_ID, required=True, help=_HELP_AGENT_ID_REQUIRED)
    p_dlg.set_defaults(_handler="downloads_get")

    # --- uploads (thin drop) ---
    p_up = sub.add_parser(
        _HANDLER_UPLOADS,
        help="Thin lease-scoped upload drop (list/put)",
    )
    up_sub = p_up.add_subparsers(dest="uploads_cmd", metavar="UP_CMD")
    p_upl = up_sub.add_parser("list", help="GET /v1/leases/{id}/uploads")
    _add_url(p_upl)
    p_upl.add_argument(_OPT_LEASE_ID, required=True)
    p_upl.add_argument(_OPT_AGENT_ID, required=True, help=_HELP_AGENT_ID_REQUIRED)
    p_upl.set_defaults(_handler="uploads_list")
    p_upp = up_sub.add_parser("put", help="POST /v1/leases/{id}/uploads")
    _add_url(p_upp)
    p_upp.add_argument(_OPT_LEASE_ID, required=True)
    p_upp.add_argument("--filename", required=True)
    p_upp.add_argument("--file", required=True, help="Local regular file to upload")
    p_upp.add_argument(_OPT_AGENT_ID, required=True, help=_HELP_AGENT_ID_REQUIRED)
    p_upp.set_defaults(_handler="uploads_put")



    # --- cred (bind/unbind/list/fill) ---
    p_cred = sub.add_parser(
        "cred",
        help="Space credential vault: bind/unbind/list/fill (secrets never to agent)",
    )
    cred_sub = p_cred.add_subparsers(dest="cred_cmd", metavar="CRED_CMD")

    p_bind = cred_sub.add_parser("bind", help="Bind secret to a Space (captain/local)")
    _add_url(p_bind)
    p_bind.add_argument(_OPT_SPACE_ID, required=True)
    p_bind.add_argument("--label", required=True)
    p_bind.add_argument("--origin", required=True)
    p_bind.add_argument("--username", required=True)
    p_bind.add_argument(
        "--secret",
        default=None,
        help="Secret on argv (refused unless SLIPSTREAM_ALLOW_SECRET_ARGV=1)",
    )
    p_bind.add_argument(
        "--secret-env",
        default=None,
        help="Read secret from environment variable NAME (preferred)",
    )
    p_bind.add_argument(
        "--secret-file",
        default=None,
        help="Read secret from file path (preferred)",
    )
    p_bind.add_argument(
        "--prompt",
        action=_STORE_TRUE,
        help="Prompt for secret via getpass (no echo)",
    )
    p_bind.set_defaults(_handler="cred_bind")

    p_unbind = cred_sub.add_parser("unbind", help="Unbind a credential from a Space")
    _add_url(p_unbind)
    p_unbind.add_argument(_OPT_SPACE_ID, required=True)
    p_unbind.add_argument("--cred-id", required=True)
    p_unbind.set_defaults(_handler="cred_unbind")

    p_clist = cred_sub.add_parser("list", help="List credential metadata (no secrets)")
    _add_url(p_clist)
    p_clist.add_argument(_OPT_SPACE_ID, required=True)
    p_clist.set_defaults(_handler="cred_list")

    p_fill = cred_sub.add_parser(
        "fill",
        help="Pool-side CDP fill (cred_id + selectors only; agent never sees secret)",
    )
    _add_url(p_fill)
    p_fill.add_argument(_OPT_LEASE_ID, required=True)
    p_fill.add_argument("--cred-id", required=True)
    p_fill.add_argument(
        "--fields",
        required=True,
        help='JSON object name→CSS selector, e.g. \'{"username":"#user","password":"#pass"}\'',
    )
    p_fill.set_defaults(_handler="cred_fill")

    # --- act (permission ladder) ---
    p_act = sub.add_parser(
        "act",
        help="Request gated act (fill|eval|download|upload|nav_irreversible); may need confirm",
    )
    _add_url(p_act)
    p_act.add_argument(_OPT_LEASE_ID, required=True)
    p_act.add_argument(
        "--category",
        required=True,
        choices=["fill", "eval", "download", "upload", "nav_irreversible"],
    )
    p_act.add_argument(
        "--summary",
        required=True,
        help="Short human-safe summary (no secrets)",
    )
    p_act.add_argument(
        "--confirm-interactive",
        action=_STORE_TRUE,
        help="Prompt y/N on TTY; Non-TTY auto-denies",
    )
    p_act.set_defaults(_handler="act")

    # --- confirm / deny ---
    p_confirm = sub.add_parser(
        "confirm",
        help="Confirm a pending gated action (allow once)",
    )
    _add_url(p_confirm)
    p_confirm.add_argument("confirm_id", help="confirm_id from confirmation_required")
    p_confirm.set_defaults(_handler="confirm")

    p_deny = sub.add_parser(
        "deny",
        help="Deny a pending gated action (fail closed)",
    )
    _add_url(p_deny)
    p_deny.add_argument("confirm_id", help="confirm_id from confirmation_required")
    p_deny.set_defaults(_handler="deny")

    # --- doctor ---


    # --- navigate ---
    p_nav = sub.add_parser(
        "navigate",
        help="Top-frame navigate on a lease (refused outside domain allowlist)",
    )
    _add_url(p_nav)
    p_nav.add_argument(_OPT_LEASE_ID, required=True)
    p_nav.add_argument("--page-url", required=True, help="http(s) URL to open")
    p_nav.set_defaults(_handler="navigate")

    # --- eval (Runtime.evaluate) ---
    p_eval = sub.add_parser(
        "eval",
        help="Runtime.evaluate on a lease (requires prior act+confirm for eval)",
    )
    _add_url(p_eval)
    p_eval.add_argument(_OPT_LEASE_ID, required=True)
    p_eval.add_argument(
        "--expression",
        required=True,
        help="JS expression for Runtime.evaluate (no secrets)",
    )
    p_eval.set_defaults(_handler="evaluate")

    # --- spaces ---
    p_spaces = sub.add_parser("spaces", help="Space registry: list/filter tags, set user_metadata")
    spaces_sub = p_spaces.add_subparsers(dest="spaces_cmd", metavar="SUBCOMMAND")
    p_sp_list = spaces_sub.add_parser("list", help="List Spaces (optional --q / --tag filter)")
    _add_url(p_sp_list)
    p_sp_list.add_argument("--q", default=None, help="Metadata query (key=value AND… / substring)")
    p_sp_list.add_argument(
        _OPT_TAG,
        action=_ACTION_APPEND,
        default=None,
        help="Filter tag key=value (repeatable; AND with --q)",
    )
    p_sp_list.set_defaults(_handler="spaces_list")
    p_sp_set = spaces_sub.add_parser("set", help="Set/replace Space user_metadata tags")
    _add_url(p_sp_set)
    p_sp_set.add_argument(_OPT_SPACE_ID, required=True, help=_HELP_SPACE_ID)
    p_sp_set.add_argument(
        "--metadata",
        default=None,
        help='user_metadata JSON object, e.g. \'{"env":"staging","team":"fleet"}\'',
    )
    p_sp_set.add_argument(
        _OPT_TAG,
        action=_ACTION_APPEND,
        default=None,
        help="Tag key=value (repeatable)",
    )
    p_sp_set.add_argument(
        "--allowed-domains",
        default=None,
        help="Comma-separated Space top-frame allowlist (omit to leave unchanged)",
    )
    p_sp_set.set_defaults(_handler="spaces_set")
    p_sp_si = spaces_sub.add_parser(
        "signed-in",
        help="Mark/unmark Space signed-in badge (profile cookies persist; no dump)",
    )
    _add_url(p_sp_si)
    p_sp_si.add_argument(_OPT_SPACE_ID, required=True, help=_HELP_SPACE_ID)
    p_sp_si.add_argument(
        _OPT_HOST,
        default=None,
        help="Optional host label for badge (e.g. github.com)",
    )
    p_sp_si.add_argument(
        "--clear",
        action=_STORE_TRUE,
        help="Unmark signed-in (clear badge)",
    )
    p_sp_si.set_defaults(_handler="spaces_signed_in")
    p_sp_lo = spaces_sub.add_parser(
        "login-once",
        help="Lease Space + need_human(reason=login) for Watch/Take-over",
    )
    _add_url(p_sp_lo)
    p_sp_lo.add_argument(_OPT_SPACE_ID, required=True, help=_HELP_SPACE_ID)
    p_sp_lo.add_argument(_OPT_AGENT_ID, required=True, help="Agent id for the lease")
    p_sp_lo.add_argument(_OPT_DETAIL, default=None, help="Human-safe detail string")
    p_sp_lo.add_argument(_OPT_HOST, default=None, help="Optional host hint for later badge")
    p_sp_lo.add_argument("--ttl-s", type=int, default=None, help="Watch TTL seconds")
    p_sp_lo.set_defaults(_handler="spaces_login_once")

    # --- sessions (ops list) ---
    p_sess = sub.add_parser(
        _HANDLER_SESSIONS,
        help="Ops session list: status, duration, tags, signed-in, watch presence",
    )
    _add_url(p_sess)
    p_sess.add_argument("--q", default=None, help="Metadata query filter")
    p_sess.add_argument(
        _OPT_TAG,
        action=_ACTION_APPEND,
        default=None,
        help="Filter tag key=value (repeatable; AND with --q)",
    )
    p_sess.add_argument(
        _OPT_JSON,
        dest=_DEST_AS_JSON,
        action=_STORE_TRUE,
        help="Print full JSON (includes secret watch_url when minted)",
    )
    p_sess.set_defaults(_handler=_HANDLER_SESSIONS)

    # --- leases list ---
    p_leases = sub.add_parser("leases", help="List/filter active leases by user_metadata")
    leases_sub = p_leases.add_subparsers(dest="leases_cmd", metavar="SUBCOMMAND")
    p_ls_list = leases_sub.add_parser("list", help="List active leases (optional --q / --tag)")
    _add_url(p_ls_list)
    p_ls_list.add_argument("--q", default=None, help="Metadata query (key=value AND… / substring)")
    p_ls_list.add_argument(
        _OPT_TAG,
        action=_ACTION_APPEND,
        default=None,
        help="Filter tag key=value (repeatable; AND with --q)",
    )
    p_ls_list.set_defaults(_handler="leases_list")

    p_doc = sub.add_parser(
        "doctor",
        help="Preflight: Chrome, CDP probe, pool healthz, spaces root, skill path",
    )
    _add_url(p_doc)
    p_doc.add_argument(
        _OPT_JSON,
        action=_STORE_TRUE,
        dest=_DEST_AS_JSON,
        help="Emit machine-readable JSON instead of human text",
    )
    p_doc.set_defaults(_handler="doctor")

    # --- watch-status ---
    p_ws = sub.add_parser(
        "watch-status",
        help="Compose /watch detection only (PASS/WARN; never hard-fail)",
    )
    p_ws.add_argument(
        _OPT_JSON,
        action=_STORE_TRUE,
        dest=_DEST_AS_JSON,
        help="Emit machine-readable JSON",
    )
    p_ws.set_defaults(_handler="watch_status")

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
            from slipstream.cli import _parse_metadata_json, _tags_to_metadata

            meta = _parse_metadata_json(getattr(args, "metadata", None)) or {}
            tag_meta = _tags_to_metadata(getattr(args, "tag", None)) or {}
            meta.update(tag_meta)
            domains = None
            raw_dom = getattr(args, "allowed_domains", None)
            if raw_dom is not None:
                from slipstream.domains import parse_allowed_domains

                domains = parse_allowed_domains(raw_dom)
            return cmd_lease(
                agent_id=args.agent_id,
                space_id=args.space_id,
                ttl_seconds=args.ttl_seconds,
                url=args.url,
                user_metadata=meta or None,
                allowed_domains=domains,
            )
        if args._handler == "navigate":
            return cmd_navigate(
                lease_id=args.lease_id,
                page_url=args.page_url,
                url=args.url,
            )
        if args._handler == "evaluate":
            return cmd_evaluate(
                lease_id=args.lease_id,
                expression=args.expression,
                url=args.url,
            )
        if args._handler == "spaces_list":
            return cmd_spaces_list(q=args.q, tags=args.tag, url=args.url)
        if args._handler == "spaces_set":
            sp_domains = None
            raw_sp = getattr(args, "allowed_domains", None)
            if raw_sp is not None:
                from slipstream.domains import parse_allowed_domains

                sp_domains = parse_allowed_domains(raw_sp)
            return cmd_spaces_set(
                space_id=args.space_id,
                metadata_json=args.metadata,
                tags=args.tag,
                allowed_domains=sp_domains,
                url=args.url,
            )
        if args._handler == "spaces_signed_in":
            return cmd_spaces_signed_in(
                space_id=args.space_id,
                signed_in=not args.clear,
                host=args.host,
                url=args.url,
            )
        if args._handler == "spaces_login_once":
            return cmd_spaces_login_once(
                space_id=args.space_id,
                agent_id=args.agent_id,
                detail=args.detail,
                host=args.host,
                ttl_s=args.ttl_s,
                url=args.url,
            )
        if args._handler == _HANDLER_SESSIONS:
            return cmd_sessions(
                q=args.q, tags=args.tag, url=args.url, as_json=args.as_json
            )
        if args._handler == "leases_list":
            return cmd_leases_list(q=args.q, tags=args.tag, url=args.url)
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
        if args._handler == _HANDLER_CAPTCHA:
            return cmd_captcha(
                event=args.event,
                lease_id=args.lease_id,
                detail=args.detail,
                provider=args.provider,
                timeout_s=args.timeout_s,
                url=args.url,
            )
        if args._handler == "downloads_list":
            return cmd_downloads_list(
                lease_id=args.lease_id,
                url=args.url,
                agent_id=args.agent_id,
                rel_path=bool(getattr(args, "rel_path", False)),
            )
        if args._handler == "downloads_get":
            return cmd_downloads_get(
                lease_id=args.lease_id,
                artifact_id=args.artifact_id,
                output=args.output,
                url=args.url,
                agent_id=args.agent_id,
            )
        if args._handler == "uploads_list":
            return cmd_uploads_list(
                lease_id=args.lease_id,
                url=args.url,
                agent_id=args.agent_id,
            )
        if args._handler == "uploads_put":
            return cmd_uploads_put(
                lease_id=args.lease_id,
                filename=args.filename,
                file_path=args.file,
                url=args.url,
                agent_id=args.agent_id,
            )
        if args._handler == "cred_bind":
            secret = resolve_secret(
                secret=args.secret,
                secret_env=args.secret_env,
                secret_file=args.secret_file,
                prompt=bool(args.prompt),
            )
            return cmd_cred_bind(
                space_id=args.space_id,
                label=args.label,
                origin=args.origin,
                username=args.username,
                secret=secret,
                url=args.url,
            )
        if args._handler == "cred_unbind":
            return cmd_cred_unbind(
                space_id=args.space_id,
                cred_id=args.cred_id,
                url=args.url,
            )
        if args._handler == "cred_list":
            return cmd_cred_list(space_id=args.space_id, url=args.url)
        if args._handler == "cred_fill":
            return cmd_cred_fill(
                lease_id=args.lease_id,
                cred_id=args.cred_id,
                fields_json=args.fields,
                url=args.url,
            )
        if args._handler == "act":
            return cmd_act(
                lease_id=args.lease_id,
                category=args.category,
                summary=args.summary,
                url=args.url,
                confirm_interactive=bool(args.confirm_interactive),
            )
        if args._handler == "confirm":
            return cmd_confirm(confirm_id=args.confirm_id, url=args.url)
        if args._handler == "deny":
            return cmd_deny(confirm_id=args.confirm_id, url=args.url)
        if args._handler == "doctor":
            return cmd_doctor(url=args.url, as_json=args.as_json)
        if args._handler == "watch_status":
            return cmd_watch_status(as_json=args.as_json)
    except CliError as e:
        print(e, file=sys.stderr)
        return e.exit_code

    parser.print_help(sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
