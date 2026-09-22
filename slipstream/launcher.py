"""Chromium launcher — one process tree per slot via CDP.

Finds google-chrome / chromium / Chrome-for-Testing. Launches with
--remote-debugging-port and --user-data-dir=<Space>. When SLIPSTREAM_MOCK=1
(or config.mock), launch is a no-op stub (fake PID/ports).
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from slipstream.config import PoolConfig


CHROME_CANDIDATES = (
    "google-chrome-stable",
    "google-chrome",
    "chromium",
    "chromium-browser",
    "chrome",
    "google-chrome-for-testing",
)


@dataclass
class LaunchHandle:
    pid: int
    cdp_port: int
    cdp_http_url: str
    cdp_ws_url: str | None
    user_data_dir: Path
    process: subprocess.Popen | None = None
    mocked: bool = False


def find_chrome_binary(explicit: str | None = None) -> str | None:
    """Resolve Chrome/Chromium binary.

    If ``explicit`` (SLIPSTREAM_CHROME / path) is set and missing/non-executable,
    return None — do **not** fall through to PATH candidates. Auto-detect only
    when no explicit path/name was requested.
    """
    if explicit:
        path = Path(explicit)
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        found = shutil.which(explicit)
        if found:
            return found
        return None  # explicit set but unusable — no PATH fallthrough
    for name in CHROME_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    # Common Linux install paths
    for path in (
        "/usr/bin/google-chrome-stable",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/opt/google/chrome/chrome",
    ):
        if Path(path).is_file() and os.access(path, os.X_OK):
            return path
    return None


class ChromiumLauncher:
    """Launch / stop one Chromium process tree bound to a Space profile dir."""

    def __init__(self, config: PoolConfig):
        self.config = config
        # Validate via find_chrome_binary (explicit missing → None)
        self._binary = find_chrome_binary(config.chrome_binary)

    @property
    def binary(self) -> str | None:
        return self._binary

    def launch(self, space_id: str, cdp_port: int) -> LaunchHandle:
        user_data = self.config.space_path(space_id)
        user_data.mkdir(parents=True, exist_ok=True)

        if self.config.mock:
            return LaunchHandle(
                pid=10_000 + cdp_port,  # fake
                cdp_port=cdp_port,
                cdp_http_url=f"http://127.0.0.1:{cdp_port}",
                cdp_ws_url=f"ws://127.0.0.1:{cdp_port}/devtools/browser/mock",
                user_data_dir=user_data,
                process=None,
                mocked=True,
            )

        if not self._binary:
            raise RuntimeError(
                "No Chrome/Chromium binary found. Install google-chrome or "
                "chromium, set SLIPSTREAM_CHROME, or use SLIPSTREAM_MOCK=1."
            )

        args = [
            self._binary,
            f"--remote-debugging-port={cdp_port}",
            f"--user-data-dir={user_data}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-features=TranslateUI",
            "--disable-component-update",
        ]
        if self.config.headless:
            args.append("--headless=new")
        # Avoid GPU / tiny-/dev/shm issues on headless boxes
        args.extend(["--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage"])
        args.append("about:blank")

        proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # own process group / tree
        )
        cdp_http_url = f"http://127.0.0.1:{cdp_port}"
        # Wait until DevTools HTTP answers (or process dies). The old 0.3s sleep
        # stub could return handles whose CDP was not yet bound — live K=5 stress
        # then saw Connection refused / lease-with-dead-CDP.
        ready_timeout = float(os.environ.get("SLIPSTREAM_CDP_READY_TIMEOUT", "20"))
        deadline = time.monotonic() + ready_timeout
        last_err: Exception | None = None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"Chromium exited early (code={proc.returncode}) before CDP "
                    f"ready on port {cdp_port} for space_id={space_id!r}"
                )
            try:
                with urllib.request.urlopen(
                    f"{cdp_http_url}/json/version", timeout=0.5
                ) as resp:
                    if resp.status == 200:
                        break
            except Exception as e:  # noqa: BLE001 — probe loop
                last_err = e
                time.sleep(0.05)
            else:
                break
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                proc.kill()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            raise RuntimeError(
                f"CDP not ready at {cdp_http_url} within {ready_timeout}s "
                f"for space_id={space_id!r} (last={last_err})"
            )

        return LaunchHandle(
            pid=proc.pid,
            cdp_port=cdp_port,
            cdp_http_url=cdp_http_url,
            cdp_ws_url=None,  # discover via /json/version when needed
            user_data_dir=user_data,
            process=proc,
            mocked=False,
        )

    def stop(self, handle: LaunchHandle | None, grace_seconds: float = 2.0) -> None:
        """Best-effort teardown of a Chromium process tree.

        SIGTERM → wait(grace) → SIGKILL → wait(2). The final wait catches
        ``TimeoutExpired`` so pool release can finish clearing lease state
        even if the OS has not fully reaped the process yet (orphan risk is
        accepted; lease bookkeeping must not desync).
        """
        if handle is None:
            return
        if handle.mocked:
            return
        proc = handle.process
        if proc is None:
            # Orphaned pid — best-effort kill process group
            try:
                os.killpg(handle.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    os.kill(handle.pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
            return
        if proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            proc.terminate()
        try:
            proc.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                proc.kill()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                # Best-effort: do not block release / lease-state cleanup.
                pass
