"""HTTP JSON API for the Browser Pool Manager (stdlib http.server).

Base URL default: http://127.0.0.1:8755

Endpoints:
  POST   /v1/leases
  POST   /v1/leases/{id}/heartbeat
  POST   /v1/leases/{id}/alerts
  POST   /v1/leases/{id}/captcha
  POST   /v1/leases/{id}/act
  POST   /v1/leases/{id}/actions
  POST   /v1/leases/{id}/navigate
  POST   /v1/leases/{id}/eval
  POST   /v1/leases/{id}/confirmations/{confirm_id}
  POST   /v1/leases/{id}/credentials/fill
  POST   /v1/leases/{id}/watch/confirm
  POST   /v1/leases/{id}/watch/input
  POST   /v1/leases/{id}/watch/cede
  POST   /v1/spaces/{space_id}/credentials/bind
  POST   /v1/spaces/{space_id}/credentials/{cred_id}/unbind
  POST   /v1/spaces/{space_id}/signed-in
  POST   /v1/spaces/{space_id}/login-once
  GET    /v1/spaces/{space_id}/credentials
  PUT    /v1/spaces/{space_id}
  POST   /v1/leases/{id}/watch/mark-signed-in
  GET    /v1/spaces?q=…
  GET    /v1/leases?q=…
  GET    /v1/ops/sessions
  GET    /v1/ops/
  GET    /v1/leases/{id}/watch
  GET    /v1/leases/{id}/watch/frame
  GET    /v1/leases/{id}/watch/events
  GET    /v1/leases/{id}/watch/timeline
  GET    /v1/leases/{id}/watch/evidence
  GET    /v1/leases/{id}/downloads
  GET    /v1/leases/{id}/downloads/{artifact_id}
  GET    /v1/leases/{id}/uploads
  GET    /v1/leases/{id}/uploads/{artifact_id}
  POST   /v1/leases/{id}/uploads
  DELETE /v1/leases/{id}
  GET    /v1/pool/status
  GET    /healthz
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from slipstream.alerts import AlertConflictError, AlertValidationError
from slipstream.domains import DomainAllowlistError
from slipstream.actions import ActFallbackValidationError
from slipstream.actions import (
    ActionValidationError,
    ConfirmationGoneError,
    ConfirmationNotFoundError,
    LadderGateError,
)
from slipstream.watch import (
    WATCH_CLICKJACK_HEADERS,
    WatchAuthError,
    WatchCaptureError,
    WatchForbiddenError,
    WatchGoneError,
    WatchInputError,
    WatchNotFoundError,
)
from slipstream.cdp_inject import CdpInjectError
from slipstream.tiers import AttachDisabledError, CloudProviderError, TierError
from slipstream.pool import (
    BrowserPool,
    LeaseExpiredError,
    LeaseNotFoundError,
    PoolFullError,
    SpaceInUseError,
    AttachDisabledError,
    TierError,
)
from slipstream.downloads import (
    KIND_DOWNLOADS,
        ArtifactNotFoundError,
    DownloadForbiddenError,
    DownloadValidationError,
    content_disposition_attachment,
)
from slipstream.vault import CredNotFoundError, VaultUnavailableError, VaultValidationError
from slipstream.metadata import MetadataValidationError
from slipstream.signed_in import SignedInValidationError
from slipstream.sessions import render_sessions_html

# --- quality-debt: shared path / header / error literals (SKY-L027) ---
_HDR_CONTENT_TYPE = "Content-Type"
_HDR_CONTENT_LENGTH = "Content-Length"
_UTF8 = "utf-8"
_PART_V1 = "v1"
_PART_LEASES = "leases"
_PART_SPACES = "spaces"
_PART_WATCH = "watch"
_PART_CREDENTIALS = "credentials"
_PART_DOWNLOADS = "downloads"
_PART_UPLOADS = "uploads"
_ERR_NOT_FOUND = "not_found"
_ERR_LEASE_NOT_FOUND = "lease_not_found"
_ERR_BAD_REQUEST = "bad_request"
_ERR_INVALID_JSON = "invalid_json"
_ERR_INVALID_ACTION = "invalid_action"
_ERR_INVALID_CREDENTIALS = "invalid_credentials"
_ERR_INVALID_SIGNED_IN = "invalid_signed_in"
_ERR_ALERT_CONFLICT = "alert_conflict"
_ERR_FORBIDDEN = "forbidden"
_ERR_CONFIRMATION_REQUIRED = "confirmation_required"
_ERR_LAUNCH_FAILED = "launch_failed"
_QS_AGENT_ID = "agent_id"
_QS_WATCH_AUTH = "token"  # skylos: ignore[SKY-L014,SKY-L032] query param name, not a secret
_FIELD_ALLOWED_DOMAINS = "allowed_domains"
_FIELD_USER_METADATA = "user_metadata"
_FIELD_KEEP_ALIVE = "keep_alive"
_FIELD_TIER = "tier"
_FIELD_MODE = "mode"
_FIELD_CDP_URL = "cdp_url"
_FIELD_CDP_PORT = "cdp_port"



def _ladder_gate_response(handler: BaseHTTPRequestHandler, exc: LadderGateError) -> None:
    _json_response(
        handler,
        403,
        {
            "error": _ERR_CONFIRMATION_REQUIRED,
            "category": exc.category,
            "detail": str(exc),
        },
    )


def _v1_resource_tail(parts: list[str], resource: str, *tail: str) -> str | None:
    """Return id when parts == [v1, resource, id, *tail]; else None."""
    if len(parts) != 3 + len(tail):
        return None
    if parts[0] != _PART_V1 or parts[1] != resource:
        return None
    if tuple(parts[3:]) != tail:
        return None
    return parts[2]


def _v1_lease_tail(parts: list[str], *tail: str) -> str | None:
    return _v1_resource_tail(parts, _PART_LEASES, *tail)


def _v1_space_tail(parts: list[str], *tail: str) -> str | None:
    return _v1_resource_tail(parts, _PART_SPACES, *tail)


def _drain_request_body(handler: BaseHTTPRequestHandler) -> None:
    length = int(handler.headers.get(_HDR_CONTENT_LENGTH, "0") or 0)
    if length > 0:
        handler.rfile.read(length)


def _watch_form_redirect(handler: BaseHTTPRequestHandler, lease_id: str, token: str | None) -> bool:
    """If HTML form POST, send 303 to Watch page; return True when handled."""
    content_type = (handler.headers.get(_HDR_CONTENT_TYPE) or "").lower()
    if "application/x-www-form-urlencoded" not in content_type:
        return False
    from urllib.parse import urlencode

    loc = (
        f"/v1/leases/{lease_id}/watch?"
        + urlencode({_QS_WATCH_AUTH: token or "", "mode": "takeover"})
    )
    handler.send_response(303)
    handler.send_header("Location", loc)
    handler.send_header(_HDR_CONTENT_LENGTH, "0")
    for hk, hv in WATCH_CLICKJACK_HEADERS.items():
        handler.send_header(hk, hv)
    handler.end_headers()
    return True


# ADV-003: shared refuse matcher for free-read secret / cookie dump paths (GET+POST)
_REFUSED_CRED_TAILS = frozenset(
    {"secret", "secrets", "cookies", "storage_state", "dump"}
)



def _parse_mark_json(raw: bytes) -> dict[str, Any]:
    if not raw:
        return {"signed_in": True}
    try:
        data = json.loads(raw.decode(_UTF8))
    except json.JSONDecodeError as e:
        raise ValueError("invalid_json") from e
    if not isinstance(data, dict):
        raise ValueError("invalid_json")
    if "signed_in" not in data:
        data = {**data, "signed_in": True}
    return data


def _parse_mark_form(raw: bytes) -> dict[str, Any]:
    from urllib.parse import parse_qs as _pqs

    parsed = _pqs(raw.decode(_UTF8, errors="replace"), keep_blank_values=True)
    host = (parsed.get("host") or [""])[0].strip()
    body: dict[str, Any] = {"signed_in": True}
    if host:
        body["host"] = host
    return body


def _read_watch_mark_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """JSON or form-urlencoded body for mark-signed-in (host label only)."""
    length = int(handler.headers.get(_HDR_CONTENT_LENGTH, "0") or 0)
    raw = handler.rfile.read(length) if length > 0 else b""
    ctype = (handler.headers.get(_HDR_CONTENT_TYPE) or "").lower()
    if "application/x-www-form-urlencoded" in ctype:
        return _parse_mark_form(raw)
    return _parse_mark_json(raw)


def is_refused_credentials_path(parts: list[str]) -> bool:
    """True when path looks like …/credentials/{secret|cookies|…} free-read."""
    return (
        len(parts) >= 4
        and _PART_CREDENTIALS in parts
        and parts[-1] in _REFUSED_CRED_TAILS
    )


def _refused_credentials_body() -> dict:
    return {
        "error": "refused",
        "detail": "free-read of secrets/cookies is not available; use fill",
    }


def _json_response(
    handler: BaseHTTPRequestHandler,
    status: int,
    body: dict[str, Any],
    *,
    extra_headers: dict[str, str] | None = None,
) -> None:
    raw = json.dumps(body).encode(_UTF8)
    handler.send_response(status)
    handler.send_header(_HDR_CONTENT_TYPE, "application/json")
    handler.send_header(_HDR_CONTENT_LENGTH, str(len(raw)))
    if extra_headers:
        for k, v in extra_headers.items():
            handler.send_header(k, v)
    handler.end_headers()
    handler.wfile.write(raw)


def _bytes_response(
    handler: BaseHTTPRequestHandler,
    status: int,
    body: bytes,
    content_type: str,
    *,
    extra_headers: dict[str, str] | None = None,
) -> None:
    handler.send_response(status)
    handler.send_header(_HDR_CONTENT_TYPE, content_type)
    handler.send_header(_HDR_CONTENT_LENGTH, str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    if extra_headers:
        for k, v in extra_headers.items():
            handler.send_header(k, v)
    handler.end_headers()
    handler.wfile.write(body)


def _watch_error(handler: BaseHTTPRequestHandler, exc: Exception) -> bool:
    """Map Watch* errors to HTTP; return True if handled."""
    if isinstance(exc, WatchAuthError):
        _json_response(
            handler, 401, {"error": "unauthorized", "detail": str(exc.detail)}
        )
        return True
    if isinstance(exc, WatchForbiddenError):
        _json_response(
            handler, 403, {"error": _ERR_FORBIDDEN, "detail": str(exc.detail)}
        )
        return True
    if isinstance(exc, WatchInputError):
        _json_response(
            handler, 400, {"error": "invalid_input", "detail": str(exc.detail)}
        )
        return True
    if isinstance(exc, WatchGoneError):
        _json_response(handler, 410, {"error": "gone", "detail": str(exc.detail)})
        return True
    if isinstance(exc, WatchNotFoundError):
        _json_response(
            handler,
            404,
            {"error": "watch_not_found", "lease_id": exc.lease_id},
        )
        return True
    if isinstance(exc, WatchCaptureError):
        # ADV-WATCH-001: never mock soft-fallback after auth — surface capture fail.
        _json_response(
            handler,
            502,
            {"error": "watch_capture_failed", "detail": str(exc)},
            extra_headers=WATCH_CLICKJACK_HEADERS,
        )
        return True
    return False


def _parse_ttl_seconds(raw: Any) -> int | None:
    """Require int (strict coerce of whole-number float); raise ValueError if bad."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise ValueError("ttl_seconds must be an integer")
    if isinstance(raw, int):
        ttl = raw
    elif isinstance(raw, float) and raw.is_integer():
        ttl = int(raw)
    else:
        raise ValueError("ttl_seconds must be an integer")
    if ttl <= 0:
        raise ValueError("ttl_seconds must be positive")
    return ttl


def _parse_keep_alive(raw: Any) -> bool | None:
    """Return explicit keep_alive, or None when omitted (new leases → false).

    Rejects non-bool (incl. 1/0) so clients do not silently coerce.
    Server env must not default keep_alive true on omit (ADV-KA-002).
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    raise ValueError("keep_alive must be a boolean")


_S_DOMAIN_NOT_ALLOWED = "domain_not_allowed"
_S_CDP_INJECT_FAILED = "cdp_inject_failed"

def make_handler(pool: BrowserPool):
    class PoolHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:  # quieter default
            pass

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get(_HDR_CONTENT_LENGTH, "0") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            if not raw:
                return {}
            return json.loads(raw.decode(_UTF8))

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path == "/healthz":
                _json_response(self, 200, {"ok": True})
                return
            if path == "/v1/pool/status":
                _json_response(self, 200, pool.status())
                return
            # GET /v1/spaces?q=… — list Spaces by user_metadata
            if path == "/v1/spaces":
                qs = parse_qs(urlparse(self.path).query)
                q = (qs.get("q") or [None])[0]
                _json_response(self, 200, pool.list_spaces(q=q))
                return
            # GET /v1/leases?q=… — list active leases by effective user_metadata
            if path == "/v1/leases":
                qs = parse_qs(urlparse(self.path).query)
                q = (qs.get("q") or [None])[0]
                _json_response(self, 200, pool.list_leases(q=q))
                return
            # GET /v1/ops/sessions — thin ops session list (JSON)
            if path == "/v1/ops/sessions":
                qs = parse_qs(urlparse(self.path).query)
                q = (qs.get("q") or [None])[0]
                _json_response(self, 200, pool.list_sessions(q=q))
                return
            # GET /v1/ops/ — thin HTML ops page (loopback)
            if path in ("/v1/ops", "/v1/ops/"):
                qs = parse_qs(urlparse(self.path).query)
                q = (qs.get("q") or [None])[0]
                payload = pool.list_sessions(q=q)
                html_body = render_sessions_html(payload.get("sessions") or [])
                _bytes_response(
                    self,
                    200,
                    html_body.encode(_UTF8),
                    "text/html; charset=utf-8",
                    extra_headers=WATCH_CLICKJACK_HEADERS,
                )
                return
            # GET /v1/spaces/{space_id}/credentials — metadata only
            parts = path.strip("/").split("/")
            space_id = _v1_space_tail(parts, _PART_CREDENTIALS)
            if space_id is not None:
                try:
                    result = pool.list_credentials(space_id)
                    _json_response(self, 200, result)
                except VaultValidationError as e:
                    _json_response(self, 400, {"error": _ERR_INVALID_CREDENTIALS, "detail": str(e)})
                except ValueError as e:
                    _json_response(self, 400, {"error": _ERR_BAD_REQUEST, "detail": str(e)})
                return
            if is_refused_credentials_path(parts):
                _json_response(self, 404, _refused_credentials_body())
                return

            # GET /v1/leases/{id}/watch  and  /v1/leases/{id}/watch/frame
            qs = parse_qs(urlparse(self.path).query)
            token = (qs.get(_QS_WATCH_AUTH) or [None])[0]
            mode = (qs.get("mode") or [None])[0]
            lease_id = _v1_lease_tail(parts, _PART_WATCH)
            if lease_id is not None:
                try:
                    ctype, body = pool.get_watch_page(lease_id, token, mode=mode)
                    _bytes_response(
                        self,
                        200,
                        body.encode(_UTF8),
                        ctype,
                        extra_headers=WATCH_CLICKJACK_HEADERS,
                    )
                except Exception as e:
                    if _watch_error(self, e):
                        return
                    raise
                return
            lease_id = _v1_lease_tail(parts, _PART_WATCH, "frame")
            if lease_id is not None:
                try:
                    jpeg = pool.get_watch_frame(lease_id, token)
                    _bytes_response(
                        self,
                        200,
                        jpeg,
                        "image/jpeg",
                        extra_headers=WATCH_CLICKJACK_HEADERS,
                    )
                except Exception as e:
                    if _watch_error(self, e):
                        return
                    raise
                return
            lease_id = _v1_lease_tail(parts, _PART_WATCH, "events")
            if lease_id is not None:
                try:
                    after_seq = int((qs.get("after_seq") or ["0"])[0] or 0)
                except ValueError:
                    after_seq = 0
                try:
                    result = pool.get_watch_events(
                        lease_id, token, after_seq=after_seq
                    )
                    _json_response(
                        self, 200, result, extra_headers=WATCH_CLICKJACK_HEADERS
                    )
                except Exception as e:
                    if _watch_error(self, e):
                        return
                    raise
                return
            lease_id = _v1_lease_tail(parts, _PART_WATCH, "timeline")
            if lease_id is not None:
                try:
                    result = pool.get_watch_timeline(lease_id, token)
                    _json_response(
                        self, 200, result, extra_headers=WATCH_CLICKJACK_HEADERS
                    )
                except Exception as e:
                    if _watch_error(self, e):
                        return
                    raise
                return

            lease_id = _v1_lease_tail(parts, _PART_WATCH, "evidence")
            if lease_id is not None:
                seq_raw = (qs.get("seq") or [None])[0]
                seq = None
                if seq_raw not in (None, ""):
                    try:
                        seq = int(seq_raw)
                    except ValueError:
                        _json_response(
                            self,
                            400,
                            {"error": "bad_seq"},
                            extra_headers=WATCH_CLICKJACK_HEADERS,
                        )
                        return
                try:
                    result = pool.get_watch_evidence(lease_id, token, seq=seq)
                    if seq is None:
                        _json_response(
                            self,
                            200,
                            result,
                            extra_headers=WATCH_CLICKJACK_HEADERS,
                        )
                    else:
                        _bytes_response(
                            self,
                            200,
                            result,
                            "image/jpeg",
                            extra_headers=WATCH_CLICKJACK_HEADERS,
                        )
                except Exception as e:
                    # Artifact missing → 404; auth/gone via watch helper.
                    from slipstream.downloads import ArtifactNotFoundError

                    if isinstance(e, ArtifactNotFoundError):
                        _json_response(
                            self,
                            404,
                            {"error": "evidence_not_found"},
                            extra_headers=WATCH_CLICKJACK_HEADERS,
                        )
                        return
                    if _watch_error(self, e):
                        return
                    raise
                return


            if _handle_lease_artifacts_get(self, pool, parts):
                return

            _json_response(self, 404, {"error": _ERR_NOT_FOUND, "path": path})

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path.rstrip("/") or "/"
            parts_early = path.strip("/").split("/")

            # Watch form POSTs (confirm/cede) + JSON input — shared path matchers.
            lease_confirm = _v1_lease_tail(parts_early, _PART_WATCH, "confirm")
            if lease_confirm is not None:
                _drain_request_body(self)
                qs = parse_qs(urlparse(self.path).query)
                token = (qs.get(_QS_WATCH_AUTH) or [None])[0]
                try:
                    result = pool.confirm_watch_takeover(lease_confirm, token)
                    if _watch_form_redirect(self, lease_confirm, token):
                        return
                    _json_response(
                        self, 200, result, extra_headers=WATCH_CLICKJACK_HEADERS
                    )
                except Exception as e:
                    if _watch_error(self, e):
                        return
                    raise
                return

            lease_cede = _v1_lease_tail(parts_early, _PART_WATCH, "cede")
            if lease_cede is not None:
                _drain_request_body(self)
                qs = parse_qs(urlparse(self.path).query)
                token = (qs.get(_QS_WATCH_AUTH) or [None])[0]
                try:
                    result = pool.cede_watch_control(lease_cede, token)
                    if _watch_form_redirect(self, lease_cede, token):
                        return
                    _json_response(
                        self, 200, result, extra_headers=WATCH_CLICKJACK_HEADERS
                    )
                except Exception as e:
                    if _watch_error(self, e):
                        return
                    raise
                return

            lease_mark = _v1_lease_tail(parts_early, _PART_WATCH, "mark-signed-in")
            if lease_mark is not None:
                qs = parse_qs(urlparse(self.path).query)
                token = (qs.get(_QS_WATCH_AUTH) or [None])[0]
                try:
                    mark_body = _read_watch_mark_body(self)
                except ValueError:
                    _json_response(self, 400, {"error": _ERR_INVALID_JSON})
                    return
                try:
                    result = pool.mark_watch_signed_in(lease_mark, token, mark_body)
                    if _watch_form_redirect(self, lease_mark, token):
                        return
                    _json_response(
                        self, 200, result, extra_headers=WATCH_CLICKJACK_HEADERS
                    )
                except SignedInValidationError as e:
                    _json_response(
                        self, 400, {"error": _ERR_INVALID_SIGNED_IN, "detail": str(e)}
                    )
                except Exception as e:
                    if _watch_error(self, e):
                        return
                    raise
                return

            lease_input = _v1_lease_tail(parts_early, _PART_WATCH, "input")
            if lease_input is not None:
                qs = parse_qs(urlparse(self.path).query)
                token = (qs.get(_QS_WATCH_AUTH) or [None])[0]
                try:
                    body = self._read_json()
                except json.JSONDecodeError:
                    _json_response(self, 400, {"error": _ERR_INVALID_JSON})
                    return
                try:
                    result = pool.dispatch_watch_input(lease_input, token, body)
                    _json_response(
                        self, 200, result, extra_headers=WATCH_CLICKJACK_HEADERS
                    )
                except Exception as e:
                    if _watch_error(self, e):
                        return
                    raise
                return

            try:
                body = self._read_json()
            except json.JSONDecodeError:
                _json_response(self, 400, {"error": _ERR_INVALID_JSON})
                return

            if path == "/v1/leases":
                agent_id = body.get(_QS_AGENT_ID)
                space_id = body.get("space_id")
                if not agent_id or not space_id:
                    _json_response(self, 400, {"error": "agent_id and space_id required"})
                    return
                try:
                    ttl = _parse_ttl_seconds(body.get("ttl_seconds"))
                except ValueError as e:
                    _json_response(
                        self,
                        400,
                        {"error": "invalid_ttl_seconds", "detail": str(e)},
                    )
                    return
                user_metadata = body.get(_FIELD_USER_METADATA)
                allowed_domains = body.get(_FIELD_ALLOWED_DOMAINS)
                try:
                    keep_alive = _parse_keep_alive(body.get(_FIELD_KEEP_ALIVE))
                except ValueError as e:
                    _json_response(
                        self,
                        400,
                        {"error": "invalid_keep_alive", "detail": str(e)},
                    )
                    return
                tier = body.get(_FIELD_TIER)
                mode = body.get(_FIELD_MODE)
                cdp_url = body.get(_FIELD_CDP_URL)
                cdp_port = body.get(_FIELD_CDP_PORT)
                try:
                    result = pool.lease(
                        agent_id,
                        space_id,
                        ttl_seconds=ttl,
                        user_metadata=user_metadata,
                        allowed_domains=allowed_domains,
                        keep_alive=keep_alive,
                        tier=tier,
                        mode=mode,
                        cdp_url=cdp_url,
                        cdp_port=cdp_port,
                    )
                    _json_response(self, 200, result)
                except MetadataValidationError as e:
                    _json_response(
                        self, 400, {"error": "invalid_user_metadata", "detail": str(e)}
                    )
                except DomainAllowlistError as e:
                    _json_response(
                        self,
                        400,
                        {"error": "invalid_allowed_domains", "detail": str(e)},
                    )
                except PoolFullError as e:
                    _json_response(self, 503, {"error": "pool_full", "detail": str(e)})
                except CloudProviderError as e:
                    _json_response(
                        self, 503, {"error": "cloud_provider_error", "detail": str(e)}
                    )
                except SpaceInUseError as e:
                    _json_response(self, 409, {"error": "space_in_use", "detail": str(e)})
                except AttachDisabledError as e:
                    _json_response(
                        self, 403, {"error": "attach_disabled", "detail": str(e)}
                    )
                except TierError as e:
                    _json_response(self, 400, {"error": "invalid_tier", "detail": str(e)})
                except ValueError as e:
                    _json_response(self, 400, {"error": _ERR_BAD_REQUEST, "detail": str(e)})
                except (RuntimeError, OSError) as e:
                    _json_response(self, 503, {"error": _ERR_LAUNCH_FAILED, "detail": str(e)})
                except Exception as e:
                    # Other launch / unexpected failures → same 503 (not bare 500)
                    _json_response(self, 503, {"error": _ERR_LAUNCH_FAILED, "detail": str(e)})
                return

            # /v1/leases/{id}/heartbeat  OR  /v1/leases/{id}/alerts
            parts = path.strip("/").split("/")
            lease_id = _v1_lease_tail(parts, "heartbeat")
            if lease_id is not None:
                try:
                    result = pool.heartbeat(lease_id)
                    _json_response(self, 200, result)
                except LeaseExpiredError:
                    _json_response(
                        self,
                        410,
                        {"error": "lease_expired", "lease_id": lease_id},
                    )
                except LeaseNotFoundError:
                    _json_response(self, 404, {"error": _ERR_LEASE_NOT_FOUND, "lease_id": lease_id})
                return

            lease_id = _v1_lease_tail(parts, "alerts")
            if lease_id is not None:
                try:
                    result = pool.raise_alert(lease_id, body)
                    _json_response(self, 200, result)
                except AlertValidationError as e:
                    _json_response(
                        self,
                        400,
                        {"error": "invalid_alert", "detail": str(e)},
                    )
                except AlertConflictError as e:
                    _json_response(
                        self,
                        409,
                        {"error": _ERR_ALERT_CONFLICT, "detail": str(e)},
                    )
                except LeaseNotFoundError:
                    _json_response(
                        self,
                        404,
                        {"error": _ERR_LEASE_NOT_FOUND, "lease_id": lease_id},
                    )
                return

            # POST /v1/leases/{lease_id}/captcha — solve-status chips (§5.11)
            lease_id = _v1_lease_tail(parts, "captcha")
            if lease_id is not None:
                try:
                    result = pool.report_captcha(lease_id, body)
                    _json_response(self, 200, result)
                except AlertValidationError as e:
                    _json_response(
                        self,
                        400,
                        {"error": "invalid_captcha", "detail": str(e)},
                    )
                except AlertConflictError as e:
                    _json_response(
                        self,
                        409,
                        {"error": _ERR_ALERT_CONFLICT, "detail": str(e)},
                    )
                except LeaseNotFoundError:
                    _json_response(
                        self,
                        404,
                        {"error": _ERR_LEASE_NOT_FOUND, "lease_id": lease_id},
                    )
                return


            # POST /v1/leases/{lease_id}/uploads — thin upload drop
            lease_id = _v1_lease_tail(parts, _PART_UPLOADS)
            if lease_id is not None:
                agent_id = body.get(_QS_AGENT_ID) if isinstance(body, dict) else None
                try:
                    result = pool.put_upload(lease_id, body, agent_id=agent_id)
                    _json_response(self, 201, result)
                except DownloadForbiddenError as e:
                    _json_response(self, 403, {"error": _ERR_FORBIDDEN, "detail": str(e)})
                except DownloadValidationError as e:
                    _json_response(self, 400, {"error": "invalid_upload", "detail": str(e)})
                except LeaseNotFoundError:
                    _json_response(
                        self, 404, {"error": _ERR_LEASE_NOT_FOUND, "lease_id": lease_id}
                    )
                return

            # POST /v1/leases/{lease_id}/act — Stagehand-style act → fallback
            lease_id = _v1_lease_tail(parts, "act")
            if lease_id is not None:
                try:
                    result = pool.run_act(lease_id, body)
                    status = result.get("status")
                    code = 403 if status == "confirmation_required" else 200
                    _json_response(self, code, result)
                except ActFallbackValidationError as e:
                    _json_response(
                        self, 400, {"error": "invalid_act", "detail": str(e)}
                    )
                except DomainAllowlistError as e:
                    _json_response(
                        self,
                        403,
                        {
                            "error": _S_DOMAIN_NOT_ALLOWED,
                            "detail": str(e),
                            "host": getattr(e, "host", None),
                        },
                    )
                except LadderGateError as e:
                    _ladder_gate_response(self, e)
                except CdpInjectError as e:
                    _json_response(self, 502, {"error": _S_CDP_INJECT_FAILED, "detail": str(e)})
                except LeaseNotFoundError:
                    _json_response(
                        self, 404, {"error": _ERR_LEASE_NOT_FOUND, "lease_id": lease_id}
                    )
                return

            # POST /v1/leases/{lease_id}/actions — permission ladder gate
            lease_id = _v1_lease_tail(parts, "actions")
            if lease_id is not None:
                try:
                    result = pool.request_action(lease_id, body)
                    _json_response(self, 202, result)
                except DomainAllowlistError as e:
                    _json_response(
                        self,
                        403,
                        {
                            "error": _S_DOMAIN_NOT_ALLOWED,
                            "detail": str(e),
                            "host": getattr(e, "host", None),
                        },
                    )
                except ActionValidationError as e:
                    _json_response(
                        self, 400, {"error": _ERR_INVALID_ACTION, "detail": str(e)}
                    )
                except AlertConflictError as e:
                    _json_response(
                        self, 409, {"error": _ERR_ALERT_CONFLICT, "detail": str(e)}
                    )
                except LeaseNotFoundError:
                    _json_response(
                        self, 404, {"error": _ERR_LEASE_NOT_FOUND, "lease_id": lease_id}
                    )
                return

            # POST /v1/leases/{lease_id}/navigate — top-frame nav + domain allowlist
            lease_id = _v1_lease_tail(parts, "navigate")
            if lease_id is not None:
                try:
                    result = pool.navigate(lease_id, body)
                    _json_response(self, 200, result)
                except DomainAllowlistError as e:
                    _json_response(
                        self,
                        403,
                        {
                            "error": _S_DOMAIN_NOT_ALLOWED,
                            "detail": str(e),
                            "host": getattr(e, "host", None),
                        },
                    )
                except LadderGateError as e:
                    _ladder_gate_response(self, e)
                except CdpInjectError as e:
                    _json_response(self, 502, {"error": _S_CDP_INJECT_FAILED, "detail": str(e)})
                except LeaseNotFoundError:
                    _json_response(
                        self, 404, {"error": _ERR_LEASE_NOT_FOUND, "lease_id": lease_id}
                    )
                return

            # POST /v1/leases/{lease_id}/eval — Runtime.evaluate (ladder-enforced)
            lease_id = _v1_lease_tail(parts, "eval")
            if lease_id is not None:
                try:
                    result = pool.evaluate(lease_id, body)
                    _json_response(self, 200, result)
                except LadderGateError as e:
                    _ladder_gate_response(self, e)
                except ActionValidationError as e:
                    _json_response(
                        self, 400, {"error": _ERR_INVALID_ACTION, "detail": str(e)}
                    )
                except CdpInjectError as e:
                    _json_response(self, 502, {"error": _S_CDP_INJECT_FAILED, "detail": str(e)})
                except LeaseNotFoundError:
                    _json_response(
                        self, 404, {"error": _ERR_LEASE_NOT_FOUND, "lease_id": lease_id}
                    )
                return

            # POST /v1/leases/{lease_id}/confirmations/{confirm_id}
            if (
                len(parts) == 5
                and parts[0] == _PART_V1
                and parts[1] == _PART_LEASES
                and parts[3] == "confirmations"
            ):
                lease_id = parts[2]
                confirm_id = parts[4]
                try:
                    result = pool.resolve_confirmation(lease_id, confirm_id, body)
                    _json_response(self, 200, result)
                except ActionValidationError as e:
                    _json_response(
                        self, 400, {"error": _ERR_INVALID_ACTION, "detail": str(e)}
                    )
                except ConfirmationGoneError as e:
                    _json_response(
                        self,
                        410,
                        {
                            "error": "confirmation_gone",
                            "confirm_id": confirm_id,
                            "detail": str(e),
                        },
                    )
                except ConfirmationNotFoundError:
                    _json_response(
                        self,
                        404,
                        {"error": "confirmation_not_found", "confirm_id": confirm_id},
                    )
                except LeaseNotFoundError:
                    _json_response(
                        self, 404, {"error": _ERR_LEASE_NOT_FOUND, "lease_id": lease_id}
                    )
                return

            # POST /v1/confirmations/{confirm_id} — CLI confirm|deny by id alone
            if (
                len(parts) == 3
                and parts[0] == _PART_V1
                and parts[1] == "confirmations"
            ):
                confirm_id = parts[2]
                try:
                    result = pool.resolve_confirmation_by_id(confirm_id, body)
                    _json_response(self, 200, result)
                except ActionValidationError as e:
                    _json_response(
                        self, 400, {"error": _ERR_INVALID_ACTION, "detail": str(e)}
                    )
                except ConfirmationGoneError as e:
                    _json_response(
                        self,
                        410,
                        {
                            "error": "confirmation_gone",
                            "confirm_id": confirm_id,
                            "detail": str(e),
                        },
                    )
                except ConfirmationNotFoundError:
                    _json_response(
                        self,
                        404,
                        {"error": "confirmation_not_found", "confirm_id": confirm_id},
                    )
                except LeaseNotFoundError as e:
                    _json_response(
                        self, 404, {"error": _ERR_LEASE_NOT_FOUND, "lease_id": str(e)}
                    )
                return

            # POST /v1/spaces/{space_id}/signed-in — mark/unmark badge (no cookies)
            space_id = _v1_space_tail(parts, "signed-in")
            if space_id is not None:
                try:
                    result = pool.set_space_signed_in(space_id, body)
                    _json_response(self, 200, result)
                except SignedInValidationError as e:
                    _json_response(
                        self, 400, {"error": _ERR_INVALID_SIGNED_IN, "detail": str(e)}
                    )
                except ValueError as e:
                    _json_response(self, 400, {"error": _ERR_BAD_REQUEST, "detail": str(e)})
                return

            # POST /v1/spaces/{space_id}/login-once — lease + need_human(reason=login)
            space_id = _v1_space_tail(parts, "login-once")
            if space_id is not None:
                try:
                    agent_id = body.get(_QS_AGENT_ID) if isinstance(body, dict) else None
                    if not agent_id:
                        _json_response(
                            self,
                            400,
                            {
                                "error": _ERR_BAD_REQUEST,
                                "detail": "agent_id required",
                            },
                        )
                        return
                    result = pool.login_once(
                        space_id,
                        agent_id=str(agent_id),
                        detail=body.get("detail"),
                        ttl_s=body.get("ttl_s"),
                        host=body.get("host"),
                        ttl_seconds=body.get("ttl_seconds"),
                    )
                    _json_response(self, 200, result)
                except SignedInValidationError as e:
                    _json_response(
                        self, 400, {"error": _ERR_INVALID_SIGNED_IN, "detail": str(e)}
                    )
                except PoolFullError as e:
                    _json_response(self, 503, {"error": "pool_full", "detail": str(e)})
                except CloudProviderError as e:
                    _json_response(
                        self, 503, {"error": "cloud_provider_error", "detail": str(e)}
                    )
                except SpaceInUseError as e:
                    _json_response(self, 409, {"error": "space_in_use", "detail": str(e)})
                except (RuntimeError, OSError) as e:
                    _json_response(self, 503, {"error": _ERR_LAUNCH_FAILED, "detail": str(e)})
                except ValueError as e:
                    _json_response(self, 400, {"error": _ERR_BAD_REQUEST, "detail": str(e)})
                return

            # POST /v1/spaces/{space_id}/credentials/bind
            space_id = _v1_space_tail(parts, _PART_CREDENTIALS, "bind")
            if space_id is not None:
                try:
                    result = pool.bind_credential(space_id, body)
                    _json_response(self, 200, result)
                except VaultValidationError as e:
                    _json_response(self, 400, {"error": _ERR_INVALID_CREDENTIALS, "detail": str(e)})
                except VaultUnavailableError as e:
                    _json_response(self, 503, {"error": "vault_unavailable", "detail": str(e)})
                except ValueError as e:
                    _json_response(self, 400, {"error": _ERR_BAD_REQUEST, "detail": str(e)})
                return

            # POST /v1/spaces/{space_id}/credentials/{cred_id}/unbind
            if (
                len(parts) == 6
                and parts[0] == _PART_V1
                and parts[1] == _PART_SPACES
                and parts[3] == _PART_CREDENTIALS
                and parts[5] == "unbind"
            ):
                space_id = parts[2]
                cred_id = parts[4]
                try:
                    result = pool.unbind_credential(space_id, cred_id)
                    _json_response(self, 200, result)
                except CredNotFoundError:
                    _json_response(
                        self,
                        404,
                        {"error": "cred_not_found", "cred_id": cred_id, "space_id": space_id},
                    )
                except VaultValidationError as e:
                    _json_response(self, 400, {"error": _ERR_INVALID_CREDENTIALS, "detail": str(e)})
                except ValueError as e:
                    _json_response(self, 400, {"error": _ERR_BAD_REQUEST, "detail": str(e)})
                return

            # POST /v1/leases/{lease_id}/credentials/fill
            lease_id = _v1_lease_tail(parts, _PART_CREDENTIALS, "fill")
            if lease_id is not None:
                try:
                    result = pool.fill_credentials(lease_id, body)
                    _json_response(self, 200, result)
                except LadderGateError as e:
                    _ladder_gate_response(self, e)
                except LeaseNotFoundError:
                    _json_response(
                        self, 404, {"error": _ERR_LEASE_NOT_FOUND, "lease_id": lease_id}
                    )
                except CredNotFoundError as e:
                    _json_response(
                        self, 404, {"error": "cred_not_found", "detail": str(e)}
                    )
                except VaultValidationError as e:
                    _json_response(self, 400, {"error": _ERR_INVALID_CREDENTIALS, "detail": str(e)})
                except CdpInjectError as e:
                    _json_response(self, 502, {"error": _S_CDP_INJECT_FAILED, "detail": str(e)})
                except VaultUnavailableError as e:
                    _json_response(self, 503, {"error": "vault_unavailable", "detail": str(e)})
                return

            # Refuse free-read secret / cookie dump paths (shared matcher — ADV-003)
            if is_refused_credentials_path(parts):
                _json_response(self, 404, _refused_credentials_body())
                return

            _json_response(self, 404, {"error": _ERR_NOT_FOUND, "path": path})


        def do_PUT(self) -> None:  # noqa: N802
            path = urlparse(self.path).path.rstrip("/") or "/"
            parts = path.strip("/").split("/")
            # PUT /v1/spaces/{space_id}  { user_metadata? , allowed_domains? }
            if len(parts) == 3 and parts[0] == _PART_V1 and parts[1] == _PART_SPACES:
                space_id = parts[2]
                try:
                    body = self._read_json()
                except json.JSONDecodeError:
                    _json_response(self, 400, {"error": _ERR_INVALID_JSON})
                    return
                if not isinstance(body, dict):
                    _json_response(self, 400, {"error": _ERR_INVALID_JSON})
                    return
                has_meta = _FIELD_USER_METADATA in body
                has_domains = _FIELD_ALLOWED_DOMAINS in body
                has_tier = _FIELD_TIER in body
                if not has_meta and not has_domains and not has_tier:
                    _json_response(
                        self,
                        400,
                        {
                            "error": "space_update_required",
                            "detail": "body must include user_metadata, allowed_domains, and/or tier",
                        },
                    )
                    return
                try:
                    result = pool.set_space_metadata(
                        space_id,
                        body.get(_FIELD_USER_METADATA) if has_meta else None,
                        allowed_domains=body.get(_FIELD_ALLOWED_DOMAINS) if has_domains else None,
                        clear_allowed_domains=(
                            has_domains and body.get(_FIELD_ALLOWED_DOMAINS) is None
                        ),
                        tier=body.get(_FIELD_TIER) if has_tier else None,
                    )
                    _json_response(self, 200, result)
                except MetadataValidationError as e:
                    _json_response(
                        self, 400, {"error": "invalid_user_metadata", "detail": str(e)}
                    )
                except DomainAllowlistError as e:
                    _json_response(
                        self,
                        400,
                        {"error": "invalid_allowed_domains", "detail": str(e)},
                    )
                except TierError as e:
                    _json_response(self, 400, {"error": "invalid_tier", "detail": str(e)})
                except ValueError as e:
                    _json_response(self, 400, {"error": _ERR_BAD_REQUEST, "detail": str(e)})
                return
            _json_response(self, 404, {"error": _ERR_NOT_FOUND, "path": path})

        def do_DELETE(self) -> None:  # noqa: N802
            path = urlparse(self.path).path.rstrip("/") or "/"
            parts = path.strip("/").split("/")
            if len(parts) == 3 and parts[0] == _PART_V1 and parts[1] == _PART_LEASES:
                lease_id = parts[2]
                try:
                    body = self._read_json()
                except json.JSONDecodeError:
                    body = {}
                reason = body.get("reason", "client_release") if isinstance(body, dict) else "client_release"
                try:
                    result = pool.release(lease_id, reason=reason)
                    _json_response(self, 200, result)
                except LeaseNotFoundError:
                    _json_response(self, 404, {"error": _ERR_LEASE_NOT_FOUND, "lease_id": lease_id})
                return
            _json_response(self, 404, {"error": _ERR_NOT_FOUND, "path": path})

    return PoolHandler



def _handle_lease_artifacts_get(handler, pool, parts) -> bool:
    """GET downloads|uploads list/get. Return True if path handled."""
    if not (
        len(parts) in (4, 5)
        and parts[0] == _PART_V1
        and parts[1] == _PART_LEASES
        and parts[3] in (_PART_DOWNLOADS, _PART_UPLOADS)
    ):
        return False
    lease_id = parts[2]
    kind = parts[3]
    qs = parse_qs(urlparse(handler.path).query)
    agent_id = (qs.get(_QS_AGENT_ID) or [None])[0]
    rel = (qs.get("rel_path") or ["0"])[0] in ("1", "true", "yes")
    try:
        if len(parts) == 4:
            result = pool.list_lease_artifacts(
                lease_id, kind, agent_id=agent_id, include_rel_path=rel
            )
            _json_response(handler, 200, result)
            return True
        meta, raw = pool.get_lease_artifact(
            lease_id, parts[4], kind, agent_id=agent_id, include_rel_path=rel
        )
    except ArtifactNotFoundError:
        aid = parts[4] if len(parts) == 5 else None
        _json_response(handler, 404, {"error": "artifact_not_found", "artifact_id": aid})
        return True
    except DownloadForbiddenError as e:
        _json_response(handler, 403, {"error": _ERR_FORBIDDEN, "detail": str(e)})
        return True
    except DownloadValidationError as e:
        err = "invalid_download" if kind == KIND_DOWNLOADS else "invalid_upload"
        _json_response(handler, 400, {"error": err, "detail": str(e)})
        return True
    except LeaseNotFoundError:
        _json_response(handler, 404, {"error": _ERR_LEASE_NOT_FOUND, "lease_id": lease_id})
        return True
    accept = (handler.headers.get("Accept") or "").lower()
    key = "download" if kind == KIND_DOWNLOADS else "upload"
    if "application/json" in accept and "octet" not in accept:
        _json_response(handler, 200, {"lease_id": lease_id, key: meta})
    else:
        _bytes_response(
            handler,
            200,
            raw,
            "application/octet-stream",
            extra_headers={
                "Content-Disposition": content_disposition_attachment(meta["filename"]),
                "X-Slipstream-Sha256": meta["sha256"],
            },
        )
    return True


class PoolServer:
    """Threading HTTP server wrapping BrowserPool."""

    def __init__(self, pool: BrowserPool, host: str | None = None, port: int | None = None):
        self.pool = pool
        self.host = host or pool.config.host
        self.port = port if port is not None else pool.config.port
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._evict_stop = threading.Event()

    def start(self, background: bool = True) -> None:
        self.pool.set_api_base_url(self.base_url)
        handler = make_handler(self.pool)
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self._httpd.daemon_threads = True

        def _evict_loop() -> None:
            while not self._evict_stop.wait(30):
                try:
                    self.pool.evict_idle()
                except Exception:
                    pass

        self._evict_thread = threading.Thread(target=_evict_loop, daemon=True)
        self._evict_thread.start()

        if background:
            self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
            self._thread.start()
        else:
            self._httpd.serve_forever()

    def stop(self) -> None:
        self._evict_stop.set()
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
        self.pool.shutdown()

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"
