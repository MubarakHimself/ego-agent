"""Browser Pool Manager — lease / heartbeat / release with hard K cap."""

from __future__ import annotations

import threading
import time
from typing import Any

from ego_pool.config import PoolConfig
from ego_pool.launcher import ChromiumLauncher, LaunchHandle
from ego_pool.models import Lease, SlotState, SlotStatus, new_lease_id
from ego_pool.rss import sample_tree_rss


class PoolFullError(Exception):
    """Raised when live slots == K and no free slot can be allocated."""


class LeaseNotFoundError(Exception):
    pass


class BrowserPool:
    """Owns ≤K Chromium process trees; agents lease slots, never own PIDs."""

    def __init__(self, config: PoolConfig | None = None, launcher: ChromiumLauncher | None = None):
        self.config = config or PoolConfig()
        self.launcher = launcher or ChromiumLauncher(self.config)
        self._lock = threading.RLock()
        self._slots: list[SlotState] = [
            SlotState(slot_id=i, status=SlotStatus.FREE_COLD) for i in range(self.config.K)
        ]
        self._handles: dict[int, LaunchHandle] = {}
        self._leases: dict[str, Lease] = {}
        # Ensure spaces root exists
        self.config.spaces_root.mkdir(parents=True, exist_ok=True)

    # --- introspection -------------------------------------------------

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._refresh_rss()
            live = sum(
                1
                for s in self._slots
                if s.status in (SlotStatus.LEASED, SlotStatus.STARTING, SlotStatus.FREE_WARM)
            )
            warm = sum(1 for s in self._slots if s.status == SlotStatus.FREE_WARM)
            leased = sum(1 for s in self._slots if s.status == SlotStatus.LEASED)
            return {
                "K": self.config.K,
                "W": self.config.W,
                "idle_ttl_seconds": self.config.idle_ttl_seconds,
                "live": live,
                "warm": warm,
                "leased": leased,
                "mock": self.config.mock,
                "spaces_root": str(self.config.spaces_root),
                "cdp_base_port": self.config.cdp_base_port,
                "chrome_binary": self.launcher.binary,
                "slots": [s.to_dict() for s in self._slots],
            }

    def _refresh_rss(self) -> None:
        for slot in self._slots:
            if slot.chromium_pid is not None and not self.config.mock:
                slot.rss_bytes = sample_tree_rss(slot.chromium_pid)
            elif self.config.mock and slot.chromium_pid is not None:
                slot.rss_bytes = None  # stub: unavailable under mock

    def _cdp_port_for(self, slot_id: int) -> int:
        return self.config.cdp_base_port + slot_id

    def _find_slot_by_lease(self, lease_id: str) -> SlotState | None:
        for s in self._slots:
            if s.lease_id == lease_id:
                return s
        return None

    def _count_warm(self) -> int:
        return sum(1 for s in self._slots if s.status == SlotStatus.FREE_WARM)

    def _count_live_processes(self) -> int:
        return sum(
            1
            for s in self._slots
            if s.status in (SlotStatus.LEASED, SlotStatus.STARTING, SlotStatus.FREE_WARM)
            and s.chromium_pid is not None
        )

    # --- lease API -----------------------------------------------------

    def lease(
        self,
        agent_id: str,
        space_id: str,
        mode: str = "isolated",
        *,
        ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        if mode != "isolated":
            raise ValueError("MVP only supports mode=isolated (attach_user_chrome is stretch)")

        with self._lock:
            # Idempotent: same agent+space already leased → return existing
            for lid, existing in self._leases.items():
                if existing.agent_id == agent_id and existing.space_id == space_id:
                    slot = self._find_slot_by_lease(lid)
                    if slot and slot.status == SlotStatus.LEASED:
                        slot.last_heartbeat = time.time()
                        return existing.to_dict()

            # Prefer FREE_WARM
            warm = next((s for s in self._slots if s.status == SlotStatus.FREE_WARM), None)
            cold = next((s for s in self._slots if s.status == SlotStatus.FREE_COLD), None)

            if warm is not None:
                slot = warm
                # Re-bind Space: stop warm browser and relaunch with new profile
                # (MVP: warm emptied slots have no Space; relaunch with requested Space)
                self._stop_slot(slot)
                return self._start_and_lease(slot, agent_id, space_id, ttl_seconds)

            if cold is not None:
                # Cap: count of live process trees must stay ≤ K (cold start adds one)
                if self._count_live_processes() >= self.config.K:
                    raise PoolFullError(f"pool at hard K={self.config.K}")
                return self._start_and_lease(cold, agent_id, space_id, ttl_seconds)

            raise PoolFullError(f"pool at hard K={self.config.K}; all slots leased")

    def _start_and_lease(
        self,
        slot: SlotState,
        agent_id: str,
        space_id: str,
        ttl_seconds: int | None,
    ) -> dict[str, Any]:
        slot.status = SlotStatus.STARTING
        port = self._cdp_port_for(slot.slot_id)
        handle = self.launcher.launch(space_id, port)
        self._handles[slot.slot_id] = handle

        now = time.time()
        lease_id = new_lease_id()
        hard_ttl = ttl_seconds if ttl_seconds is not None else self.config.lease_hard_ttl_seconds
        lease = Lease(
            lease_id=lease_id,
            slot_id=slot.slot_id,
            agent_id=agent_id,
            space_id=space_id,
            status="leased",
            cdp_http_url=handle.cdp_http_url,
            cdp_ws_url=handle.cdp_ws_url,
            created_at=now,
            expires_at=now + hard_ttl,
        )
        self._leases[lease_id] = lease

        slot.status = SlotStatus.LEASED
        slot.chromium_pid = handle.pid
        slot.cdp_port = handle.cdp_port
        slot.cdp_http_url = handle.cdp_http_url
        slot.cdp_ws_url = handle.cdp_ws_url
        slot.space_id = space_id
        slot.lease_id = lease_id
        slot.agent_id = agent_id
        slot.last_heartbeat = now
        slot.leased_at = now
        if not self.config.mock:
            slot.rss_bytes = sample_tree_rss(handle.pid)

        return lease.to_dict()

    def heartbeat(self, lease_id: str) -> dict[str, Any]:
        with self._lock:
            lease = self._leases.get(lease_id)
            if not lease:
                raise LeaseNotFoundError(lease_id)
            slot = self._find_slot_by_lease(lease_id)
            if not slot or slot.status != SlotStatus.LEASED:
                raise LeaseNotFoundError(lease_id)
            now = time.time()
            slot.last_heartbeat = now
            # Extend soft window; hard expiry unchanged unless renewed policy later
            return {
                "lease_id": lease_id,
                "ok": True,
                "last_heartbeat": now,
                "idle_ttl_seconds": self.config.idle_ttl_seconds,
            }

    def release(self, lease_id: str, reason: str = "client_release") -> dict[str, Any]:
        with self._lock:
            lease = self._leases.get(lease_id)
            if not lease:
                raise LeaseNotFoundError(lease_id)
            slot = self._find_slot_by_lease(lease_id)
            if not slot:
                raise LeaseNotFoundError(lease_id)

            slot.status = SlotStatus.RELEASING
            keep_warm = self._count_warm() < self.config.W

            if keep_warm:
                # Detach lease; keep process as FREE_WARM (profile stays on disk)
                self._clear_lease_fields(slot)
                slot.status = SlotStatus.FREE_WARM
                # Warm slot has no active Space binding for next lease
                slot.space_id = None
            else:
                self._stop_slot(slot)
                slot.status = SlotStatus.FREE_COLD

            del self._leases[lease_id]
            return {"lease_id": lease_id, "released": True, "reason": reason, "kept_warm": keep_warm}

    def _clear_lease_fields(self, slot: SlotState) -> None:
        slot.lease_id = None
        slot.agent_id = None
        slot.last_heartbeat = None
        slot.leased_at = None

    def _stop_slot(self, slot: SlotState) -> None:
        handle = self._handles.pop(slot.slot_id, None)
        self.launcher.stop(handle)
        slot.chromium_pid = None
        slot.cdp_port = None
        slot.cdp_http_url = None
        slot.cdp_ws_url = None
        slot.rss_bytes = None
        self._clear_lease_fields(slot)
        slot.space_id = None

    def evict_idle(self, now: float | None = None) -> list[str]:
        """Soft-evict leases whose last_heartbeat is older than idle_ttl_seconds."""
        now = now if now is not None else time.time()
        evicted: list[str] = []
        with self._lock:
            stale = [
                s.lease_id
                for s in self._slots
                if s.status == SlotStatus.LEASED
                and s.lease_id
                and s.last_heartbeat is not None
                and (now - s.last_heartbeat) > self.config.idle_ttl_seconds
            ]
        for lid in stale:
            try:
                self.release(lid, reason="idle_evicted")
                evicted.append(lid)
            except LeaseNotFoundError:
                pass
        return evicted

    def shutdown(self) -> None:
        with self._lock:
            for slot in self._slots:
                if slot.chromium_pid is not None:
                    self._stop_slot(slot)
                    slot.status = SlotStatus.FREE_COLD
            self._leases.clear()
