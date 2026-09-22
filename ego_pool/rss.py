"""RSS sampling hook for Chromium process trees (Linux /proc).

Used later for K-tuning. Stub-safe: returns None when /proc is unavailable
(non-Linux, missing pid, permission denied).
"""

from __future__ import annotations

import os
from pathlib import Path


def sample_rss_kb(pid: int) -> int | None:
    """Return RSS of a single process in KiB, or None if unavailable."""
    try:
        status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
        return None
    for line in status.splitlines():
        if line.startswith("VmRSS:"):
            parts = line.split()
            if len(parts) >= 2:
                try:
                    return int(parts[1])
                except ValueError:
                    return None
    return None


def _child_pids(pid: int) -> list[int]:
    """List direct children via /proc/{pid}/task/*/children (best-effort)."""
    children: list[int] = []
    task_dir = Path(f"/proc/{pid}/task")
    if not task_dir.is_dir():
        return children
    try:
        for task in task_dir.iterdir():
            children_file = task / "children"
            try:
                text = children_file.read_text(encoding="utf-8").strip()
            except (FileNotFoundError, PermissionError, OSError):
                continue
            if text:
                for tok in text.split():
                    try:
                        children.append(int(tok))
                    except ValueError:
                        pass
    except (FileNotFoundError, PermissionError, OSError):
        pass
    return children


def sample_tree_rss(pid: int | None) -> int | None:
    """Sum RSS (bytes) across a process tree rooted at ``pid``.

    Walks children breadth-first via /proc. Returns None if ``pid`` is None
    or the root process cannot be sampled (non-Linux / gone).
    """
    if pid is None:
        return None
    root_kb = sample_rss_kb(pid)
    if root_kb is None:
        return None

    total_kb = 0
    seen: set[int] = set()
    queue = [pid]
    while queue:
        current = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)
        kb = sample_rss_kb(current)
        if kb is not None:
            total_kb += kb
        for child in _child_pids(current):
            if child not in seen:
                queue.append(child)

    return total_kb * 1024  # bytes
