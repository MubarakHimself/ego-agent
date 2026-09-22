"""HTTP JSON API for the Browser Pool Manager (stdlib http.server).

Base URL default: http://127.0.0.1:8755

Endpoints:
  POST   /v1/leases
  POST   /v1/leases/{id}/heartbeat
  POST   /v1/leases/{id}/alerts
  POST   /v1/leases/{id}/credentials/fill
  POST   /v1/leases/{id}/watch/confirm
  POST   /v1/spaces/{space_id}/credentials/bind
  POST   /v1/spaces/{space_id}/credentials/{cred_id}/unbind
  GET    /v1/spaces/{space_id}/credentials
  GET    /v1/leases/{id}/watch
  GET    /v1/leases/{id}/watch/frame
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
from slipstream.watch import WatchAuthError, WatchGoneError, WatchNotFoundError
from slipstream.cdp_inject import CdpInjectError
from slipstream.pool import (
    BrowserPool,
    LeaseExpiredError,
    LeaseNotFoundError,
    PoolFullError,
    SpaceInUseError,
)
from slipstream.vault import CredNotFoundError, VaultUnavailableError, VaultValidationError

# ADV-003: shared refuse matcher for free-read secret / cookie dump paths (GET+POST)
_REFUSED_CRED_TAILS = frozenset(
    {"secret", "secrets", "cookies", "storage_state", "dump"}
)


def is_refused_credentials_path(parts: list[str]) -> bool:
    """True when path looks like …/credentials/{secret|cookies|…} free-read."""
    return (
        len(parts) >= 4
        and "credentials" in parts
        and parts[-1] in _REFUSED_CRED_TAILS
    )


def _refused_credentials_body() -> dict:
    return {
        "error": "refused",
        "detail": "free-read of secrets/cookies is not available; use fill",
    }


def _json_response(handler: BaseHTTPRequestHandler, status: int, body: dict[str, Any]) -> None:
    raw = json.dumps(body).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(raw)))
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
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
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


def make_handler(pool: BrowserPool):
    class PoolHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:  # quieter default
            pass

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            if not raw:
                return {}
            return json.loads(raw.decode("utf-8"))

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path == "/healthz":
                _json_response(self, 200, {"ok": True})
                return
            if path == "/v1/pool/status":
                _json_response(self, 200, pool.status())
                return
            # GET /v1/spaces/{space_id}/credentials — metadata only
            parts = path.strip("/").split("/")
            if (
                len(parts) == 4
                and parts[0] == "v1"
                and parts[1] == "spaces"
                and parts[3] == "credentials"
            ):
                space_id = parts[2]
                try:
                    result = pool.list_credentials(space_id)
                    _json_response(self, 200, result)
                except VaultValidationError as e:
                    _json_response(self, 400, {"error": "invalid_credentials", "detail": str(e)})
                except ValueError as e:
                    _json_response(self, 400, {"error": "bad_request", "detail": str(e)})
                return
            if is_refused_credentials_path(parts):
                _json_response(self, 404, _refused_credentials_body())
                return

            # GET /v1/leases/{id}/watch  and  /v1/leases/{id}/watch/frame
            qs = parse_qs(urlparse(self.path).query)
            token = (qs.get("token") or [None])[0]
            mode = (qs.get("mode") or [None])[0]
            if (
                len(parts) == 4
                and parts[0] == "v1"
                and parts[1] == "leases"
                and parts[3] == "watch"
            ):
                lease_id = parts[2]
                try:
                    ctype, body = pool.get_watch_page(lease_id, token, mode=mode)
                    _bytes_response(self, 200, body.encode("utf-8"), ctype)
                except Exception as e:
                    if _watch_error(self, e):
                        return
                    raise
                return
            if (
                len(parts) == 5
                and parts[0] == "v1"
                and parts[1] == "leases"
                and parts[3] == "watch"
                and parts[4] == "frame"
            ):
                lease_id = parts[2]
                try:
                    jpeg = pool.get_watch_frame(lease_id, token)
                    _bytes_response(self, 200, jpeg, "image/jpeg")
                except Exception as e:
                    if _watch_error(self, e):
                        return
                    raise
                return

            _json_response(self, 404, {"error": "not_found", "path": path})

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path.rstrip("/") or "/"
            parts_early = path.strip("/").split("/")

            # Take-over confirm may be an HTML form POST (not JSON).
            if (
                len(parts_early) == 5
                and parts_early[0] == "v1"
                and parts_early[1] == "leases"
                and parts_early[3] == "watch"
                and parts_early[4] == "confirm"
            ):
                # Drain body without requiring JSON.
                length = int(self.headers.get("Content-Length", "0") or 0)
                if length > 0:
                    self.rfile.read(length)
                lease_id = parts_early[2]
                qs = parse_qs(urlparse(self.path).query)
                token = (qs.get("token") or [None])[0]
                try:
                    result = pool.confirm_watch_takeover(lease_id, token)
                    content_type = (self.headers.get("Content-Type") or "").lower()
                    # HTML form POST → redirect; JSON clients get JSON.
                    if "application/x-www-form-urlencoded" in content_type:
                        from urllib.parse import urlencode

                        loc = (
                            f"/v1/leases/{lease_id}/watch?"
                            + urlencode({"token": token or "", "mode": "takeover"})
                        )
                        self.send_response(303)
                        self.send_header("Location", loc)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    _json_response(self, 200, result)
                except Exception as e:
                    if _watch_error(self, e):
                        return
                    raise
                return

            try:
                body = self._read_json()
            except json.JSONDecodeError:
                _json_response(self, 400, {"error": "invalid_json"})
                return

            if path == "/v1/leases":
                agent_id = body.get("agent_id")
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
                try:
                    result = pool.lease(agent_id, space_id, ttl_seconds=ttl)
                    _json_response(self, 200, result)
                except PoolFullError as e:
                    _json_response(self, 503, {"error": "pool_full", "detail": str(e)})
                except SpaceInUseError as e:
                    _json_response(self, 409, {"error": "space_in_use", "detail": str(e)})
                except ValueError as e:
                    _json_response(self, 400, {"error": "bad_request", "detail": str(e)})
                except (RuntimeError, OSError) as e:
                    _json_response(self, 503, {"error": "launch_failed", "detail": str(e)})
                except Exception as e:
                    # Other launch / unexpected failures → same 503 (not bare 500)
                    _json_response(self, 503, {"error": "launch_failed", "detail": str(e)})
                return

            # /v1/leases/{id}/heartbeat  OR  /v1/leases/{id}/alerts
            parts = path.strip("/").split("/")
            if (
                len(parts) == 4
                and parts[0] == "v1"
                and parts[1] == "leases"
                and parts[3] == "heartbeat"
            ):
                lease_id = parts[2]
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
                    _json_response(self, 404, {"error": "lease_not_found", "lease_id": lease_id})
                return

            if (
                len(parts) == 4
                and parts[0] == "v1"
                and parts[1] == "leases"
                and parts[3] == "alerts"
            ):
                lease_id = parts[2]
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
                        {"error": "alert_conflict", "detail": str(e)},
                    )
                except LeaseNotFoundError:
                    _json_response(
                        self,
                        404,
                        {"error": "lease_not_found", "lease_id": lease_id},
                    )
                return

            # POST /v1/spaces/{space_id}/credentials/bind
            if (
                len(parts) == 5
                and parts[0] == "v1"
                and parts[1] == "spaces"
                and parts[3] == "credentials"
                and parts[4] == "bind"
            ):
                space_id = parts[2]
                try:
                    result = pool.bind_credential(space_id, body)
                    _json_response(self, 200, result)
                except VaultValidationError as e:
                    _json_response(self, 400, {"error": "invalid_credentials", "detail": str(e)})
                except VaultUnavailableError as e:
                    _json_response(self, 503, {"error": "vault_unavailable", "detail": str(e)})
                except ValueError as e:
                    _json_response(self, 400, {"error": "bad_request", "detail": str(e)})
                return

            # POST /v1/spaces/{space_id}/credentials/{cred_id}/unbind
            if (
                len(parts) == 6
                and parts[0] == "v1"
                and parts[1] == "spaces"
                and parts[3] == "credentials"
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
                    _json_response(self, 400, {"error": "invalid_credentials", "detail": str(e)})
                except ValueError as e:
                    _json_response(self, 400, {"error": "bad_request", "detail": str(e)})
                return

            # POST /v1/leases/{lease_id}/credentials/fill
            if (
                len(parts) == 5
                and parts[0] == "v1"
                and parts[1] == "leases"
                and parts[3] == "credentials"
                and parts[4] == "fill"
            ):
                lease_id = parts[2]
                try:
                    result = pool.fill_credentials(lease_id, body)
                    _json_response(self, 200, result)
                except LeaseNotFoundError:
                    _json_response(
                        self, 404, {"error": "lease_not_found", "lease_id": lease_id}
                    )
                except CredNotFoundError as e:
                    _json_response(
                        self, 404, {"error": "cred_not_found", "detail": str(e)}
                    )
                except VaultValidationError as e:
                    _json_response(self, 400, {"error": "invalid_credentials", "detail": str(e)})
                except CdpInjectError as e:
                    _json_response(self, 502, {"error": "cdp_inject_failed", "detail": str(e)})
                except VaultUnavailableError as e:
                    _json_response(self, 503, {"error": "vault_unavailable", "detail": str(e)})
                return

            # Refuse free-read secret / cookie dump paths (shared matcher — ADV-003)
            if is_refused_credentials_path(parts):
                _json_response(self, 404, _refused_credentials_body())
                return

            _json_response(self, 404, {"error": "not_found", "path": path})

        def do_DELETE(self) -> None:  # noqa: N802
            path = urlparse(self.path).path.rstrip("/") or "/"
            parts = path.strip("/").split("/")
            if len(parts) == 3 and parts[0] == "v1" and parts[1] == "leases":
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
                    _json_response(self, 404, {"error": "lease_not_found", "lease_id": lease_id})
                return
            _json_response(self, 404, {"error": "not_found", "path": path})

    return PoolHandler


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
