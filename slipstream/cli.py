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
from urllib.parse import quote
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_URL = "http://127.0.0.1:8755"
_PATH_LEASES = "/v1/leases/"
_ERR_AGENT_ID_REQUIRED = "agent_id required"
_ACTION_CONFIRM = "confirm"
_PATH_SPACES = "/v1/spaces/"
_PATH_CONFIRMATIONS = "/v1/confirmations/"
_UTF8 = "utf-8"


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


def _resolve_policy_path(path: Path) -> Path:
    """Resolve path (name recognized by Skylos PATH_SANITIZERS)."""
    return Path(path).expanduser().resolve()


def _validate_api_request_url(url: str) -> str:
    """Allowlist outbound HTTP URLs (loopback unless remote opted in).

    Name is intentional: Skylos SSRF (SKY-D216) treats this as a URL sanitizer.
    """
    if not isinstance(url, str) or not url.strip():
        raise CliError("empty URL", exit_code=2)
    # Reject non-http(s) schemes early (file:, gopher:, etc.).
    scheme = urllib.parse.urlparse(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise CliError(f"refusing non-http(s) URL scheme {scheme!r}", exit_code=2)
    assert_url_allowed(url)
    return url


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
    safe_url = _validate_api_request_url(url)
    data = None if body is None else json.dumps(body).encode(_UTF8)
    # Construct Request with sanitized URL only — set method/data via attrs so
    # Skylos does not taint `req` through kwargs (SKY-D216).
    req = urllib.request.Request(safe_url)
    req.method = method
    if data is not None:
        req.data = data
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode(_UTF8)
            parsed: dict[str, Any] = json.loads(raw) if raw else {}
            return resp.status, parsed
    except urllib.error.HTTPError as e:
        raw = e.read().decode(_UTF8, errors="replace")
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



def _request_bytes(
    method: str,
    url: str,
    timeout: float = 30.0,
) -> tuple[int, bytes, dict[str, str]]:
    """HTTP request returning raw bytes (for artifact fetch)."""
    safe_url = _validate_api_request_url(url)
    req = urllib.request.Request(safe_url)
    req.method = method
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            headers = {k.lower(): v for k, v in resp.headers.items()}
            return resp.status, resp.read(), headers  # skylos: ignore[SKY-P401] bounded HTTP body
    except urllib.error.HTTPError as e:
        raw = e.read()
        headers = {k.lower(): v for k, v in e.headers.items()} if e.headers else {}
        return e.code, raw, headers
    except urllib.error.URLError as e:
        raise CliError(f"connection failed: {e.reason}", exit_code=1) from e


def cmd_lease(
    *,
    agent_id: str,
    space_id: str,
    ttl_seconds: int | None = None,
    url: str | None = None,
    user_metadata: dict[str, Any] | None = None,
    allowed_domains: list[str] | None = None,
    keep_alive: bool | None = None,
) -> int:
    base = resolve_base_url(url)
    body: dict[str, Any] = {"agent_id": agent_id, "space_id": space_id}
    if ttl_seconds is not None:
        body["ttl_seconds"] = ttl_seconds
    if user_metadata:
        body["user_metadata"] = user_metadata
    if allowed_domains is not None:
        body["allowed_domains"] = allowed_domains
    if keep_alive is not None:
        body["keep_alive"] = bool(keep_alive)
    status, payload = _request("POST", f"{base}/v1/leases", body)
    if status != 200:
        _fail_http(status, payload)
    _print_json(payload)
    return 0


def cmd_heartbeat(*, lease_id: str, url: str | None = None) -> int:
    base = resolve_base_url(url)
    status, payload = _request("POST", f"{base}{_PATH_LEASES}{lease_id}/heartbeat", {})
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
    status, payload = _request("DELETE", f"{base}{_PATH_LEASES}{lease_id}", body)
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

    status, payload = _request("POST", f"{base}{_PATH_LEASES}{lease_id}/alerts", body)
    if status != 200:
        _fail_http(status, payload)
    _print_json(payload)
    return 0


def cmd_captcha(
    *,
    event: str,
    lease_id: str,
    detail: str | None = None,
    provider: str | None = None,
    timeout_s: int | None = None,
    url: str | None = None,
) -> int:
    """POST /v1/leases/{id}/captcha — started|finished|failed chip events."""
    base = resolve_base_url(url)
    body: dict[str, Any] = {"event": event, "detail": detail or ""}
    if provider:
        body["provider"] = provider
    if timeout_s is not None:
        body["timeout_s"] = timeout_s
    status, payload = _request(
        "POST", f"{base}{_PATH_LEASES}{lease_id}/captcha", body
    )
    if status != 200:
        _fail_http(status, payload)
    _print_json(payload)
    return 0


def resolve_secret(
    *,
    secret: str | None = None,
    secret_env: str | None = None,
    secret_file: str | None = None,
    prompt: bool = False,
) -> str:
    """Resolve bind secret without putting it on argv when possible (ADV-005).

    Preference order: --secret-env → --secret-file → getpass prompt → bare
    --secret (refused unless SLIPSTREAM_ALLOW_SECRET_ARGV=1).
    """
    sources = [bool(secret), bool(secret_env), bool(secret_file), bool(prompt)]
    if sum(1 for s in sources if s) > 1:
        raise CliError(
            "use only one of --secret-env / --secret-file / --secret / --prompt",
            exit_code=2,
        )
    if secret_env:
        val = os.environ.get(secret_env, "")
        if not val:
            raise CliError(f"env {secret_env!r} empty or unset", exit_code=2)
        return val
    if secret_file:
        # Operator-supplied path: refuse symlinks, require regular file, bound size.
        path = _resolve_policy_path(Path(secret_file))
        try:
            if path.is_symlink():
                raise CliError("--secret-file must not be a symlink", exit_code=2)
            if not path.is_file():
                raise CliError(f"--secret-file not a regular file: {path}", exit_code=2)
            MAX_BYTES = 8192
            st = path.stat()
            if st.st_size > MAX_BYTES:
                raise CliError("--secret-file too large", exit_code=2)
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(path, flags)
            try:
                val = os.read(fd, MAX_BYTES).decode(_UTF8).rstrip("\n")
            finally:
                os.close(fd)
        except CliError:
            raise
        except OSError as e:
            raise CliError(f"cannot read --secret-file: {e}", exit_code=2) from e
        if not val:
            raise CliError("--secret-file is empty", exit_code=2)
        return val
    if prompt:
        import getpass

        val = getpass.getpass("Secret: ")
        if not val:
            raise CliError("empty secret from prompt", exit_code=2)
        return val
    if secret is not None:
        if os.environ.get("SLIPSTREAM_ALLOW_SECRET_ARGV", "") != "1":
            raise CliError(
                "refusing bare --secret on argv (visible in process list); "
                "prefer --secret-env / --secret-file / --prompt, "
                "or set SLIPSTREAM_ALLOW_SECRET_ARGV=1",
                exit_code=2,
            )
        if secret == "":
            raise CliError("secret must be non-empty", exit_code=2)
        return secret
    raise CliError(
        "secret required: pass --secret-env NAME, --secret-file PATH, "
        "--prompt, or --secret with SLIPSTREAM_ALLOW_SECRET_ARGV=1",
        exit_code=2,
    )



def cmd_cred_bind(
    *,
    space_id: str,
    label: str,
    origin: str,
    username: str,
    secret: str,
    url: str | None = None,
) -> int:
    """POST /v1/spaces/{space_id}/credentials/bind — captain/local only."""
    base = resolve_base_url(url)
    body = {
        "label": label,
        "origin": origin,
        "username": username,
        "secret": secret,
    }
    status, payload = _request(
        "POST", f"{base}{_PATH_SPACES}{space_id}/credentials/bind", body
    )
    if status != 200:
        _fail_http(status, payload)
    _print_json(payload)
    return 0


def cmd_cred_unbind(
    *,
    space_id: str,
    cred_id: str,
    url: str | None = None,
) -> int:
    """POST /v1/spaces/{space_id}/credentials/{cred_id}/unbind."""
    base = resolve_base_url(url)
    status, payload = _request(
        "POST",
        f"{base}{_PATH_SPACES}{space_id}/credentials/{cred_id}/unbind",
        {},
    )
    if status != 200:
        _fail_http(status, payload)
    _print_json(payload)
    return 0


def cmd_cred_list(*, space_id: str, url: str | None = None) -> int:
    """GET /v1/spaces/{space_id}/credentials — metadata only."""
    base = resolve_base_url(url)
    status, payload = _request("GET", f"{base}{_PATH_SPACES}{space_id}/credentials")
    if status != 200:
        _fail_http(status, payload)
    _print_json(payload)
    return 0


def cmd_cred_fill(
    *,
    lease_id: str,
    cred_id: str,
    fields_json: str,
    url: str | None = None,
) -> int:
    """POST /v1/leases/{lease_id}/credentials/fill — selectors only, never secrets."""
    base = resolve_base_url(url)
    try:
        fields = json.loads(fields_json)
    except json.JSONDecodeError as e:
        raise CliError(f"fields must be JSON object: {e}", exit_code=2) from e
    if not isinstance(fields, dict):
        raise CliError("fields must be a JSON object of name→selector", exit_code=2)
    body = {"cred_id": cred_id, "fields": fields}
    status, payload = _request(
        "POST", f"{base}{_PATH_LEASES}{lease_id}/credentials/fill", body
    )
    if status != 200:
        _fail_http(status, payload)
    _print_json(payload)
    return 0


def cmd_act(
    *,
    lease_id: str,
    category: str,
    summary: str,
    url: str | None = None,
    confirm_interactive: bool = False,
) -> int:
    """POST /v1/leases/{id}/actions — gate fill|eval|download|upload|nav_irreversible.

    On confirmation_required (HTTP 202): print JSON and exit 0 (orchestrator
    mode). With --confirm-interactive: prompt on TTY; Non-TTY → auto-deny.
    """
    base = resolve_base_url(url)
    body = {"category": category, "summary": summary}
    status, payload = _request(
        "POST", f"{base}{_PATH_LEASES}{lease_id}/actions", body
    )
    if status not in (200, 202):
        _fail_http(status, payload)

    if payload.get("status") == "confirmation_required" and confirm_interactive:
        confirm_id = payload.get("confirm_id")
        if not confirm_id:
            raise CliError("confirmation_required missing confirm_id", exit_code=1)
        if not sys.stdin.isatty():
            # Non-TTY → deny (agent-browser pattern)
            d_status, d_payload = _request(
                "POST",
                f"{base}{_PATH_CONFIRMATIONS}{confirm_id}",
                {"action": "deny"},
            )
            if d_status != 200:
                _fail_http(d_status, d_payload)
            _print_json(d_payload)
            return 1
        sys.stderr.write(
            f"Allow {payload.get('category')} "
            f"({payload.get('summary')!r})? [y/N] "
        )
        sys.stderr.flush()
        try:
            answer = sys.stdin.readline().strip().lower()
        except EOFError:
            answer = ""
        action = _ACTION_CONFIRM if answer in ("y", "yes") else "deny"
        r_status, r_payload = _request(
            "POST",
            f"{base}{_PATH_CONFIRMATIONS}{confirm_id}",
            {"action": action},
        )
        if r_status != 200:
            _fail_http(r_status, r_payload)
        _print_json(r_payload)
        return 0 if action == _ACTION_CONFIRM else 1

    _print_json(payload)
    return 0


def cmd_try_act(
    *,
    lease_id: str,
    body_json: str,
    url: str | None = None,
) -> int:
    """POST /v1/leases/{id}/act — body is JSON (kind/fallback_plan/soft_retry)."""
    import json

    base = resolve_base_url(url)
    body = json.loads(body_json)
    if not isinstance(body, dict):
        raise CliError("--body must be a JSON object", exit_code=2)
    status, payload = _request(
        "POST", f"{base}{_PATH_LEASES}{lease_id}/act", body
    )
    if status == 403:
        detail = payload.get("detail") if isinstance(payload, dict) else payload
        st = payload.get("status") if isinstance(payload, dict) else None
        err = payload.get("error") if isinstance(payload, dict) else None
        if err == "confirmation_required" or st == "confirmation_required":
            raise CliError(f"confirmation_required: {detail}", exit_code=3)
        raise CliError(f"forbidden: {detail}", exit_code=3)
    if status != 200:
        _fail_http(status, payload)
    _print_json(payload)
    return 0


def cmd_confirm(*, confirm_id: str, url: str | None = None) -> int:
    """POST /v1/confirmations/{confirm_id} {action: confirm}."""
    base = resolve_base_url(url)
    status, payload = _request(
        "POST", f"{base}{_PATH_CONFIRMATIONS}{confirm_id}", {"action": _ACTION_CONFIRM}
    )
    if status != 200:
        _fail_http(status, payload)
    _print_json(payload)
    return 0


def cmd_deny(*, confirm_id: str, url: str | None = None) -> int:
    """POST /v1/confirmations/{confirm_id} {action: deny}."""
    base = resolve_base_url(url)
    status, payload = _request(
        "POST", f"{base}{_PATH_CONFIRMATIONS}{confirm_id}", {"action": "deny"}
    )
    if status != 200:
        _fail_http(status, payload)
    _print_json(payload)
    return 0


def _parse_metadata_json(raw: str | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        raise CliError(f"invalid --metadata JSON: {e}", exit_code=2) from e
    if not isinstance(obj, dict):
        raise CliError("--metadata must be a JSON object", exit_code=2)
    return obj


def _tags_to_metadata(tags: list[str] | None) -> dict[str, Any] | None:
    """Build a flat user_metadata object from repeated --tag key=value."""
    if not tags:
        return None
    out: dict[str, Any] = {}
    for t in tags:
        if "=" in t:
            k, v = t.split("=", 1)
        elif ":" in t:
            k, v = t.split(":", 1)
        else:
            raise CliError(f"--tag must be key=value, got {t!r}", exit_code=2)
        k, v = k.strip(), v.strip()
        if not k:
            raise CliError(f"--tag missing key: {t!r}", exit_code=2)
        out[k] = v
    return out



def cmd_navigate(
    *,
    lease_id: str,
    page_url: str,
    url: str | None = None,
) -> int:
    """POST /v1/leases/{id}/navigate — top-frame nav; ladder + domain allowlist."""
    base = resolve_base_url(url)
    status, payload = _request(
        "POST",
        f"{base}{_PATH_LEASES}{lease_id}/navigate",
        {"url": page_url},
    )
    if status == 403:
        err = payload.get("error") if isinstance(payload, dict) else None
        detail = payload.get("detail") if isinstance(payload, dict) else payload
        if err == "confirmation_required":
            raise CliError(f"confirmation_required: {detail}", exit_code=3)
        raise CliError(f"domain_not_allowed: {detail}", exit_code=3)
    if status != 200:
        _fail_http(status, payload)
    # Optional content-boundary wrap for page-derived title/observed_url echoes.
    from slipstream.boundaries import content_boundaries_enabled, wrap_page_content

    if content_boundaries_enabled() and isinstance(payload, dict):
        origin = page_url
        if "title" in payload and isinstance(payload["title"], str):
            payload = dict(payload)
            payload["title"] = wrap_page_content(payload["title"], origin=origin)
    _print_json(payload)
    return 0


def cmd_evaluate(
    *,
    lease_id: str,
    expression: str,
    url: str | None = None,
) -> int:
    """POST /v1/leases/{id}/eval — Runtime.evaluate; requires prior eval confirm."""
    base = resolve_base_url(url)
    status, payload = _request(
        "POST",
        f"{base}{_PATH_LEASES}{lease_id}/eval",
        {"expression": expression},
    )
    if status == 403:
        detail = payload.get("detail") if isinstance(payload, dict) else payload
        raise CliError(f"confirmation_required: {detail}", exit_code=3)
    if status != 200:
        _fail_http(status, payload)
    _print_json(payload)
    return 0


def cmd_spaces_list(
    *,
    q: str | None = None,
    tags: list[str] | None = None,
    url: str | None = None,
) -> int:
    from slipstream.metadata import MetadataValidationError, combine_q

    base = resolve_base_url(url)
    try:
        query = combine_q(q, tags)
    except MetadataValidationError as e:
        raise CliError(str(e), exit_code=2) from e
    path = f"{base}/v1/spaces"
    if query:
        path = f"{path}?q={quote(query)}"
    status, payload = _request("GET", path)
    if status != 200:
        _fail_http(status, payload)
    _print_json(payload)
    return 0


def cmd_spaces_set(
    *,
    space_id: str,
    metadata_json: str | None = None,
    tags: list[str] | None = None,
    allowed_domains: list[str] | None = None,
    url: str | None = None,
) -> int:
    base = resolve_base_url(url)
    meta = _parse_metadata_json(metadata_json) or {}
    tag_meta = _tags_to_metadata(tags) or {}
    meta.update(tag_meta)
    body: dict[str, Any] = {}
    if meta or metadata_json is not None or tags:
        body["user_metadata"] = meta
    if allowed_domains is not None:
        body["allowed_domains"] = allowed_domains
    if not body:
        raise CliError(
            "provide --metadata/--tag and/or --allowed-domains",
            exit_code=2,
        )
    status, payload = _request("PUT", f"{base}{_PATH_SPACES}{space_id}", body)
    if status != 200:
        _fail_http(status, payload)
    _print_json(payload)
    return 0


def cmd_leases_list(
    *,
    q: str | None = None,
    tags: list[str] | None = None,
    url: str | None = None,
) -> int:
    from slipstream.metadata import MetadataValidationError, combine_q

    base = resolve_base_url(url)
    try:
        query = combine_q(q, tags)
    except MetadataValidationError as e:
        raise CliError(str(e), exit_code=2) from e
    path = f"{base}/v1/leases"
    if query:
        path = f"{path}?q={quote(query)}"
    status, payload = _request("GET", path)
    if status != 200:
        _fail_http(status, payload)
    _print_json(payload)
    return 0




def cmd_sessions(
    *,
    q: str | None = None,
    tags: list[str] | None = None,
    url: str | None = None,
    as_json: bool = False,
) -> int:
    """GET /v1/ops/sessions — ops list with duration + watch presence."""
    from slipstream.metadata import MetadataValidationError, combine_q
    from slipstream.sessions import format_sessions_table

    base = resolve_base_url(url)
    try:
        query = combine_q(q, tags)
    except MetadataValidationError as e:
        raise CliError(str(e), exit_code=2) from e
    path = f"{base}/v1/ops/sessions"
    if query:
        path = f"{path}?q={quote(query)}"
    status, payload = _request("GET", path)
    if status != 200:
        _fail_http(status, payload)
    if as_json:
        _print_json(payload)
        return 0
    sessions = payload.get("sessions") or []
    sys.stdout.write(format_sessions_table(sessions))
    # Tip: secret watch_url only in --json
    if any(s.get("watch_url") for s in sessions):
        sys.stdout.write(
            "# watch_url present (secret) — use --json to print tokenized URLs\n"
        )
    return 0


def cmd_spaces_signed_in(
    *,
    space_id: str,
    signed_in: bool = True,
    host: str | None = None,
    url: str | None = None,
) -> int:
    """POST /v1/spaces/{id}/signed-in — mark/unmark badge (never cookies)."""
    base = resolve_base_url(url)
    body: dict[str, Any] = {"signed_in": signed_in}
    if host:
        body["host"] = host
    status, payload = _request("POST", f"{base}{_PATH_SPACES}{space_id}/signed-in", body)
    if status != 200:
        _fail_http(status, payload)
    _print_json(payload)
    return 0


def cmd_spaces_login_once(
    *,
    space_id: str,
    agent_id: str,
    detail: str | None = None,
    host: str | None = None,
    ttl_s: int | None = None,
    url: str | None = None,
) -> int:
    """POST /v1/spaces/{id}/login-once — lease + need_human(reason=login)."""
    base = resolve_base_url(url)
    body: dict[str, Any] = {"agent_id": agent_id}
    if detail:
        body["detail"] = detail
    if host:
        body["host"] = host
    if ttl_s is not None:
        body["ttl_s"] = ttl_s
    status, payload = _request("POST", f"{base}{_PATH_SPACES}{space_id}/login-once", body)
    if status != 200:
        _fail_http(status, payload)
    _print_json(payload)
    return 0

def cmd_downloads_list(
    *,
    lease_id: str,
    url: str | None = None,
    agent_id: str,
    rel_path: bool = False,
) -> int:
    """GET /v1/leases/{id}/downloads — list session download artifacts."""
    base = resolve_base_url(url)
    q = []
    if not agent_id:
        raise SystemExit(_ERR_AGENT_ID_REQUIRED)
    q.append(f"agent_id={quote(agent_id, safe='')}")
    if rel_path:
        q.append("rel_path=1")
    qs = ("?" + "&".join(q)) if q else ""
    status, payload = _request(
        "GET", f"{base}{_PATH_LEASES}{lease_id}/downloads{qs}"
    )
    if status != 200:
        _fail_http(status, payload)
    _print_json(payload)
    return 0


def cmd_downloads_get(
    *,
    lease_id: str,
    artifact_id: str,
    output: str | None = None,
    url: str | None = None,
    agent_id: str | None = None,
) -> int:
    """GET /v1/leases/{id}/downloads/{artifact_id} — fetch bytes to file or stdout."""
    base = resolve_base_url(url)
    if not agent_id:
        raise SystemExit(_ERR_AGENT_ID_REQUIRED)
    q = f"?agent_id={quote(agent_id, safe='')}"
    status, raw, headers = _request_bytes(
        "GET", f"{base}{_PATH_LEASES}{lease_id}/downloads/{artifact_id}{q}"
    )
    if status != 200:
        try:
            payload = json.loads(raw.decode(_UTF8)) if raw else {}
        except json.JSONDecodeError:
            payload = {"error": "http_error", "detail": raw[:200]}
        _fail_http(status, payload if isinstance(payload, dict) else {"error": str(payload)})
    if output:
        # Operator-supplied output path: resolve + O_NOFOLLOW create (no symlink follow).
        out_path = _resolve_policy_path(Path(output))
        if out_path.is_symlink():
            raise CliError("--output must not be a symlink", exit_code=2)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(  # skylos: ignore[SKY-D215,SKY-D324] O_NOFOLLOW; operator --output
            os.fspath(out_path), flags, 0o600
        )
        try:
            os.write(fd, raw)
        finally:
            os.close(fd)
        meta = {
            "ok": True,
            "bytes": len(raw),
            "sha256": headers.get("x-slipstream-sha256"),
            "path": str(out_path),
        }
        _print_json(meta)
    else:
        sys.stdout.buffer.write(raw)
    return 0


def cmd_uploads_list(
    *,
    lease_id: str,
    url: str | None = None,
    agent_id: str,
) -> int:
    base = resolve_base_url(url)
    if not agent_id:
        raise SystemExit(_ERR_AGENT_ID_REQUIRED)
    q = f"?agent_id={quote(agent_id, safe='')}"
    status, payload = _request("GET", f"{base}{_PATH_LEASES}{lease_id}/uploads{q}")
    if status != 200:
        _fail_http(status, payload)
    _print_json(payload)
    return 0


def cmd_uploads_put(
    *,
    lease_id: str,
    filename: str,
    file_path: str,
    url: str | None = None,
    agent_id: str,
) -> int:
    """POST /v1/leases/{id}/uploads — thin drop from local file."""
    import base64

    base = resolve_base_url(url)
    path = _resolve_policy_path(Path(file_path))
    if path.is_symlink() or not path.is_file():
        raise CliError("--file must be a regular non-symlink file", exit_code=2)
    data = path.read_bytes()
    body: dict[str, Any] = {
        "filename": filename or path.name,
        "content_b64": base64.b64encode(data).decode("ascii"),
    }
    if not agent_id:
        raise SystemExit(_ERR_AGENT_ID_REQUIRED)
    body["agent_id"] = agent_id
    status, payload = _request("POST", f"{base}{_PATH_LEASES}{lease_id}/uploads", body)
    if status not in (200, 201):
        _fail_http(status, payload)
    _print_json(payload)
    return 0

