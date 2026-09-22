"""Thin observe-only live Watch over leased CDP (JPEG / HTML).

Minted on need_human as a short-TTL tokenized watch_url. Revoked on
task_done and after TTL expiry. No pair-browse / input / secrets on page.
"""

from __future__ import annotations

import base64
import html
import secrets
import time
from dataclasses import dataclass
from urllib.parse import quote, urlencode, urlparse

# Minimal JPEG SOI+EOI for mock / fallback (not a secret; low entropy).
_MOCK_JPEG = bytes((0xFF, 0xD8, 0xFF, 0xD9))



class WatchAuthError(Exception):
    """Missing / wrong watch token (maps to HTTP 401)."""

    def __init__(self, detail: str = "unauthorized"):
        super().__init__(detail)
        self.detail = detail


class WatchGoneError(Exception):
    """Revoked or TTL-expired watch session (maps to HTTP 410)."""

    def __init__(self, detail: str = "gone"):
        super().__init__(detail)
        self.detail = detail


class WatchNotFoundError(Exception):
    """No watch session for lease (maps to HTTP 404)."""

    def __init__(self, lease_id: str):
        super().__init__(lease_id)
        self.lease_id = lease_id


class WatchCaptureError(RuntimeError):
    """CDP screenshot failed."""


@dataclass
class WatchSession:
    """In-memory short-TTL watch credential for one lease."""

    lease_id: str
    token: str
    expires_at: float
    reason: str | None = None
    detail: str = ""
    revoked: bool = False
    takeover_confirmed: bool = False
    created_at: float = 0.0

    def alive(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return (not self.revoked) and now < self.expires_at


def mint_watch_token() -> str:
    """Unguessable URL credential (treat watch_url as a secret)."""
    return secrets.token_urlsafe(32)


def build_watch_url(
    lease_id: str,
    token: str,
    *,
    base_url: str = "http://127.0.0.1:8755",
    mode: str | None = None,
) -> str:
    """Local tokenized Watch URL (observe-only; optional takeover mode)."""
    q: dict[str, str] = {"token": token}
    if mode:
        q["mode"] = mode
    return (
        f"{base_url.rstrip('/')}/v1/leases/{quote(lease_id, safe='')}/watch"
        f"?{urlencode(q)}"
    )


def build_takeover_url(
    lease_id: str,
    token: str,
    *,
    base_url: str = "http://127.0.0.1:8755",
) -> str:
    return build_watch_url(lease_id, token, base_url=base_url, mode="takeover")


def _host_is_loopback(host: str | None) -> bool:
    if not host:
        return False
    h = host.strip().lower().strip("[]")
    if h in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        import ipaddress

        return bool(ipaddress.ip_address(h).is_loopback)
    except ValueError:
        return False


def assert_cdp_loopback(cdp_http_url: str) -> None:
    """Refuse non-loopback CDP endpoints (SSRF / allowlist).

    Uses ``_validate_api_request_url`` so Skylos SSRF (SKY-D216) sees a sanitizer.
    """
    from slipstream.cli import _validate_api_request_url

    try:
        _validate_api_request_url(cdp_http_url)
    except Exception as e:
        raise WatchCaptureError(f"CDP URL refused by allowlist: {e}") from e
    host = urlparse(cdp_http_url).hostname
    if not _host_is_loopback(host):
        raise WatchCaptureError(f"refusing non-loopback CDP URL host {host!r}")


def capture_jpeg_frame(cdp_http_url: str, *, mock: bool = False) -> bytes:
    """Capture a JPEG viewport via CDP Page.captureScreenshot (or mock JPEG).

    Never returns cookies, passwords, tokens, vault material, or CDP auth —
    only image bytes.
    """
    if mock or not cdp_http_url:
        return _MOCK_JPEG

    assert_cdp_loopback(cdp_http_url)

    # Reuse inject helpers for one-shot CDP over page WS.
    from slipstream.cdp_inject import CdpInjectError, _page_ws_url, _ws_cdp_call

    try:
        ws_url = _page_ws_url(cdp_http_url)
        # Page domain must be enabled for some Chrome builds; captureScreenshot
        # usually works without — enable defensively, ignore enable errors.
        try:
            _ws_cdp_call(ws_url, "Page.enable", {})
        except Exception:
            pass
        result = _ws_cdp_call(
            ws_url,
            "Page.captureScreenshot",
            {"format": "jpeg", "quality": 60},
        )
    except CdpInjectError as e:
        raise WatchCaptureError(str(e)) from e
    except Exception as e:  # noqa: BLE001 — websocket / CDP transport
        raise WatchCaptureError(f"CDP screenshot transport failed: {e}") from e

    if not isinstance(result, dict):
        raise WatchCaptureError("CDP screenshot returned non-object")
    data = result.get("data")
    if not isinstance(data, str) or not data:
        raise WatchCaptureError("CDP screenshot missing data")
    try:
        raw = base64.b64decode(data, validate=False)
    except Exception as e:  # noqa: BLE001
        raise WatchCaptureError("CDP screenshot base64 decode failed") from e
    if len(raw) < 4 or raw[:2] != b"\xff\xd8":
        raise WatchCaptureError("CDP screenshot was not a JPEG")
    return raw


def render_watch_html(
    *,
    lease_id: str,
    reason: str | None,
    detail: str,
    frame_url: str,
    confirm_url: str | None,
    mode: str | None,
    expires_in_s: int,
    takeover_confirmed: bool,
) -> str:
    """Observe-only HTML shell. Never embeds secrets / CDP / cookies / tokens.

    All dynamic pieces are HTML-escaped before concatenation (no raw f-string
    interpolation of caller strings into markup).
    """
    from slipstream.alerts import scrub_text

    short = html.escape((lease_id or "?")[:8])
    why = html.escape(reason or "other")
    safe_detail = html.escape(scrub_text(detail or "")[:200])
    frame_href = html.escape(frame_url, quote=True)
    exp = html.escape(str(max(0, int(expires_in_s))))
    mode_label = html.escape(
        "Take-over confirm" if mode == "takeover" else "Observe only"
    )

    confirm_parts: list[str] = []
    if mode == "takeover" and confirm_url and not takeover_confirmed:
        cu = html.escape(confirm_url, quote=True)
        confirm_parts.append('<form method="POST" action="')
        confirm_parts.append(cu)
        confirm_parts.append(
            '"><button type="submit">Confirm Take-over (pause agent)</button></form>'
        )
        confirm_parts.append(
            '<p class="hint">Confirm only pauses the agent path — '
            "no pair-browse / input in v1.</p>"
        )
    elif mode == "takeover" and takeover_confirmed:
        confirm_parts.append(
            '<p class="ok">Take-over confirmed — agent remains paused; lease warm.</p>'
        )

    parts = [
        '<!DOCTYPE html><html lang="en"><head>',
        '<meta charset="utf-8"/>',
        '<meta name="viewport" content="width=device-width, initial-scale=1"/>',
        '<meta http-equiv="refresh" content="2"/>',
        "<title>Slipstream Watch — ",
        mode_label,
        "</title>",
        "<style>",
        "body{font-family:system-ui,sans-serif;margin:1rem;background:#111;color:#eee;}",
        "img{max-width:100%;border:1px solid #444;background:#000;}",
        ".meta{color:#aaa;font-size:0.9rem;}.hint{color:#888;font-size:0.85rem;}",
        ".ok{color:#8c8;}button{font-size:1rem;padding:0.5rem 1rem;cursor:pointer;}",
        "</style></head><body>",
        "<h1>Slipstream Watch</h1>",
        '<p class="meta">Lease ',
        short,
        "… · ",
        mode_label,
        " · reason=",
        why,
        " · TTL left ~",
        exp,
        "s</p>",
        '<p class="meta">',
        safe_detail,
        "</p>",
        '<p class="hint">Observe-only — no cookies, passwords, tokens, vault, '
        "or CDP auth on this page.</p>",
        '<p><img src="',
        frame_href,
        '" alt="live viewport (JPEG)"/></p>',
    ]
    parts.extend(confirm_parts)
    parts.append("</body></html>")
    return "".join(parts)


def frame_path(lease_id: str, token: str, *, base_path: str = "") -> str:
    """Relative frame URL path with token (for HTML img src)."""
    q = urlencode({"token": token})
    return f"{base_path}/v1/leases/{quote(lease_id, safe='')}/watch/frame?{q}"


def confirm_path(lease_id: str, token: str, *, base_path: str = "") -> str:
    q = urlencode({"token": token})
    return f"{base_path}/v1/leases/{quote(lease_id, safe='')}/watch/confirm?{q}"
