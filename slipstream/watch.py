"""Live Watch + thin exclusive pair-browse over leased CDP.

Minted on need_human as a short-TTL tokenized watch_url. Observe-only by
default. After Take-over confirm, human click/type/scroll is bridged into
the leased CDP (Input.dispatchMouseEvent / dispatchKeyEvent / mouseWheel)
while the agent stays paused. Cede / TTL / cancel disables input and clears
pause; task_done / session end revokes watch_url (401/410) and drops the
input bridge. No secrets on page / stream / payloads.

Frames are sensitive screenshots of the leased viewport (ADV-WATCH-007) —
treat watch_url like a screen-share secret (v1: credential-in-URL).
"""

from __future__ import annotations

_PATH_LEASES = "/v1/leases/"

import base64
import html
import secrets
import time
from dataclasses import dataclass
from urllib.parse import quote, urlencode, urlparse

# Minimal JPEG SOI+EOI for mock / fallback (not a secret; low entropy).
_MOCK_JPEG = bytes((0xFF, 0xD8, 0xFF, 0xD9))

# Clickjacking defenses for Watch HTML / frame / confirm (ADV-WATCH-003).
WATCH_CLICKJACK_HEADERS: dict[str, str] = {
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'",
}


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


class WatchForbiddenError(Exception):
    """Input / pair-browse refused (maps to HTTP 403) — observe-only or ceded."""

    def __init__(self, detail: str = "forbidden"):
        super().__init__(detail)
        self.detail = detail


class WatchInputError(Exception):
    """Bad input payload (maps to HTTP 400)."""

    def __init__(self, detail: str = "invalid_input"):
        super().__init__(detail)
        self.detail = detail


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
    input_enabled: bool = False
    # Bumped on cede / revoke / Watch TTL clear so in-flight CDP input cannot ack success.
    input_epoch: int = 0
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
        f"{base_url.rstrip('/')}{_PATH_LEASES}{quote(lease_id, safe='')}/watch"
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


def assert_ws_debugger_url(ws_url: str) -> str:
    """Reject non-loopback / non-ws(s) webSocketDebuggerUrl before connect.

    ADV-WATCH-006: Chrome may advertise a debugger WS; only loopback ws/wss
    are allowed for pool-side Watch screenshot and credential inject.
    """
    parsed = urlparse(ws_url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("ws", "wss"):
        raise WatchCaptureError(
            f"refusing non-ws(s) webSocketDebuggerUrl scheme {scheme!r}"
        )
    host = parsed.hostname
    if not _host_is_loopback(host):
        raise WatchCaptureError(
            f"refusing non-loopback webSocketDebuggerUrl host {host!r}"
        )
    return ws_url


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
        ws_url = assert_ws_debugger_url(_page_ws_url(cdp_http_url))
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



def _signed_in_chip_html(*, signed_in: bool, signed_in_host: str | None) -> str:
    if not signed_in:
        return '<span class="chip off">not signed-in</span>'
    host = (" · " + html.escape(signed_in_host)) if signed_in_host else ""
    return (
        '<span class="chip on" title="Space profile holds session cookies">'
        f"signed-in{host}</span>"
    )


def _mark_signed_in_form_html(mark_url: str) -> str:
    mu = html.escape(mark_url, quote=True)
    return (
        f'<form method="POST" action="{mu}" class="hint" style="margin-top:0.75rem">'
        "<label>Host label (optional) "
        '<input name="host" maxlength="128" placeholder="github.com"/>'
        "</label> "
        '<button type="submit">Mark Space signed-in</button>'
        '<p class="hint">Cookies already persist in the Space profile dir; '
        "this only sets the signed-in badge (never dumps cookies).</p>"
        "</form>"
    )


def _signed_in_ok_html(signed_in_host: str | None) -> str:
    host_bit = (" (" + html.escape(signed_in_host) + ")") if signed_in_host else ""
    return (
        '<p class="ok">Space tagged signed-in'
        + host_bit
        + " — profile cookies reused on later leases.</p>"
    )


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
    input_enabled: bool = False,
    input_url: str | None = None,
    cede_url: str | None = None,
    events_url: str | None = None,
    signed_in: bool = False,
    signed_in_host: str | None = None,
    mark_signed_in_url: str | None = None,
) -> str:
    """Watch HTML shell. Input bridge only when confirmed+enabled; no secrets.

    All dynamic pieces are HTML-escaped before concatenation (no raw f-string
    interpolation of caller strings into markup). Typed keystrokes are POSTed
    to the pool — never echoed back into the page.
    """
    from slipstream.alerts import scrub_text

    short = html.escape((lease_id or "?")[:8])
    why = html.escape(reason or "other")
    safe_detail = html.escape(scrub_text(detail or "")[:200])
    frame_href = html.escape(frame_url, quote=True)
    exp = html.escape(str(max(0, int(expires_in_s))))
    if input_enabled:
        mode_label = html.escape("Pair-browse (exclusive)")
    elif mode == "takeover":
        mode_label = html.escape("Take-over confirm")
    else:
        mode_label = html.escape("Observe only")

    confirm_parts: list[str] = []
    if mode == "takeover" and confirm_url and not takeover_confirmed:
        cu = html.escape(confirm_url, quote=True)
        confirm_parts.append('<form method="POST" action="')
        confirm_parts.append(cu)
        confirm_parts.append(
            '"><button type="submit">Confirm Take-over (pause agent)</button></form>'
        )
        confirm_parts.append(
            '<p class="hint">Confirm pauses the agent, then enables click/type/scroll '
            "into the leased session. Observe-only until then.</p>"
        )
    elif mode == "takeover" and takeover_confirmed and input_enabled:
        confirm_parts.append(
            '<p class="ok">Take-over confirmed — agent paused; pair-browse on '
            "(click / type / scroll). Cede returns drive to the agent.</p>"
        )
        if cede_url:
            cu = html.escape(cede_url, quote=True)
            confirm_parts.append('<form method="POST" action="')
            confirm_parts.append(cu)
            confirm_parts.append(
                '"><button type="submit">Cede (return drive to agent)</button></form>'
            )
    elif mode == "takeover" and takeover_confirmed and not input_enabled:
        confirm_parts.append(
            '<p class="ok">Control ceded — agent may resume; observe-only.</p>'
        )

    # Meta-refresh only while observe-only (refresh would steal focus mid-type).
    refresh_meta = (
        ""
        if input_enabled
        else '<meta http-equiv="refresh" content="2"/>'
    )

    events_href = html.escape(events_url, quote=True) if events_url else ""

    parts = [
        '<!DOCTYPE html><html lang="en"><head>',
        '<meta charset="utf-8"/>',
        '<meta name="viewport" content="width=device-width, initial-scale=1"/>',
        refresh_meta,
        "<title>Slipstream Watch — ",
        mode_label,
        "</title>",
        "<style>",
        "body{font-family:system-ui,sans-serif;margin:1rem;background:#111;color:#eee;}",
        ".layout{display:flex;gap:1rem;align-items:flex-start;flex-wrap:wrap;}",
        ".main{flex:1 1 420px;min-width:280px;}",
        ".feed{flex:0 1 320px;max-width:100%;max-height:70vh;overflow:auto;",
        "border:1px solid #333;background:#1a1a1a;padding:0.5rem 0.75rem;",
        "font-size:0.8rem;line-height:1.35;}",
        ".feed h2{margin:0 0 0.5rem;font-size:0.95rem;color:#ccc;}",
        ".feed .ev{border-top:1px solid #2a2a2a;padding:0.35rem 0;}",
        ".feed .ev .t{color:#888;font-variant-numeric:tabular-nums;}",
        ".feed .ev .k{color:#8af;font-weight:600;margin-right:0.35rem;}",
        ".feed .ev .o{color:#8c8;}",
        ".feed .ev .o.err{color:#c88;}",
        ".feed .empty{color:#666;}",
        "img{max-width:100%;border:1px solid #444;background:#000;}",
        ".meta{color:#aaa;font-size:0.9rem;}.hint{color:#888;font-size:0.85rem;}",
        ".ok{color:#8c8;}button{font-size:1rem;padding:0.5rem 1rem;cursor:pointer;}",
".chip{display:inline-block;padding:0.15rem 0.5rem;border-radius:999px;font-size:0.75rem;font-weight:600;vertical-align:middle;}",
".chip.on{background:#1a4;color:#cfc;border:1px solid #2a6;}",
".chip.off{background:#333;color:#aaa;border:1px solid #555;}",
        "#viewport{display:inline-block;position:relative;max-width:100%;}",
        "#viewport.drive{cursor:crosshair;outline:2px solid #4a4;}",
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
        "s · ",
        _signed_in_chip_html(signed_in=signed_in, signed_in_host=signed_in_host),
        "</p>",
        '<p class="meta">',
        safe_detail,
        "</p>",
        '<p class="hint">No cookies, passwords, tokens, vault, or CDP auth on this page. '
        "Observe-only blocks input until Take-over is confirmed. Activity feed is "
        "lease-scoped, scrubbed, and revoked with watch_url.</p>",
        '<div class="layout"><div class="main">',
        '<div id="viewport"',
        ' class="drive"' if input_enabled else "",
        '><img id="frame" src="',
        frame_href,
        '" alt="live viewport (JPEG)"/></div>',
    ]

    parts.extend(confirm_parts)
    # Login-once: captain can mark Space signed-in after human login (badge only).
    if mark_signed_in_url and (reason or "") == "login" and not signed_in:
        parts.append(_mark_signed_in_form_html(mark_signed_in_url))
    elif signed_in:
        parts.append(_signed_in_ok_html(signed_in_host))
    parts.append("</div>")  # .main
    # Activity feed dock (CTO 007) — polls tokenized /watch/events
    parts.append('<aside class="feed" id="feed" aria-label="Activity feed">')
    parts.append("<h2>Activity</h2>")
    parts.append('<div id="feed-list"><p class="empty">No events yet.</p></div>')
    parts.append("</aside></div>")  # feed + layout
    if events_url:
        # Poll tokenized events; never embed secrets — summaries are server-scrubbed.
        parts.append("<script>(function(){")
        parts.append(f"var EVENTS_URL={json_quote(events_url)};")
        parts.append(
            "var list=document.getElementById('feed-list');"
            "var after=0;"
            "function fmt(ts){try{return new Date(ts*1000).toLocaleTimeString();}catch(e){return '';}}"
            "function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/\"/g,'&quot;');}"
            "function addRows(rows){"
            "if(!rows||!rows.length)return;"
            "var empty=list.querySelector('.empty');if(empty)empty.remove();"
            "for(var i=0;i<rows.length;i++){"
            "var r=rows[i];after=Math.max(after,r.seq||0);"
            "var d=document.createElement('div');d.className='ev';"
            "var oc=(r.outcome&&r.outcome!=='ok')?'o err':'o';"
            "d.innerHTML='<span class=\"t\">'+fmt(r.ts)+'</span> '"
            "+'<span class=\"k\">'+esc(r.kind)+'</span>'"
            "+'<span>'+esc(r.summary)+'</span> '"
            "+'<span class=\"'+oc+'\">'+esc(r.outcome)+'</span>';"
            "list.appendChild(d);}"
            "list.parentElement.scrollTop=list.parentElement.scrollHeight;}"
            "function tick(){var u=EVENTS_URL+(EVENTS_URL.indexOf('?')>=0?'&':'?')+'after_seq='+after;"
            "fetch(u,{credentials:'same-origin'}).then(function(res){"
            "if(res.status===410||res.status===401){return null;}"
            "return res.ok?res.json():null;}).then(function(data){"
            "if(data&&data.events)addRows(data.events);}).catch(function(){});}"
            "tick();setInterval(tick,2000);"
        )
        parts.append("})();</script>")


    if input_enabled and input_url:
        # Inline bridge: coordinates + keys POST to pool; never echo typed text.
        parts.append("<script>(function(){")
        parts.append(f"var INPUT_URL={json_quote(input_url)};")
        parts.append(
            "var img=document.getElementById('frame');"
            "var vp=document.getElementById('viewport');"
            "function refresh(){if(!img)return;var u=img.getAttribute('data-src')||img.src.split('&_t=')[0];"
            "img.setAttribute('data-src',u);img.src=u+(u.indexOf('?')>=0?'&':'?')+'_t='+Date.now();}"
            "setInterval(refresh,1500);"
            "function post(body){"
            "fetch(INPUT_URL,{method:'POST',headers:{'Content-Type':'application/json'},"
            "body:JSON.stringify(body),credentials:'same-origin'}).catch(function(){});}"
            "function rel(ev){var r=img.getBoundingClientRect();"
            "var x=(ev.clientX-r.left)*(img.naturalWidth||r.width)/Math.max(r.width,1);"
            "var y=(ev.clientY-r.top)*(img.naturalHeight||r.height)/Math.max(r.height,1);"
            "return {x:Math.round(x),y:Math.round(y)};}"
            "vp.addEventListener('click',function(ev){ev.preventDefault();var p=rel(ev);"
            "post({kind:'click',x:p.x,y:p.y,button:'left'});});"
            "vp.addEventListener('wheel',function(ev){ev.preventDefault();var p=rel(ev);"
            "post({kind:'scroll',x:p.x,y:p.y,deltaX:ev.deltaX,deltaY:ev.deltaY});},{passive:false});"
            "window.addEventListener('keydown',function(ev){"
            "if(ev.metaKey||ev.ctrlKey||ev.altKey)return;"
            "if(ev.key.length===1){post({kind:'type',text:ev.key});}"
            "else{post({kind:'key',key:ev.key,code:ev.code||'',type:'keyDown'});}"
            "});"
            "})();</script>"
        )

    parts.append("</body></html>")
    return "".join(parts)


def json_quote(s: str) -> str:
    """JSON-encode a string for safe embedding in a <script> literal."""
    import json as _json

    return _json.dumps(s)


def frame_path(lease_id: str, token: str, *, base_path: str = "") -> str:
    """Relative frame URL path with token (for HTML img src)."""
    q = urlencode({"token": token})
    return f"{base_path}{_PATH_LEASES}{quote(lease_id, safe='')}/watch/frame?{q}"


def confirm_path(lease_id: str, token: str, *, base_path: str = "") -> str:
    q = urlencode({"token": token})
    return f"{base_path}{_PATH_LEASES}{quote(lease_id, safe='')}/watch/confirm?{q}"


def input_path(lease_id: str, token: str, *, base_path: str = "") -> str:
    q = urlencode({"token": token})
    return f"{base_path}{_PATH_LEASES}{quote(lease_id, safe='')}/watch/input?{q}"


def cede_path(lease_id: str, token: str, *, base_path: str = "") -> str:
    q = urlencode({"token": token})
    return f"{base_path}{_PATH_LEASES}{quote(lease_id, safe='')}/watch/cede?{q}"


def events_path(lease_id: str, token: str, *, base_path: str = "") -> str:
    """Tokenized activity-feed JSON path (same TTL/revoke as Watch)."""
    q = urlencode({"token": token})
    return f"{base_path}{_PATH_LEASES}{quote(lease_id, safe='')}/watch/events?{q}"


# --- pair-browse CDP input bridge -----------------------------------------

# Keys that must never appear in an input-bridge payload (alerts refuse list).
_INPUT_REFUSED_KEYS = frozenset(
    {
        "cookie",
        "cookies",
        "password",
        "passwd",
        "token",
        "private_key",
        "jwt",
        "bearer",
        "access_key",
        "credential",
        "credentials",
        "authorization",
        "secret",
        "secrets",
        "storage_state",
        "cdp_http",
        "cdp_http_url",
        "websocketdebuggerurl",
    }
)


def _refuse_secret_keys(body: dict) -> None:
    for k in body:
        if not isinstance(k, str):
            continue
        kl = k.strip().lower().replace("-", "_")
        if kl in _INPUT_REFUSED_KEYS:
            raise WatchInputError(f"refused secret-like field: {k}")


def parse_watch_input(body: dict | None) -> dict:
    """Validate and normalize a pair-browse input event (no secret fields)."""
    if not isinstance(body, dict) or not body:
        raise WatchInputError("input body required")
    _refuse_secret_keys(body)
    kind = body.get("kind")
    if kind not in ("click", "type", "key", "scroll"):
        raise WatchInputError("kind must be click|type|key|scroll")

    if kind == "click":
        try:
            x = float(body["x"])
            y = float(body["y"])
        except (KeyError, TypeError, ValueError) as e:
            raise WatchInputError("click requires numeric x,y") from e
        button = body.get("button") or "left"
        if button not in ("left", "middle", "right"):
            raise WatchInputError("button must be left|middle|right")
        return {"kind": "click", "x": x, "y": y, "button": button}

    if kind == "type":
        text = body.get("text")
        if not isinstance(text, str) or not text:
            raise WatchInputError("type requires non-empty text")
        if len(text) > 64:
            raise WatchInputError("type text too long (max 64)")
        return {"kind": "type", "text": text}

    if kind == "key":
        key = body.get("key")
        if not isinstance(key, str) or not key or len(key) > 32:
            raise WatchInputError("key requires short key string")
        code = body.get("code") or ""
        if not isinstance(code, str) or len(code) > 32:
            raise WatchInputError("code too long")
        etype = body.get("type") or "keyDown"
        if etype not in ("keyDown", "keyUp", "rawKeyDown", "char"):
            raise WatchInputError("type must be keyDown|keyUp|rawKeyDown|char")
        return {"kind": "key", "key": key, "code": code, "type": etype}

    # scroll
    try:
        x = float(body.get("x", 0))
        y = float(body.get("y", 0))
        dx = float(body.get("deltaX", 0))
        dy = float(body.get("deltaY", 0))
    except (TypeError, ValueError) as e:
        raise WatchInputError("scroll requires numeric x,y,deltaX,deltaY") from e
    return {"kind": "scroll", "x": x, "y": y, "deltaX": dx, "deltaY": dy}


def dispatch_cdp_input(
    cdp_http_url: str, event: dict, *, mock: bool = False, mock_log: list | None = None
) -> dict:
    """Forward a normalized input event into leased CDP (or mock recorder).

    Returns a scrubbed ack — never echoes typed text / secrets.
    """
    kind = event["kind"]
    if mock or not cdp_http_url:
        if mock_log is not None:
            # ADV-PAIR-003: never store full typed text — kind + text_len only.
            rec = {"kind": kind, "cdp_http_url": cdp_http_url}
            if kind == "click":
                rec.update({"x": event["x"], "y": event["y"], "button": event["button"]})
            elif kind == "type":
                rec.update({"text_len": len(event["text"])})
            elif kind == "key":
                rec.update({"key": event["key"], "code": event["code"], "type": event["type"]})
            else:
                rec.update(
                    {
                        "x": event["x"],
                        "y": event["y"],
                        "deltaX": event["deltaX"],
                        "deltaY": event["deltaY"],
                    }
                )
            mock_log.append(rec)
        return {"ok": True, "kind": kind, "mock": True}

    assert_cdp_loopback(cdp_http_url)
    from slipstream.cdp_inject import CdpInjectError, _page_ws_url, _ws_cdp_call

    try:
        ws_url = assert_ws_debugger_url(_page_ws_url(cdp_http_url))
        if kind == "click":
            btn = event["button"]
            btn_bits = {"left": 1, "right": 2, "middle": 4}[btn]
            for etype in ("mousePressed", "mouseReleased"):
                _ws_cdp_call(
                    ws_url,
                    "Input.dispatchMouseEvent",
                    {
                        "type": etype,
                        "x": event["x"],
                        "y": event["y"],
                        "button": btn,
                        "buttons": btn_bits if etype == "mousePressed" else 0,
                        "clickCount": 1,
                    },
                )
        elif kind == "type":
            _ws_cdp_call(ws_url, "Input.insertText", {"text": event["text"]})
        elif kind == "key":
            params: dict = {
                "type": event["type"],
                "key": event["key"],
            }
            if event.get("code"):
                params["code"] = event["code"]
            # Common non-char keys need windowsVirtualKeyCode hints
            _KEY_VK = {
                "Enter": 13,
                "Tab": 9,
                "Backspace": 8,
                "Escape": 27,
                "ArrowLeft": 37,
                "ArrowUp": 38,
                "ArrowRight": 39,
                "ArrowDown": 40,
                "Delete": 46,
            }
            if event["key"] in _KEY_VK:
                params["windowsVirtualKeyCode"] = _KEY_VK[event["key"]]
                params["nativeVirtualKeyCode"] = _KEY_VK[event["key"]]
            _ws_cdp_call(ws_url, "Input.dispatchKeyEvent", params)
        else:  # scroll
            _ws_cdp_call(
                ws_url,
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseWheel",
                    "x": event["x"],
                    "y": event["y"],
                    "deltaX": event["deltaX"],
                    "deltaY": event["deltaY"],
                },
            )
    except CdpInjectError as e:
        raise WatchCaptureError(str(e)) from e
    except WatchCaptureError:
        raise
    except Exception as e:  # noqa: BLE001 — websocket / CDP transport
        raise WatchCaptureError(f"CDP input transport failed: {e}") from e

    return {"ok": True, "kind": kind}
