"""HTTP JSON API for the Browser Pool Manager (stdlib http.server).

Base URL default: http://127.0.0.1:8755

Endpoints:
  POST   /v1/leases
  POST   /v1/leases/{id}/heartbeat
  DELETE /v1/leases/{id}
  GET    /v1/pool/status
  GET    /healthz
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from ego_pool.pool import BrowserPool, LeaseNotFoundError, PoolFullError


def _json_response(handler: BaseHTTPRequestHandler, status: int, body: dict[str, Any]) -> None:
    raw = json.dumps(body).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def make_handler(pool: BrowserPool):
    class PoolHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:  # quieter default
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
            _json_response(self, 404, {"error": "not_found", "path": path})

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path.rstrip("/") or "/"
            try:
                body = self._read_json()
            except json.JSONDecodeError:
                _json_response(self, 400, {"error": "invalid_json"})
                return

            if path == "/v1/leases":
                agent_id = body.get("agent_id")
                space_id = body.get("space_id")
                mode = body.get("mode", "isolated")
                ttl = body.get("ttl_seconds")
                if not agent_id or not space_id:
                    _json_response(self, 400, {"error": "agent_id and space_id required"})
                    return
                try:
                    result = pool.lease(agent_id, space_id, mode=mode, ttl_seconds=ttl)
                    _json_response(self, 200, result)
                except PoolFullError as e:
                    _json_response(self, 503, {"error": "pool_full", "detail": str(e)})
                except ValueError as e:
                    _json_response(self, 400, {"error": "bad_request", "detail": str(e)})
                return

            # /v1/leases/{id}/heartbeat
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
                except LeaseNotFoundError:
                    _json_response(self, 404, {"error": "lease_not_found", "lease_id": lease_id})
                return

            # Optional POST release alias
            if (
                len(parts) == 4
                and parts[0] == "v1"
                and parts[1] == "leases"
                and parts[3] == "release"
            ):
                lease_id = parts[2]
                reason = body.get("reason", "client_release")
                try:
                    result = pool.release(lease_id, reason=reason)
                    _json_response(self, 200, result)
                except LeaseNotFoundError:
                    _json_response(self, 404, {"error": "lease_not_found", "lease_id": lease_id})
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
