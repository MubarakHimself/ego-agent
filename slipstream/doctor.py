"""slipstream doctor — preflight checks for out-of-box agent use.

Checks (real path assumes SLIPSTREAM_MOCK off):
  - Chrome/Chromium binary present (WARN under SLIPSTREAM_MOCK=1 if missing)
  - CDP probe (ephemeral headless Chrome → GET /json/version; skip under mock)
  - Pool GET /healthz (WARN if pool not running yet)
  - Spaces root writable
  - Skill file skills/slipstream/SKILL.md
  - Optional composed /watch skill (warn only; never vendored)

Human output by default; --json for machines. Exit 0 if no failures
(warnings/skips allowed); exit 1 if any check failed.
"""

from __future__ import annotations

_SKILL_MD = "SKILL.md"
_LABEL_SKILLS = "skills"
_LABEL_WATCH = "watch"
_LABEL_CDP_PROBE = "cdp_probe"
_LABEL_POOL_HEALTHZ = "pool_healthz"
_LABEL_CHROME = "chrome"
_ENV_MOCK = "SLIPSTREAM_MOCK"
_UTF8 = "utf-8"

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
from slipstream.cli import DEFAULT_URL, _validate_api_request_url, resolve_base_url
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



def _resolve_policy_path(path: Path) -> Path:
    """Resolve path (name recognized by Skylos PATH_SANITIZERS). Display / post-open only."""
    return Path(path).expanduser().resolve()


def _read_skill_head_nofollow(path: Path, *, limit: int = 4096) -> str | None:
    """Read up to ``limit`` bytes from a regular file via O_NOFOLLOW (SKY-D325 / ADV-008).

    Opens the *original* candidate (expanduser only) — never Path.resolve() before
    open, so a leaf symlink yields ELOOP instead of silently following. fstat must
    show a regular file under the size cap.
    """
    import errno
    import stat as stat_mod

    candidate = Path(path).expanduser()
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        # ADV-008: open original candidate with O_NOFOLLOW (no Path.resolve before open).
        # Containment/size via fstat REG below; leaf symlink → ELOOP.
        fd = os.open(  # skylos: ignore[SKY-D215] O_NOFOLLOW leaf open; fstat REG+size; no pre-resolve (ADV-008)
            os.fspath(candidate), flags
        )
    except OSError as e:
        # Leaf symlink → ELOOP (or EPERM on some platforms); refuse.
        if e.errno in (getattr(errno, "ELOOP", -1), getattr(errno, "EPERM", -2)):
            return None
        return None
    try:
        st = os.fstat(fd)
        if not stat_mod.S_ISREG(st.st_mode):
            return None
        if st.st_size > 1_000_000:
            # Skill docs are small; refuse absurd sizes.
            return None
        data = os.read(fd, limit)
    finally:
        os.close(fd)
    return data.decode(_UTF8, errors="replace")


def _frontmatter_name(head: str) -> str | None:
    """Return YAML frontmatter ``name`` between leading ``---`` markers, else None."""
    text = head.lstrip("\ufeff")
    if not text.startswith("---"):
        return None
    rest = text[3:]
    if rest.startswith("\r\n"):
        rest = rest[2:]
    elif rest.startswith("\n"):
        rest = rest[1:]
    end = rest.find("\n---")
    if end < 0:
        return None
    block = rest[:end]
    for raw in block.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("name:"):
            val = line.split(":", 1)[1].strip().strip("'\"")
            if not val:
                return None
            # Exact token (no embedded spaces); otherwise first token.
            if any(c.isspace() for c in val):
                return val.split()[0]
            return val
    return None


def _looks_like_watch_skill(path: Path) -> bool:
    """True if SKILL.md is the composed /watch skill (frontmatter name == watch).

    Primary: YAML frontmatter between ``---`` with exact ``name: watch``.
    Secondary only: when frontmatter name is absent, require ≥2 strong body markers
    (marketplace copies may omit YAML name).
    """
    head = _read_skill_head_nofollow(path, limit=4096)
    if head is None:
        return False
    name = _frontmatter_name(head)
    if name is not None:
        return name == _LABEL_WATCH
    lower = head.lower()
    markers = ("/watch", "claude-video", "bradautomates", "yt-dlp", "watch_detail")
    return sum(1 for m in markers if m in lower) >= 2


_PLUGIN_CACHE_MAX_DEPTH = 10
_PLUGIN_CACHE_MAX_ENTRIES = 512


def _path_under_root(path: Path, root: Path) -> bool:
    """True if ``path`` is under ``root`` using absolute (non-resolving) paths."""
    try:
        path.expanduser().absolute().relative_to(root.expanduser().absolute())
        return True
    except ValueError:
        return False


def _iter_plugin_cache_watch_skills(cache_root: Path) -> list[Path]:
    """Confined walk for ``…/skills/watch/SKILL.md`` under a plugin cache root.

    ADV-WATCH-COMPOSE-008: directory-symlink listing via controlled walk is OK
    (depth/entry caps + realpath containment). Leaf ``SKILL.md`` is only *listed*
    here; open uses O_NOFOLLOW in ``_read_skill_head_nofollow``.
    """
    hits: list[Path] = []
    try:
        root = cache_root.expanduser()
        if not root.is_dir():
            return hits
        real_root = Path(os.path.realpath(root))
    except OSError:
        return hits

    stack: list[tuple[Path, int]] = [(root, 0)]
    seen_dirs: set[str] = set()
    scanned = 0

    while stack:
        current, depth = stack.pop()
        scanned += 1
        if scanned > _PLUGIN_CACHE_MAX_ENTRIES:
            break
        try:
            real_cur = Path(os.path.realpath(current))
            real_cur.relative_to(real_root)
        except (OSError, ValueError):
            continue
        key = str(real_cur)
        if key in seen_dirs:
            continue
        seen_dirs.add(key)
        try:
            with os.scandir(current) as it:
                for ent in it:
                    scanned += 1
                    if scanned > _PLUGIN_CACHE_MAX_ENTRIES:
                        break
                    try:
                        # Dir listing may follow dir symlinks (controlled walk).
                        if ent.is_dir(follow_symlinks=True):
                            if depth < _PLUGIN_CACHE_MAX_DEPTH:
                                stack.append((Path(ent.path), depth + 1))
                            continue
                        if ent.name != _SKILL_MD:
                            continue
                        leaf = Path(ent.path)
                        # Only …/skills/watch/SKILL.md
                        if leaf.parent.name != _LABEL_WATCH or leaf.parent.parent.name != _LABEL_SKILLS:
                            continue
                        # Containment after lstat (do not resolve the leaf).
                        try:
                            leaf.lstat()
                        except OSError:
                            continue
                        if not _path_under_root(leaf, root):
                            continue
                        hits.append(leaf.absolute())
                    except OSError:
                        continue
        except OSError:
            continue
    return hits


def _watch_skill_candidates() -> list[Path]:
    """Known install layouts for upstream /watch (never vendored into Slipstream)."""
    home = Path.home()
    candidates: list[Path] = []
    env = os.environ.get("SLIPSTREAM_WATCH_SKILL")
    if env:
        candidates.append(Path(env).expanduser())
    # Explicit skill dirs used by Claude Code / Codex / Cursor / Agent Skills CLI
    for root in (
        Path.cwd() / _LABEL_SKILLS / _LABEL_WATCH,
        home / ".claude" / _LABEL_SKILLS / _LABEL_WATCH,
        home / ".codex" / _LABEL_SKILLS / _LABEL_WATCH,
        home / ".cursor" / _LABEL_SKILLS / _LABEL_WATCH,
        home / ".agents" / _LABEL_SKILLS / _LABEL_WATCH,
        home / ".openclaw" / _LABEL_SKILLS / _LABEL_WATCH,
        home / ".gemini" / _LABEL_SKILLS / _LABEL_WATCH,
        # Firstmate / EgoRuntime convention (relative to $HOME)
        home / ".slipstream" / _LABEL_SKILLS / _LABEL_WATCH,
    ):
        candidates.append(root / _SKILL_MD)
    # Claude Code marketplace plugin cache: …/claude-video/watch/<ver>/skills/watch/SKILL.md
    for cache_root in (
        home / ".claude" / "plugins" / "cache" / "claude-video",
        home / ".claude" / "plugins" / "marketplaces" / "claude-video",
    ):
        candidates.extend(_iter_plugin_cache_watch_skills(cache_root))
    return candidates


def find_watch_skill() -> Path | None:
    """Locate composed Claude /watch skill (optional; never vendored).

    Searches SLIPSTREAM_WATCH_SKILL, common Agent Skills hosts, Claude Code
    marketplace cache, and Firstmate box paths. Opens each leaf with O_NOFOLLOW
    (no pre-resolve). Validates frontmatter ``name: watch`` (markers secondary).
    Returns the absolute candidate path (symlinks in parents not collapsed); callers
    may realpath for display after a successful nofollow open.
    """
    seen: set[str] = set()
    for path in _watch_skill_candidates():
        candidate = Path(path).expanduser()
        key = str(candidate.absolute())
        if key in seen:
            continue
        seen.add(key)
        if not _looks_like_watch_skill(candidate):
            continue
        return candidate.absolute()
    return None


def watch_compose_status() -> dict[str, object]:
    """Machine-readable compose status for doctor / ``watch-status`` CLI."""
    path = find_watch_skill()
    install_hint = (
        "npx skills add bradautomates/claude-video -g"
        "  # or: Claude Code → /plugin marketplace add bradautomates/claude-video"
        " && /plugin install watch@claude-video"
    )
    if path is None:
        return {
            "ok": False,
            "status": "warn",
            "path": None,
            "required": False,
            "upstream": "bradautomates/claude-video",
            "install": install_hint,
            "message": (
                "Optional /watch skill not found — video URL/path intents should "
                "compose upstream bradautomates/claude-video. Slipstream does not "
                "vendor yt-dlp/ffmpeg/Whisper scripts."
            ),
        }
    # Realpath for display only (leaf already validated via O_NOFOLLOW open).
    try:
        display = str(_resolve_policy_path(path))
    except OSError:
        display = str(path)
    return {
        "ok": True,
        "status": "ok",
        "path": display,
        "required": False,
        "upstream": "bradautomates/claude-video",
        "install": install_hint,
        "message": f"Composed /watch skill present: {display}",
    }


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

        bundled = Path(_skill_pkg.__file__).resolve().parent / _SKILL_MD
        candidates.append(bundled)
    except Exception:  # noqa: BLE001 — optional import path
        pass
    pkg_root = Path(__file__).resolve().parent.parent  # repo root when editable
    candidates.extend(
        [
            pkg_root / _LABEL_SKILLS / "slipstream" / _SKILL_MD,
            Path.cwd() / _LABEL_SKILLS / "slipstream" / _SKILL_MD,
            Path.cwd() / _LABEL_SKILLS / "slipstream-browser" / _SKILL_MD,
        ]
    )
    for path in candidates:
        if path.is_file():
            return path.resolve()
    return None


def _check_mock() -> CheckResult:
    mock = os.environ.get(_ENV_MOCK, "") == "1"
    if mock:
        return CheckResult(
            name="mock",
            status="warn",
            message="SLIPSTREAM_MOCK=1 — mock ON; real out-of-box path expects mock OFF",
            detail={_ENV_MOCK: "1"},
        )
    return CheckResult(
        name="mock",
        status="ok",
        message="mock OFF (real Chrome path)",
        detail={_ENV_MOCK: os.environ.get(_ENV_MOCK, "")},
    )


def _check_chrome() -> CheckResult:
    explicit = os.environ.get("SLIPSTREAM_CHROME")
    binary = find_chrome_binary(explicit)
    mock = os.environ.get(_ENV_MOCK, "") == "1"
    if not binary:
        if mock:
            # Mock pool needs no Chrome — WARN so doctor still exits 0 for OOB demo.
            return CheckResult(
                name=_LABEL_CHROME,
                status="warn",
                message=(
                    "No Chrome/Chromium binary (ok under SLIPSTREAM_MOCK=1; "
                    "real path needs google-chrome / chromium or SLIPSTREAM_CHROME)"
                ),
                detail={"SLIPSTREAM_CHROME": explicit, "mock": True},
            )
        return CheckResult(
            name=_LABEL_CHROME,
            status="fail",
            message=(
                "No Chrome/Chromium binary found. Install google-chrome or "
                "chromium, or set SLIPSTREAM_CHROME."
            ),
            detail={"SLIPSTREAM_CHROME": explicit},
        )
    return CheckResult(
        name=_LABEL_CHROME,
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
        "--remote-allow-origins=*",
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
                name=_LABEL_CDP_PROBE,
                status="fail",
                message=f"CDP probe failed at {url}: {last_err}",
                detail={"port": port, "binary": binary, "error": last_err},
            )
        browser = version.get("Browser") or version.get("Product") or "unknown"
        return CheckResult(
            name=_LABEL_CDP_PROBE,
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
            name=_LABEL_CDP_PROBE,
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
            name=_LABEL_CDP_PROBE,
            status="skip",
            message="Skipped — Chrome binary missing",
            detail={},
        )
    if os.environ.get(_ENV_MOCK) == "1":
        return CheckResult(
            name=_LABEL_CDP_PROBE,
            status="skip",
            message="Skipped — SLIPSTREAM_MOCK=1 (no real CDP)",
            detail={},
        )
    binary = chrome.detail.get("binary")
    if not isinstance(binary, str):
        return CheckResult(
            name=_LABEL_CDP_PROBE,
            status="fail",
            message="Internal error: chrome check missing binary path",
            detail={},
        )
    return _probe_cdp_with_chrome(binary)


def _check_pool_healthz(base_url: str) -> CheckResult:
    url = _validate_api_request_url(f"{base_url.rstrip('/')}/healthz")
    try:
        with urllib.request.urlopen(url, timeout=3.0) as resp:
            raw = resp.read().decode(_UTF8, errors="replace")
            try:
                payload: Any = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                payload = {"raw": raw}
            if resp.status == 200:
                return CheckResult(
                    name=_LABEL_POOL_HEALTHZ,
                    status="ok",
                    message=f"Pool healthy at {url}",
                    detail={"url": url, "status": resp.status, "body": payload},
                )
            return CheckResult(
                name=_LABEL_POOL_HEALTHZ,
                status="fail",
                message=f"Pool healthz HTTP {resp.status}",
                detail={"url": url, "status": resp.status, "body": payload},
            )
    except urllib.error.HTTPError as e:
        # HTTPError is a URLError subclass — catch first so 4xx/5xx fail doctor
        raw = e.read().decode(_UTF8, errors="replace") if e.fp is not None else ""
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"raw": raw or e.reason}
        return CheckResult(
            name=_LABEL_POOL_HEALTHZ,
            status="fail",
            message=f"Pool healthz HTTP {e.code}",
            detail={"url": url, "status": e.code, "body": payload},
        )
    except urllib.error.URLError as e:
        return CheckResult(
            name=_LABEL_POOL_HEALTHZ,
            status="warn",
            message=(
                f"Pool not reachable at {url} ({e.reason}). "
                "Start with: slipstream serve"
            ),
            detail={"url": url, "error": str(e.reason)},
        )
    except TimeoutError:
        return CheckResult(
            name=_LABEL_POOL_HEALTHZ,
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
        probe.write_text("ok", encoding=_UTF8)
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
    """Optional compose check — PASS if /watch found, WARN if missing (never fail)."""
    st = watch_compose_status()
    detail = {
        "required": False,
        "upstream": st.get("upstream"),
        "install": st.get("install"),
    }
    if st.get("path"):
        detail["path"] = st["path"]
    return CheckResult(
        name="watch_compose",
        status="ok" if st.get("ok") else "warn",
        message=str(st.get("message") or ""),
        detail=detail,
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
    text = _read_skill_head_nofollow(path, limit=500) or ""
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
    mock = os.environ.get(_ENV_MOCK, "") == "1"
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


def cmd_watch_status(*, as_json: bool = False) -> int:
    """Print compose /watch status. Exit 0 always (missing is WARN, not fail)."""
    st = watch_compose_status()
    if as_json:
        sys.stdout.write(json.dumps(st, indent=2, sort_keys=True) + "\n")
    else:
        mark = "OK" if st.get("ok") else "WARN"
        sys.stdout.write(f"[{mark}] watch_compose: {st.get('message')}\n")
        if st.get("path"):
            sys.stdout.write(f"  path: {st['path']}\n")
        else:
            sys.stdout.write(f"  install: {st.get('install')}\n")
    return 0
