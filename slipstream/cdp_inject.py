"""Pool-side CDP credential inject — plaintext never returns to the agent.

Agents call ``fill`` with ``cred_id`` + CSS selectors only. The pool unlocks
the vault in-process, focuses each selector, and inserts text via CDP
``Input.insertText`` (or a mock recorder under ``SLIPSTREAM_MOCK``).

Login / 2FA / CAPTCHA that the agent cannot complete must raise
``need_human`` (reason ``login`` or ``other``) — see alerts + SKILL.md.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlparse
from urllib.request import urlopen


class CdpInjectError(RuntimeError):
    """CDP inject failed (target missing, selector miss, protocol error)."""


class CdpInjector(Protocol):
    def fill_fields(
        self,
        cdp_http_url: str,
        fields: list[tuple[str, str, str]],
    ) -> list[str]:
        """Fill ``(field_name, selector, value)`` tuples; return filled names."""

    def page_url(self, cdp_http_url: str) -> str:
        """Return current page location.href (for origin check before inject)."""


@dataclass
class MockCdpInjector:
    """Records fill attempts — used under mock / unit tests."""

    calls: list[dict[str, Any]] = field(default_factory=list)
    current_url: str = "https://example.com/"
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def page_url(self, cdp_http_url: str) -> str:
        return self.current_url

    def fill_fields(
        self,
        cdp_http_url: str,
        fields: list[tuple[str, str, str]],
    ) -> list[str]:
        filled: list[str] = []
        with self._lock:
            for name, selector, value in fields:
                self.calls.append(
                    {
                        "cdp_http_url": cdp_http_url,
                        "field": name,
                        "selector": selector,
                        # Record length only — never retain plaintext in mock log
                        "value_len": len(value),
                    }
                )
                filled.append(name)
        return filled


def _assert_ws_debugger_url(ws_url: str) -> str:
    """ADV-WATCH-006: refuse non-loopback / non-ws(s) debugger URLs before connect."""
    parsed = urlparse(ws_url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("ws", "wss"):
        raise CdpInjectError(
            f"refusing non-ws(s) webSocketDebuggerUrl scheme {scheme!r}"
        )
    host = (parsed.hostname or "").strip().lower().strip("[]")
    if host not in ("127.0.0.1", "localhost", "::1"):
        # Also accept other loopback IPs
        try:
            import ipaddress

            if not ipaddress.ip_address(host).is_loopback:
                raise CdpInjectError(
                    f"refusing non-loopback webSocketDebuggerUrl host {host!r}"
                )
        except ValueError as e:
            raise CdpInjectError(
                f"refusing non-loopback webSocketDebuggerUrl host {host!r}"
            ) from e
    return ws_url


def _page_ws_url(cdp_http_url: str) -> str:
    """Resolve a page WebSocket debugger URL from CDP HTTP endpoint."""
    base = cdp_http_url.rstrip("/")
    with urlopen(f"{base}/json/list", timeout=3.0) as resp:
        targets = json.load(resp)
    if not isinstance(targets, list):
        raise CdpInjectError("CDP /json/list returned non-list")
    for t in targets:
        if not isinstance(t, dict):
            continue
        if t.get("type") in (None, "page") and t.get("webSocketDebuggerUrl"):
            return _assert_ws_debugger_url(str(t["webSocketDebuggerUrl"]))
    # Fallback: version endpoint browser-level WS (less ideal for DOM)
    with urlopen(f"{base}/json/version", timeout=3.0) as resp:
        ver = json.load(resp)
    ws = ver.get("webSocketDebuggerUrl") if isinstance(ver, dict) else None
    if not ws:
        raise CdpInjectError("no CDP WebSocket target available")
    return _assert_ws_debugger_url(str(ws))


def _ws_cdp_call(ws_url: str, method: str, params: dict[str, Any] | None = None) -> Any:
    """Minimal one-shot CDP call over WebSocket (stdlib handshake).

    Prefer ``websocket-client`` if installed; else raise with guidance.
    """
    try:
        import websocket  # type: ignore
    except ImportError as e:
        raise CdpInjectError(
            "real CDP inject requires websocket-client (pip install websocket-client) "
            "or use SLIPSTREAM_MOCK=1 / MockCdpInjector"
        ) from e

    payload = {"id": 1, "method": method, "params": params or {}}
    ws = websocket.create_connection(ws_url, timeout=5)
    try:
        ws.send(json.dumps(payload))
        # Read until matching id (skip events)
        for _ in range(50):
            raw = ws.recv()
            msg = json.loads(raw)
            if msg.get("id") == 1:
                if "error" in msg:
                    raise CdpInjectError(f"CDP {method} error: {msg['error']}")
                return msg.get("result")
        raise CdpInjectError(f"CDP {method} timed out waiting for result")
    finally:
        try:
            ws.close()
        except Exception:
            pass


class RealCdpInjector:
    """Focus selector via Runtime.evaluate, then Input.insertText."""

    def page_url(self, cdp_http_url: str) -> str:
        host = urlparse(cdp_http_url).hostname
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise CdpInjectError(f"refusing non-loopback CDP URL host {host!r}")
        ws_url = _page_ws_url(cdp_http_url)
        result = _ws_cdp_call(
            ws_url,
            "Runtime.evaluate",
            {
                "expression": "location.href",
                "returnByValue": True,
            },
        )
        if isinstance(result, dict) and "result" in result:
            val = result["result"].get("value")
            if isinstance(val, str) and val:
                return val
        raise CdpInjectError("could not read location.href from page")

    def fill_fields(
        self,
        cdp_http_url: str,
        fields: list[tuple[str, str, str]],
    ) -> list[str]:
        # Validate loopback-ish CDP URL (pool-owned)
        host = urlparse(cdp_http_url).hostname
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise CdpInjectError(f"refusing non-loopback CDP URL host {host!r}")

        ws_url = _page_ws_url(cdp_http_url)
        filled: list[str] = []
        for name, selector, value in fields:
            # Focus + clear via DOM
            sel_json = json.dumps(selector)
            focus_expr = (
                f"(function(){{ var el=document.querySelector({sel_json}); "
                f"if(!el) throw new Error('selector_miss'); "
                f"el.focus(); if('value' in el) el.value=''; return true; }})()"
            )
            _ws_cdp_call(
                ws_url,
                "Runtime.evaluate",
                {
                    "expression": focus_expr,
                    "awaitPromise": False,
                    "returnByValue": True,
                },
            )
            _ws_cdp_call(ws_url, "Input.insertText", {"text": value})
            filled.append(name)
        return filled


def default_injector(*, mock: bool) -> CdpInjector:
    if mock:
        return MockCdpInjector()
    return RealCdpInjector()
