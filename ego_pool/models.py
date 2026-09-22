"""Slot / lease data models."""

from __future__ import annotations

import enum
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


class SlotStatus(str, enum.Enum):
    FREE_COLD = "free_cold"
    FREE_WARM = "free_warm"
    STARTING = "starting"
    LEASED = "leased"
    RELEASING = "releasing"


@dataclass
class SlotState:
    """One pool slot = one Chromium process tree + optional Space binding."""

    slot_id: int
    status: SlotStatus = SlotStatus.FREE_COLD
    chromium_pid: int | None = None
    cdp_port: int | None = None
    cdp_http_url: str | None = None
    cdp_ws_url: str | None = None
    space_id: str | None = None
    lease_id: str | None = None
    agent_id: str | None = None
    last_heartbeat: float | None = None
    leased_at: float | None = None
    rss_bytes: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot_id": self.slot_id,
            "status": self.status.value,
            "chromium_pid": self.chromium_pid,
            "cdp_port": self.cdp_port,
            "cdp_http_url": self.cdp_http_url,
            "cdp_ws_url": self.cdp_ws_url,
            "space_id": self.space_id,
            "lease_id": self.lease_id,
            "agent_id": self.agent_id,
            "last_heartbeat": self.last_heartbeat,
            "leased_at": self.leased_at,
            "rss_bytes": self.rss_bytes,
        }


@dataclass
class Lease:
    """Active lease handle returned to an agent."""

    lease_id: str
    slot_id: int
    agent_id: str
    space_id: str
    status: str = "leased"
    cdp_http_url: str | None = None
    cdp_ws_url: str | None = None
    created_at: float = field(default_factory=time.time)
    expires_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "lease_id": self.lease_id,
            "slot_id": self.slot_id,
            "agent_id": self.agent_id,
            "space_id": self.space_id,
            "status": self.status,
            "cdp_http_url": self.cdp_http_url,
            "cdp_ws_url": self.cdp_ws_url,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }


def new_lease_id() -> str:
    return str(uuid.uuid4())
