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
    origin_part = f" origin={origin}" if origin else ""
    begin = BEGIN_FMT.format(nonce=n, origin_part=origin_part)
    end = END_FMT.format(nonce=n)
    return f"{begin}\n{text}\n{end}"


def boundary_meta(*, origin: str | None = None, nonce: str | None = None) -> dict[str, Any]:
    """JSON ``_boundary`` object for orchestrators (agent-browser-style)."""
    return {"nonce": nonce or boundary_nonce(), "origin": origin}
