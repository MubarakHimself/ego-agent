"""Shared fixtures — always mock Chrome unless @pytest.mark.live."""

from __future__ import annotations

import pytest

from slipstream.config import PoolConfig
from slipstream.pool import BrowserPool


@pytest.fixture
def mock_config(tmp_path):
    return PoolConfig(
        K=5,
        W=1,
        idle_ttl_seconds=300,
        spaces_root=tmp_path / "spaces",
        cdp_base_port=19222,
        mock=True,
        headless=True,
    )


@pytest.fixture
def pool(mock_config):
    p = BrowserPool(mock_config)
    yield p
    p.shutdown()
