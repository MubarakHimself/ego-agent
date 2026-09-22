"""Agent-facing HTTP client CLI for a running Slipstream pool.

Talks to the pool JSON API (default http://127.0.0.1:8755). Prefer
SLIPSTREAM_URL for the base URL. Stdlib urllib only — no third-party HTTP.
"""

from __future__ import annotations

import ipaddress
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


DEFAULT_URL = "http://127.0.0.1:8755"


class CliError(Exception):
    """CLI-level failure (bad args, HTTP error, connection)."""

    def __init__(self, message: str, exit_code: int = 1):
        super().__init__(message)
        self.exit_code = exit_code


def _host_is_loopback(host: str) -> bool:
    """True for localhost / 127.0.0.0/8 / ::1 (literal hosts only)."""
    h = host.strip().lower().strip("[]")
    if h in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        return bool(ipaddress.ip_address(h).is_loopback)
    except ValueError:
        return False


def assert_url_allowed(url: str) -> None:
    """Refuse non-loopback pool URLs unless SLIPSTREAM_ALLOW_REMOTE_URL=1."""
    if os.environ.get("SLIPSTREAM_ALLOW_REMOTE_URL", "") == "1":
        return
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname
    if not host or not _host_is_loopback(host):
        raise CliError(
            f"refusing non-loopback pool URL {url!r}; "
            "use 127.0.0.1 / ::1 / localhost, or set SLIPSTREAM_ALLOW_REMOTE_URL=1",
            exit_code=2,
        )


def resolve_base_url(url: str | None = None) -> str:
    """Resolve pool base URL: --url > SLIPSTREAM_URL > default.

    Default allowlist is loopback only (127.0.0.1 / ::1 / localhost).
    Escape hatch: SLIPSTREAM_ALLOW_REMOTE_URL=1.
    """
    raw = (url or os.environ.get("SLIPSTREAM_URL") or DEFAULT_URL).strip().rstrip("/")
    assert_url_allowed(raw)
    return raw


def _request(
    method: str,
    url: str,
    body: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> tuple[int, dict[str, Any]]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers: dict[str, str] = {}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            parsed: dict[str, Any] = json.loads(raw) if raw else {}
            return resp.status, parsed
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {"error": "http_error", "detail": raw or e.reason}
        return e.code, parsed
    except urllib.error.URLError as e:
        raise CliError(f"connection failed: {e.reason}", exit_code=1) from e
    except TimeoutError as e:
        raise CliError(f"request timed out contacting {url}", exit_code=1) from e
    except json.JSONDecodeError as e:
        raise CliError(f"invalid JSON from pool: {e}", exit_code=1) from e


def _print_json(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _fail_http(status: int, payload: dict[str, Any]) -> None:
    detail = payload.get("detail") or payload.get("error") or payload
    msg = f"HTTP {status}: {detail if isinstance(detail, str) else json.dumps(detail)}"
    raise CliError(msg, exit_code=1)


def cmd_lease(
    *,
    agent_id: str,
    space_id: str,
    ttl_seconds: int | None = None,
    url: str | None = None,
) -> int:
    base = resolve_base_url(url)
    body: dict[str, Any] = {"agent_id": agent_id, "space_id": space_id}
    if ttl_seconds is not None:
        body["ttl_seconds"] = ttl_seconds
    status, payload = _request("POST", f"{base}/v1/leases", body)
    if status != 200:
        _fail_http(status, payload)
    _print_json(payload)
    return 0


def cmd_heartbeat(*, lease_id: str, url: str | None = None) -> int:
    base = resolve_base_url(url)
    status, payload = _request("POST", f"{base}/v1/leases/{lease_id}/heartbeat", {})
    if status != 200:
        _fail_http(status, payload)
    _print_json(payload)
    return 0


def cmd_release(
    *,
    lease_id: str,
    reason: str | None = None,
    url: str | None = None,
) -> int:
    base = resolve_base_url(url)
    body = {"reason": reason} if reason else None
    status, payload = _request("DELETE", f"{base}/v1/leases/{lease_id}", body)
    if status != 200:
        _fail_http(status, payload)
    _print_json(payload)
    return 0


def cmd_status(*, url: str | None = None) -> int:
    base = resolve_base_url(url)
    status, payload = _request("GET", f"{base}/v1/pool/status")
    if status != 200:
        _fail_http(status, payload)
    _print_json(payload)
    return 0


def cmd_alert(
    *,
    kind: str,
    lease_id: str,
    reason: str | None = None,
    detail: str | None = None,
    task_id: str | None = None,
    ttl_s: int | None = None,
    ok: bool | None = None,
    summary: str | None = None,
    watch_url: str | None = None,
    url: str | None = None,
) -> int:
    """POST /v1/leases/{id}/alerts — need_human or task_done."""
    base = resolve_base_url(url)
    kind_norm = kind.strip().lower().replace("_", "-")
    if kind_norm in ("need-human", "needhuman"):
        event = "need_human"
        body: dict[str, Any] = {
            "event": event,
            "reason": reason or "other",
            "detail": detail or "",
        }
    elif kind_norm in ("done", "task-done", "task_done"):
        event = "task_done"
        body = {
            "event": event,
            "detail": detail or "",
            "outcome": {
                "ok": True if ok is None else bool(ok),
                "summary": summary or "",
            },
        }
        if reason:
            body["reason"] = reason
    else:
        raise CliError(
            f"unknown alert kind {kind!r}; use need-human or done",
            exit_code=2,
        )

    if task_id:
        body["task_id"] = task_id
    if ttl_s is not None:
        body["ttl_s"] = ttl_s
    if watch_url:
        body["watch_url"] = watch_url

    status, payload = _request("POST", f"{base}/v1/leases/{lease_id}/alerts", body)
    if status != 200:
        _fail_http(status, payload)
    _print_json(payload)
    return 0
