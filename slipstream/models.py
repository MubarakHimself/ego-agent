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
        from slipstream.actions import expose_raw_cdp

        out: dict[str, Any] = {
            "slot_id": self.slot_id,
            "status": self.status.value,
            "space_id": self.space_id,
            "lease_id": self.lease_id,
            "agent_id": self.agent_id,
            "last_heartbeat": self.last_heartbeat,
            "leased_at": self.leased_at,
            "rss_bytes": self.rss_bytes,
        }
        # ADV-PL-001-BYPASS-RAW-CDP-PORT: cdp_port / chromium_pid are operator tip —
        # same escape hatch as cdp_* urls (do not leak reconstruct path on public JSON).
        if expose_raw_cdp():
            out["chromium_pid"] = self.chromium_pid
            out["cdp_port"] = self.cdp_port
            out["cdp_http_url"] = self.cdp_http_url
            out["cdp_ws_url"] = self.cdp_ws_url
        return out


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
    # Lease-level override of Space user_metadata (string leaves only).
    user_metadata_override: dict[str, Any] = field(default_factory=dict)
    # Effective tags = deep_merge(space_meta, override); set by pool on attach.
    user_metadata: dict[str, Any] = field(default_factory=dict)
    # None = inherit Space/config; explicit list (incl. empty) = lease override.
    allowed_domains_override: list[str] | None = None
    # Effective top-frame allowlist (empty = unrestricted); set by pool on attach.
    allowed_domains: list[str] = field(default_factory=list)
    # Space signed-in badge (metadata only; cookies stay in user-data-dir).
    signed_in: bool = False
    signed_in_host: str | None = None
    # Browserbase-style keepAlive: survive driver disconnect / soft-idle.
    keep_alive: bool = False

    def to_dict(self) -> dict[str, Any]:
        from slipstream.actions import expose_raw_cdp

        out: dict[str, Any] = {
            "lease_id": self.lease_id,
            "slot_id": self.slot_id,
            "agent_id": self.agent_id,
            "space_id": self.space_id,
            "status": self.status,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "user_metadata": dict(self.user_metadata),
            "allowed_domains": list(self.allowed_domains),
            "signed_in": bool(self.signed_in),
            "keep_alive": bool(self.keep_alive),
        }
        if expose_raw_cdp():
            out["cdp_http_url"] = self.cdp_http_url
            out["cdp_ws_url"] = self.cdp_ws_url
        if self.signed_in and self.signed_in_host:
            out["signed_in_host"] = self.signed_in_host
        return out


def new_lease_id() -> str:
    return str(uuid.uuid4())
