"""Ops session list — Spaces/leases with duration + optional watch_url.

Thin Browserbase-style fleet view (ui-peers steal #7). ``watch_url`` is only
included when a valid short-TTL tokenized Watch already exists (need_human);
treat it as a screen-share secret — never invent long-lived public URLs and
never log the tokenized URL.
"""

from __future__ import annotations

import html
import time
from typing import Any

from slipstream.watch import build_watch_url

_OPS_JSON = "/v1/ops/sessions"


def duration_seconds(leased_at: float | None, *, now: float | None = None) -> int | None:
    """Whole seconds since lease start; None if unknown."""
    if leased_at is None:
        return None
    t = time.time() if now is None else now
    return max(0, int(t - float(leased_at)))


def alive_watch_url(
    *,
    lease_id: str,
    token: str | None,
    revoked: bool,
    expires_at: float,
    base_url: str,
    now: float | None = None,
    mode: str | None = None,
) -> str | None:
    """Return tokenized watch_url only when the Watch session is still valid."""
    if not token or revoked:
        return None
    t = time.time() if now is None else now
    if t >= float(expires_at):
        return None
    return build_watch_url(lease_id, token, base_url=base_url, mode=mode)


def session_row(
    *,
    space_id: str,
    lease_id: str | None,
    agent_id: str | None,
    status: str,
    leased_at: float | None,
    user_metadata: dict[str, Any] | None,
    signed_in: bool,
    signed_in_host: str | None = None,
    watch_url: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """One ops session row (safe for JSON; omit watch_url when absent)."""
    row: dict[str, Any] = {
        "space_id": space_id,
        "status": status,
        "leased_at": leased_at,
        "duration_s": duration_seconds(leased_at, now=now),
        "user_metadata": dict(user_metadata or {}),
        "signed_in": bool(signed_in),
    }
    if lease_id:
        row["lease_id"] = lease_id
    if agent_id:
        row["agent_id"] = agent_id
    if signed_in and signed_in_host:
        row["signed_in_host"] = signed_in_host
    if watch_url:
        row["watch_url"] = watch_url
    return row


def _fmt_duration(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds}s"
    mins, sec = divmod(seconds, 60)
    if mins < 60:
        return f"{mins}m{sec:02d}s"
    hours, mins = divmod(mins, 60)
    return f"{hours}h{mins:02d}m"


def _tags_cell(meta: dict[str, Any]) -> str:
    if not meta:
        return "—"
    parts: list[str] = []
    for key in sorted(meta.keys()):
        val = meta[key]
        parts.append(f"{key}=…" if isinstance(val, dict) else f"{key}={val}")
        if len(parts) >= 6:
            parts.append("…")
            break
    return ", ".join(parts)


def _html_row(s: dict[str, Any]) -> str:
    space = html.escape(str(s.get("space_id") or ""))
    lease = html.escape(str(s.get("lease_id") or "—"))
    status = html.escape(str(s.get("status") or ""))
    dur = html.escape(_fmt_duration(s.get("duration_s")))
    tags = html.escape(_tags_cell(s.get("user_metadata") or {}))
    signed = "yes" if s.get("signed_in") else "no"
    host = s.get("signed_in_host")
    if host:
        signed = f"yes ({html.escape(str(host))})"
    watch = s.get("watch_url")
    action = (
        f'<a class="watch" href="{html.escape(str(watch), quote=True)}" '
        'target="_blank" rel="noopener">Open Watch</a>'
        if watch
        else "—"
    )
    return (
        f"<tr><td>{space}</td><td>{lease}</td><td>{status}</td>"
        f"<td>{dur}</td><td>{tags}</td><td>{signed}</td><td>{action}</td></tr>"
    )


def render_sessions_html(
    sessions: list[dict[str, Any]], *, title: str = "Slipstream sessions"
) -> str:
    """Minimal loopback ops HTML table with Open Watch when watch_url present."""
    body = "\n".join(_html_row(s) for s in sessions) or (
        '<tr><td colspan="7">No active sessions</td></tr>'
    )
    safe_title = html.escape(title)
    note = html.escape(_OPS_JSON)
    return (
        "<!DOCTYPE html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<title>{safe_title}</title>"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<style>"
        "body{font:14px/1.4 system-ui,sans-serif;margin:1.5rem;background:#0f1115;color:#e8eaed}"
        "h1{font-size:1.15rem;margin:0 0 1rem}"
        "table{border-collapse:collapse;width:100%;max-width:1100px}"
        "th,td{border:1px solid #2a2f3a;padding:.45rem .6rem;text-align:left;vertical-align:top}"
        "th{background:#1a1f2a;color:#9aa0a6}"
        "a.watch{color:#8ab4f8;font-weight:600}"
        ".note{color:#9aa0a6;font-size:12px;margin-top:1rem}"
        f"</style></head><body><h1>{safe_title}</h1>"
        "<table><thead><tr>"
        "<th>space</th><th>lease</th><th>status</th><th>duration</th>"
        "<th>tags</th><th>signed-in</th><th>watch</th>"
        f"</tr></thead><tbody>\n{body}\n</tbody></table>"
        f'<p class="note">JSON: <code>{note}</code> · '
        "watch_url is a short-TTL secret — do not log or share.</p>"
        "</body></html>\n"
    )


def format_sessions_table(sessions: list[dict[str, Any]]) -> str:
    """Plain-text columns for CLI (watch presence only — never print secret URL)."""
    headers = ("SPACE", "LEASE", "STATUS", "DUR", "SIGNED", "TAGS", "WATCH")
    lines = ["\t".join(headers)]
    for s in sessions:
        signed = "yes" if s.get("signed_in") else "no"
        host = s.get("signed_in_host")
        if host:
            signed = f"yes@{host}"
        lines.append(
            "\t".join(
                [
                    str(s.get("space_id") or ""),
                    str(s.get("lease_id") or "—")[:8],
                    str(s.get("status") or ""),
                    _fmt_duration(s.get("duration_s")),
                    signed,
                    _tags_cell(s.get("user_metadata") or {}),
                    "yes" if s.get("watch_url") else "—",
                ]
            )
        )
    if len(lines) == 1:
        lines.append("(no active sessions)")
    return "\n".join(lines) + "\n"
