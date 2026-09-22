"""Pool configuration — locked MVP defaults from architecture."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def keep_alive_default() -> bool:
    """Env default for lease keep_alive (SLIPSTREAM_KEEPALIVE; default false)."""
    return os.environ.get("SLIPSTREAM_KEEPALIVE", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


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
    vault_root: Path = field(default_factory=lambda: Path("./data/vault"))
    # Lease session artifacts (downloads/uploads) — outside spaces + vault.
    artifacts_root: Path = field(default_factory=lambda: Path("./data/artifacts"))
    cdp_base_port: int = 9222
    host: str = "127.0.0.1"
    port: int = 8755
    headless: bool = True
    mock: bool = field(default_factory=lambda: os.environ.get("SLIPSTREAM_MOCK", "") == "1")
    chrome_binary: str | None = None  # auto-detect if None
    # Top-frame nav allowlist (empty = unrestricted). Env: SLIPSTREAM_ALLOWED_DOMAINS.
    allowed_domains: list[str] = field(default_factory=list)

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

    def ensure_vault_outside_spaces(self) -> None:
        """Fail closed if vault_root is inside (or equal to) spaces_root.

        ADV-002: resolve both paths and require vault is *not* a relative_to
        child of spaces (vault co-located under user-data-dir is scrapeable).
        """
        vault = self.vault_root.expanduser().resolve()
        spaces = self.spaces_root.expanduser().resolve()
        if vault == spaces:
            raise ValueError(
                f"vault_root must be outside spaces_root (got equal paths: {vault})"
            )
        try:
            vault.relative_to(spaces)
        except ValueError:
            pass  # vault is not under spaces — OK
        else:
            raise ValueError(
                f"vault_root must be outside spaces_root "
                f"(vault={vault} is under spaces={spaces})"
            )
        self.ensure_artifacts_outside()

    def ensure_artifacts_outside(self) -> None:
        """Fail closed if artifacts_root sits inside spaces or vault."""
        from slipstream.downloads import ensure_artifacts_outside

        ensure_artifacts_outside(
            self.artifacts_root,
            spaces_root=self.spaces_root,
            vault_root=self.vault_root,
        )


    def _apply_storage_roots_from_env(self) -> None:
        """Apply SLIPSTREAM_* root path overrides from the environment."""
        if root := os.environ.get("SLIPSTREAM_SPACES_ROOT"):
            self.spaces_root = Path(root)
        vault = os.environ.get("SLIPSTREAM_VAULT_ROOT") or os.environ.get("VAULT_ROOT")
        if vault:
            self.vault_root = Path(vault)
        if art := os.environ.get("SLIPSTREAM_ARTIFACTS_ROOT"):
            self.artifacts_root = Path(art)

    @classmethod
    def from_env(cls) -> PoolConfig:
        cfg = cls()
        if os.environ.get("SLIPSTREAM_MOCK") == "1":
            cfg.mock = True
        if os.environ.get("SLIPSTREAM_HEADLESS", "1") == "0":
            cfg.headless = False
        if bin_path := os.environ.get("SLIPSTREAM_CHROME"):
            cfg.chrome_binary = bin_path
        # Vault/artifacts MUST stay outside Space user-data-dir
        cfg._apply_storage_roots_from_env()
        if k := os.environ.get("SLIPSTREAM_K"):
            cfg.K = int(k)
        if w := os.environ.get("SLIPSTREAM_W"):
            cfg.W = int(w)
        if port := os.environ.get("SLIPSTREAM_PORT"):
            cfg.port = int(port)
        from slipstream.domains import allowed_domains_from_env

        cfg.allowed_domains = allowed_domains_from_env()
        cfg.ensure_vault_outside_spaces()
        return cfg
