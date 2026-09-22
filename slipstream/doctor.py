"""slipstream doctor — preflight checks for out-of-box agent use.

Checks (real path assumes SLIPSTREAM_MOCK off):
  - Chrome/Chromium binary present
  - CDP probe (ephemeral headless Chrome → GET /json/version)
  - Pool GET /healthz
  - Spaces root writable
  - Skill file skills/slipstream/SKILL.md
  - Optional composed /watch skill (warn only; never vendored)

Human output by default; --json for machines. Exit 0 if no failures
(warnings allowed); exit 1 if any check failed.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from slipstream import __version__
from slipstream.cli import DEFAULT_URL, resolve_base_url
from slipstream.config import PoolConfig
from slipstream.launcher import find_chrome_binary

CheckStatus = Literal["ok", "warn", "fail", "skip"]


@dataclass
class CheckResult:
    name: str
    status: CheckStatus
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class DoctorReport:
    ok: bool
    version: str
    mock: bool
    base_url: str
    checks: list[CheckResult]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "version": self.version,
            "mock": self.mock,
            "base_url": self.base_url,
            "checks": [asdict(c) for c in self.checks],
        }



def find_watch_skill() -> Path | None:
    """Locate composed Claude /watch skill (optional; never vendored)."""
    home = Path.home()
    candidates = [
        Path.cwd() / "skills" / "watch" / "SKILL.md",
        home / ".claude" / "skills" / "watch" / "SKILL.md",
        home / ".codex" / "skills" / "watch" / "SKILL.md",
        home / ".agents" / "skills" / "watch" / "SKILL.md",
        home / ".openclaw" / "skills" / "watch" / "SKILL.md",
    ]
    env = os.environ.get("SLIPSTREAM_WATCH_SKILL")
    if env:
        candidates.insert(0, Path(env))
    for path in candidates:
        if path.is_file():
            return path.resolve()
    return None


def find_skill_path() -> Path | None:
    """Locate skills/slipstream/SKILL.md (repo, packaged, or override).

    Prefers SLIPSTREAM_SKILL_PATH, then installed ``skills.slipstream`` package
    data (non-editable wheels), then editable/repo layout.
    """
    candidates: list[Path] = []
    env = os.environ.get("SLIPSTREAM_SKILL_PATH")
    if env:
        candidates.append(Path(env))
    try:
        import skills.slipstream as _skill_pkg  # type: ignore[import-not-found]

        bundled = Path(_skill_pkg.__file__).resolve().parent / "SKILL.md"
        candidates.append(bundled)
    except Exception:  # noqa: BLE001 — optional import path
        pass
    pkg_root = Path(__file__).resolve().parent.parent  # repo root when editable
    candidates.extend(
        [
            pkg_root / "skills" / "slipstream" / "SKILL.md",
            Path.cwd() / "skills" / "slipstream" / "SKILL.md",
            Path.cwd() / "skills" / "slipstream-browser" / "SKILL.md",
        ]
    )
    for path in candidates:
        if path.is_file():
            return path.resolve()
    return None


def _check_mock() -> CheckResult:
    mock = os.environ.get("SLIPSTREAM_MOCK", "") == "1"
    if mock:
        return CheckResult(
            name="mock",
            status="warn",
            message="SLIPSTREAM_MOCK=1 — mock ON; real out-of-box path expects mock OFF",
            detail={"SLIPSTREAM_MOCK": "1"},
        )
    return CheckResult(
        name="mock",
        status="ok",
        message="mock OFF (real Chrome path)",
        detail={"SLIPSTREAM_MOCK": os.environ.get("SLIPSTREAM_MOCK", "")},
    )


def _check_chrome() -> CheckResult:
    explicit = os.environ.get("SLIPSTREAM_CHROME")
    binary = find_chrome_binary(explicit)
    if not binary:
        return CheckResult(
            name="chrome",
            status="fail",
            message=(
                "No Chrome/Chromium binary found. Install google-chrome or "
                "chromium, or set SLIPSTREAM_CHROME."
            ),
            detail={"SLIPSTREAM_CHROME": explicit},
        )
    return CheckResult(
        name="chrome",
        status="ok",
        message=f"Chrome found: {binary}",
        detail={"binary": binary, "SLIPSTREAM_CHROME": explicit},
    )


def _ephemeral_loopback_port() -> int:
    """Bind :0 on loopback and return a free TCP port (race-tolerant)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


def _probe_cdp_with_chrome(binary: str, *, timeout: float = 12.0) -> CheckResult:
    """Launch ephemeral headless Chrome, probe /json/version, then stop."""
    port = _ephemeral_loopback_port()
    user_data = Path(tempfile.mkdtemp(prefix="slipstream-doctor-"))
    args = [
        binary,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data}",
        "--headless=new",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-gpu",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "about:blank",
    ]
    proc: subprocess.Popen | None = None
    version: dict[str, Any] | None = None
    last_err: str | None = None
    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + timeout
        url = f"http://127.0.0.1:{port}/json/version"
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                last_err = f"chrome exited early with code {proc.returncode}"
                break
            try:
                with urllib.request.urlopen(url, timeout=1.0) as resp:
                    if resp.status == 200:
                        version = json.load(resp)
                        break
            except Exception as e:  # noqa: BLE001 — probe loop
                last_err = str(e)
                time.sleep(0.15)
        if version is None:
            return CheckResult(
                name="cdp_probe",
                status="fail",
                message=f"CDP probe failed at {url}: {last_err}",
                detail={"port": port, "binary": binary, "error": last_err},
            )
        browser = version.get("Browser") or version.get("Product") or "unknown"
        return CheckResult(
            name="cdp_probe",
            status="ok",
            message=f"CDP ready ({browser})",
            detail={
                "port": port,
                "binary": binary,
                "browser": browser,
                "webSocketDebuggerUrl": version.get("webSocketDebuggerUrl"),
            },
        )
    except OSError as e:
        return CheckResult(
            name="cdp_probe",
            status="fail",
            message=f"Failed to launch Chrome for CDP probe: {e}",
            detail={"binary": binary, "error": str(e)},
        )
    finally:
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    proc.terminate()
                except OSError:
                    pass
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    proc.kill()
        shutil.rmtree(user_data, ignore_errors=True)


def _check_cdp_probe(chrome: CheckResult) -> CheckResult:
    if chrome.status != "ok":
        return CheckResult(
            name="cdp_probe",
            status="skip",
            message="Skipped — Chrome binary missing",
            detail={},
        )
    if os.environ.get("SLIPSTREAM_MOCK") == "1":
        return CheckResult(
            name="cdp_probe",
            status="skip",
            message="Skipped — SLIPSTREAM_MOCK=1 (no real CDP)",
            detail={},
        )
    binary = chrome.detail.get("binary")
    if not isinstance(binary, str):
        return CheckResult(
            name="cdp_probe",
            status="fail",
            message="Internal error: chrome check missing binary path",
            detail={},
        )
    return _probe_cdp_with_chrome(binary)


def _check_pool_healthz(base_url: str) -> CheckResult:
    url = f"{base_url.rstrip('/')}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=3.0) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                payload: Any = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                payload = {"raw": raw}
            if resp.status == 200:
                return CheckResult(
                    name="pool_healthz",
                    status="ok",
                    message=f"Pool healthy at {url}",
                    detail={"url": url, "status": resp.status, "body": payload},
                )
            return CheckResult(
                name="pool_healthz",
                status="fail",
                message=f"Pool healthz HTTP {resp.status}",
                detail={"url": url, "status": resp.status, "body": payload},
            )
    except urllib.error.HTTPError as e:
        # HTTPError is a URLError subclass — catch first so 4xx/5xx fail doctor
        raw = e.read().decode("utf-8", errors="replace") if e.fp is not None else ""
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"raw": raw or e.reason}
        return CheckResult(
            name="pool_healthz",
            status="fail",
            message=f"Pool healthz HTTP {e.code}",
            detail={"url": url, "status": e.code, "body": payload},
        )
    except urllib.error.URLError as e:
        return CheckResult(
            name="pool_healthz",
            status="warn",
            message=(
                f"Pool not reachable at {url} ({e.reason}). "
                "Start with: slipstream serve"
            ),
            detail={"url": url, "error": str(e.reason)},
        )
    except TimeoutError:
        return CheckResult(
            name="pool_healthz",
            status="warn",
            message=f"Pool healthz timed out at {url}. Start with: slipstream serve",
            detail={"url": url},
        )


def _check_spaces_root() -> CheckResult:
    cfg = PoolConfig.from_env()
    root = Path(cfg.spaces_root).expanduser()
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".slipstream-doctor-write"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return CheckResult(
            name="spaces_root",
            status="ok",
            message=f"Spaces root writable: {root.resolve()}",
            detail={"path": str(root.resolve())},
        )
    except OSError as e:
        return CheckResult(
            name="spaces_root",
            status="fail",
            message=f"Spaces root not writable: {root} ({e})",
            detail={"path": str(root), "error": str(e)},
        )



def _check_watch_compose() -> CheckResult:
    """Optional compose check — WARN if /watch missing (not a hard fail)."""
    path = find_watch_skill()
    if path is None:
        return CheckResult(
            name="watch_compose",
            status="warn",
            message=(
                "Optional /watch skill not found — video intents should compose "
                "upstream bradautomates/claude-video (npx skills add …). "
                "Slipstream does not vendor watch scripts."
            ),
            detail={"required": False},
        )
    return CheckResult(
        name="watch_compose",
        status="ok",
        message=f"Composed /watch skill present: {path}",
        detail={"path": str(path), "required": False},
    )


def _check_skill_path() -> CheckResult:
    path = find_skill_path()
    if path is None:
        return CheckResult(
            name="skill_path",
            status="fail",
            message=(
                "Skill file not found (skills/slipstream/SKILL.md). "
                "Set SLIPSTREAM_SKILL_PATH or install from the slipstream repo."
            ),
            detail={},
        )
    text = path.read_text(encoding="utf-8", errors="replace")[:500]
    alias_ok = "slipstream-browser" in text or "name: slipstream" in text
    return CheckResult(
        name="skill_path",
        status="ok",
        message=f"Skill present: {path}",
        detail={
            "path": str(path),
            "alias_noted": alias_ok,
            "name": "slipstream",
            "alias": "slipstream-browser",
        },
    )


def run_doctor(*, url: str | None = None) -> DoctorReport:
    base = resolve_base_url(url)
    mock = os.environ.get("SLIPSTREAM_MOCK", "") == "1"
    checks: list[CheckResult] = []
    checks.append(_check_mock())
    chrome = _check_chrome()
    checks.append(chrome)
    checks.append(_check_cdp_probe(chrome))
    checks.append(_check_pool_healthz(base))
    checks.append(_check_spaces_root())
    checks.append(_check_skill_path())
    checks.append(_check_watch_compose())
    failed = any(c.status == "fail" for c in checks)
    return DoctorReport(
        ok=not failed,
        version=__version__,
        mock=mock,
        base_url=base or DEFAULT_URL,
        checks=checks,
    )


_STATUS_MARK = {"ok": "OK", "warn": "WARN", "fail": "FAIL", "skip": "SKIP"}


def format_human(report: DoctorReport) -> str:
    lines = [
        f"slipstream doctor  v{report.version}  mock={'ON' if report.mock else 'OFF'}",
        f"pool URL: {report.base_url}",
        "",
    ]
    for c in report.checks:
        mark = _STATUS_MARK.get(c.status, c.status.upper())
        lines.append(f"  [{mark:4}] {c.name}: {c.message}")
    lines.append("")
    if report.ok:
        lines.append("Result: PASS (no failures; warnings/skips allowed)")
    else:
        lines.append("Result: FAIL (one or more checks failed)")
    return "\n".join(lines) + "\n"


def cmd_doctor(*, url: str | None = None, as_json: bool = False) -> int:
    report = run_doctor(url=url)
    if as_json:
        sys.stdout.write(json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(format_human(report))
    return 0 if report.ok else 1
