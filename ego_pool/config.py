"""Pool configuration — locked MVP defaults from architecture."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PoolConfig:
    """Hard-capped browser pool settings.

    K=5 max live Chromium trees; W=1 warm idle slots; soft-evict ~5 min.
    """

    K: int = 5
    W: int = 1
    idle_ttl_seconds: int = 300
    lease_hard_ttl_seconds: int = 1800
    spaces_root: Path = field(default_factory=lambda: Path("./data/spaces"))
    cdp_base_port: int = 9222
    host: str = "127.0.0.1"
    port: int = 8755
    headless: bool = True
    mock: bool = field(default_factory=lambda: os.environ.get("EGO_POOL_MOCK", "") == "1")
    chrome_binary: str | None = None  # auto-detect if None

    @staticmethod
    def normalize_space_id(space_id: str) -> str:
        """Validate space_id; raise ValueError if unsafe. Distinct ids stay distinct.

        Rejects path separators, '..', NULs, and empty/dot names — never rewrites
        characters into '_' (that would collapse distinct ids).
        """
        if not isinstance(space_id, str):
            raise ValueError("space_id must be a string")
        raw = space_id.strip()
        if not raw:
            raise ValueError("space_id must be non-empty")
        if "\0" in raw:
            raise ValueError(f"unsafe space_id: {space_id!r}")
        if "/" in raw or "\\" in raw:
            raise ValueError(f"unsafe space_id: {space_id!r}")
        if raw in (".", "..") or ".." in raw:
            raise ValueError(f"unsafe space_id: {space_id!r}")
        if Path(raw).name != raw:
            raise ValueError(f"unsafe space_id: {space_id!r}")
        return raw

    def space_path(self, space_id: str) -> Path:
        """Return the Chromium user-data-dir for a Space: {spaces_root}/{space_id}/."""
        safe = self.normalize_space_id(space_id)
        return self.spaces_root / safe

    @classmethod
    def from_env(cls) -> PoolConfig:
        cfg = cls()
        if os.environ.get("EGO_POOL_MOCK") == "1":
            cfg.mock = True
        if os.environ.get("EGO_POOL_HEADLESS", "1") == "0":
            cfg.headless = False
        if bin_path := os.environ.get("EGO_POOL_CHROME"):
            cfg.chrome_binary = bin_path
        if root := os.environ.get("EGO_POOL_SPACES_ROOT"):
            cfg.spaces_root = Path(root)
        if k := os.environ.get("EGO_POOL_K"):
            cfg.K = int(k)
        if w := os.environ.get("EGO_POOL_W"):
            cfg.W = int(w)
        if port := os.environ.get("EGO_POOL_PORT"):
            cfg.port = int(port)
        return cfg
