"""Ego Browser Pool Manager — shared Chromium CDP lease service (MVP scaffold).

Architecture lock: docs/ARCHITECTURE.md, docs/POOL_API.md
Hard K=5 live slots, W=1 warm, Space = user-data-dir, Linux-first.
No Electron / Jev / Laya / proprietary ego binary code.
"""

__version__ = "0.1.0"

from slipstream.config import PoolConfig
from slipstream.pool import BrowserPool

__all__ = ["PoolConfig", "BrowserPool", "__version__"]
