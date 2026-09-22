"""CDP download behavior + mock recorder (split from downloads for quality)."""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from slipstream.downloads import DownloadError, DownloadValidationError



_BEHAVIOR_ALLOW = "allow"
def _resolve_policy_path(path: Path) -> Path:
    return Path(path).expanduser().resolve()


# ---------------------------------------------------------------------------
# CDP download behavior + mock recorder
# ---------------------------------------------------------------------------


@dataclass
class MockDownloadRecorder:
    """Records setDownloadBehavior calls when SLIPSTREAM_MOCK=1."""

    calls: list[dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, *, download_path: str, behavior: str = _BEHAVIOR_ALLOW) -> None:
        with self._lock:
            self.calls.append(
                {
                    "method": "Browser.setDownloadBehavior",
                    "behavior": behavior,
                    # Store lease-relative hint only in public tests — absolute
                    # path kept for assert-contains in unit tests under tmp.
                    "download_path": download_path,
                    "events_enabled": True,
                    "ts": time.time(),
                }
            )

    def clear(self) -> None:
        with self._lock:
            self.calls.clear()


def _browser_ws_url(cdp_http_url: str) -> str:
    """Resolve browser-level WebSocket URL from CDP /json/version."""
    import json
    import urllib.request

    from slipstream.cli import _validate_api_request_url

    base = cdp_http_url.rstrip("/")
    host = urlparse(base).hostname
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise DownloadValidationError(f"refusing non-loopback CDP host {host!r}")
    with urllib.request.urlopen(
        _validate_api_request_url(f"{base}/json/version"), timeout=3.0
    ) as resp:
        ver = json.load(resp)
    ws = ver.get("webSocketDebuggerUrl") if isinstance(ver, dict) else None
    if not isinstance(ws, str) or not ws.startswith("ws"):
        raise DownloadError("no browser WebSocketDebuggerUrl")
    return ws


def configure_chrome_download_behavior(
    cdp_http_url: str,
    download_dir: Path,
    *,
    mock: bool = False,
    recorder: MockDownloadRecorder | None = None,
) -> dict[str, Any]:
    """CDP Browser.setDownloadBehavior → lease downloads dir (or mock record).

    When mock=True, records the call and returns without talking to Chrome.
    Absolute ``download_dir`` is used for Chrome; never returned to agents.
    """
    abs_dir = _resolve_policy_path(download_dir)
    abs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path_str = str(abs_dir)
    if mock:
        if recorder is not None:
            recorder.record(download_path=path_str, behavior=_BEHAVIOR_ALLOW)
        return {"ok": True, "mocked": True, "behavior": _BEHAVIOR_ALLOW}

    from slipstream.cdp_inject import CdpInjectError, _ws_cdp_call

    try:
        ws_url = _browser_ws_url(cdp_http_url)
        _ws_cdp_call(
            ws_url,
            "Browser.setDownloadBehavior",
            {
                "behavior": _BEHAVIOR_ALLOW,
                "downloadPath": path_str,
                "eventsEnabled": True,
            },
        )
    except CdpInjectError as e:
        raise DownloadError(str(e)) from e
    return {"ok": True, "mocked": False, "behavior": _BEHAVIOR_ALLOW}

configure_chrome_download_behavior = configure_chrome_download_behavior
