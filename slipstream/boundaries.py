"""Content-boundary markers — prompt-injection hygiene for page-derived output.

Pattern-steal from agent-browser ``--content-boundaries``: nonce-wrapped markers
separate untrusted page output from trusted tool/skill output in CLI/skill echoes.

Minimal helper only — not a full sandbox. Orchestrators / LLMs must respect the
markers; a capable page could try to mimic them, but the per-process CSPRNG
nonce makes prediction impractical.
"""

from __future__ import annotations

import os
import secrets
from typing import Any
from urllib.parse import urlparse

# Process-lifetime nonce (stable within one CLI/pool process).
_PROCESS_NONCE: str | None = None

BEGIN_FMT = "--- SLIPSTREAM_PAGE_CONTENT nonce={nonce}{origin_part} ---"
END_FMT = "--- END_SLIPSTREAM_PAGE_CONTENT nonce={nonce} ---"


def content_boundaries_enabled() -> bool:
    """True when ``SLIPSTREAM_CONTENT_BOUNDARIES`` is truthy (1/true/yes/on)."""
    raw = os.environ.get("SLIPSTREAM_CONTENT_BOUNDARIES", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def boundary_nonce() -> str:
    """Return (and cache) a per-process CSPRNG nonce for boundary markers."""
    global _PROCESS_NONCE
    if _PROCESS_NONCE is None:
        _PROCESS_NONCE = secrets.token_hex(8)
    return _PROCESS_NONCE


def reset_boundary_nonce_for_tests() -> None:
    """Clear cached nonce — tests only."""
    global _PROCESS_NONCE
    _PROCESS_NONCE = None


def sanitize_origin(origin: str | None) -> str | None:
    """Reduce origin/page_url to a single-line ``scheme://host[:port]``.

    ADV-BOUND-001: unsanitized newlines (or forged ``--- END_…`` suffixes) in
    origin= break marker lines so naive parsers treat trailing attacker text
    as outside the untrusted zone. Only a strict origin form is emitted.

    ADV-BOUND-002: refuse hosts / netlocs that still contain whitespace (or
    percent-encoded space). A space in ``origin=…`` would emit a second
    ``origin=`` token on the BEGIN marker line (dual-origin forge).
    """
    if origin is None:
        return None
    if not isinstance(origin, str):
        origin = str(origin)
    # Drop C0 controls + DEL; collapse to single line first.
    cleaned = "".join(ch for ch in origin if ord(ch) >= 32 and ord(ch) != 127)
    cleaned = cleaned.replace("\r", "").replace("\n", "").strip()
    if not cleaned:
        return None
    # Marker / whitespace payloads: try salvage via urlparse, then re-validate.
    try:
        parsed = urlparse(cleaned)
    except Exception:
        return None
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower().rstrip(".")
    if scheme not in ("http", "https") or not host:
        return None
    # ADV-BOUND-002: host must be a single token — no space/tab/%20/---.
    if (
        " " in host
        or "\t" in host
        or "%" in host
        or "---" in host
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in host)
    ):
        return None
    # Rebuild origin only — drop path/query/fragment/userinfo.
    netloc = host
    if parsed.port:
        netloc = f"{host}:{parsed.port}"
    out = f"{scheme}://{netloc}"[:512]
    # Final guard: emitted value must never introduce a second origin= field.
    if " " in out or "\t" in out or "origin=" in out.lower():
        return None
    return out


def wrap_page_content(
    text: str,
    *,
    origin: str | None = None,
    nonce: str | None = None,
    force: bool = False,
) -> str:
    """Wrap page-derived ``text`` in nonce markers when boundaries are enabled.

    When disabled (and ``force`` is False), returns ``text`` unchanged so existing
    skill/CLI echoes stay quiet until opted in.
    """
    if text is None:
        text = ""
    if not isinstance(text, str):
        text = str(text)
    if not force and not content_boundaries_enabled():
        return text
    n = nonce or boundary_nonce()
    safe_origin = sanitize_origin(origin)
    origin_part = f" origin={safe_origin}" if safe_origin else ""
    begin = BEGIN_FMT.format(nonce=n, origin_part=origin_part)
    end = END_FMT.format(nonce=n)
    return f"{begin}\n{text}\n{end}"


def boundary_meta(*, origin: str | None = None, nonce: str | None = None) -> dict[str, Any]:
    """JSON ``_boundary`` object for orchestrators (agent-browser-style)."""
    return {"nonce": nonce or boundary_nonce(), "origin": sanitize_origin(origin)}
