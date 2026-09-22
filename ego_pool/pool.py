"""Browser Pool Manager — lease / heartbeat / release with hard K cap."""

from __future__ import annotations

import os
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


class LeaseExpiredError(Exception):
    """Raised when a heartbeat hits a hard-TTL-expired lease (already released)."""


class SpaceInUseError(Exception):
    """Raised when another agent already holds a lease on this space_id."""


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

    def _reset_slot_cold(self, slot: SlotState) -> None:
        """Clear all process/lease fields and mark FREE_COLD (no teardown)."""
        self._handles.pop(slot.slot_id, None)
        slot.chromium_pid = None
        slot.cdp_port = None
        slot.cdp_http_url = None
        slot.cdp_ws_url = None
        slot.rss_bytes = None
        slot.space_id = None
        self._clear_lease_fields(slot)
        slot.status = SlotStatus.FREE_COLD

    def _handle_alive(self, slot: SlotState) -> bool:
        """True if the slot's launch handle is still a live process (mocked ⇒ alive)."""
        handle = self._handles.get(slot.slot_id)
        if handle is None:
            return False
        if handle.mocked:
            return True
        if handle.process is not None:
            return handle.process.poll() is None
        pid = handle.pid if handle.pid is not None else slot.chromium_pid
        if pid is None:
            return False
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False

    def _attach_lease(
        self,
        slot: SlotState,
        agent_id: str,
        space_id: str,
        ttl_seconds: int | None,
        *,
        cdp_http_url: str | None,
        cdp_ws_url: str | None,
    ) -> dict[str, Any]:
        """Wire Lease + slot lease fields; caller sets process/CDP fields as needed."""
        now = time.time()
        lease_id = new_lease_id()
        # Client may request shorter TTLs; never exceed config ceiling (docs align).
        requested = ttl_seconds if ttl_seconds is not None else self.config.lease_hard_ttl_seconds
        hard_ttl = min(requested, self.config.lease_hard_ttl_seconds)
        lease = Lease(
            lease_id=lease_id,
            slot_id=slot.slot_id,
            agent_id=agent_id,
            space_id=space_id,
            status="leased",
            cdp_http_url=cdp_http_url,
            cdp_ws_url=cdp_ws_url,
            created_at=now,
            expires_at=now + hard_ttl,
        )
        self._leases[lease_id] = lease
        slot.status = SlotStatus.LEASED
        slot.space_id = space_id
        slot.lease_id = lease_id
        slot.agent_id = agent_id
        slot.last_heartbeat = now
        slot.leased_at = now
        return lease.to_dict()

    # --- lease API -----------------------------------------------------

    def lease(
        self,
        agent_id: str,
        space_id: str,
        *,
        ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        # Validate early (rejects ".", "..", "/", NULs, empty, unsafe)
        space_id = self.config.normalize_space_id(space_id)

        with self._lock:
            now = time.time()
            # Idempotent: same agent+space already leased → return existing
            # unless hard TTL has expired (then release and fall through).
            for lid, existing in list(self._leases.items()):
                if existing.agent_id == agent_id and existing.space_id == space_id:
                    slot = self._find_slot_by_lease(lid)
                    if slot and slot.status == SlotStatus.LEASED:
                        if existing.expires_at is not None and now >= existing.expires_at:
                            self._release_locked(
                                lid, reason="hard_ttl_expired", allow_warm=False
                            )
                            break
                        # Dead Chromium under an active lease → stop + fall through
                        # to cold start (mirror dead-warm path); never hand off a
                        # stale CDP endpoint.
                        if not self._handle_alive(slot):
                            self._release_locked(
                                lid, reason="dead_process", allow_warm=False
                            )
                            break
                        slot.last_heartbeat = now
                        return existing.to_dict()

            # Space exclusivity: never two trees on one Space without handoff
            for s in self._slots:
                if s.space_id == space_id and s.status in (
                    SlotStatus.LEASED,
                    SlotStatus.STARTING,
                ):
                    raise SpaceInUseError(
                        f"space_id {space_id!r} already in use "
                        f"(agent_id={s.agent_id!r}, status={s.status.value})"
                    )

            # Prefer FREE_WARM with matching space_id → reuse process (no relaunch)
            # if still alive; dead warm → stop + cold start.
            matching_warm = next(
                (
                    s
                    for s in self._slots
                    if s.status == SlotStatus.FREE_WARM and s.space_id == space_id
                ),
                None,
            )
            if matching_warm is not None:
                if self._handle_alive(matching_warm):
                    return self._reuse_warm_and_lease(
                        matching_warm, agent_id, space_id, ttl_seconds
                    )
                self._stop_slot(matching_warm)
                matching_warm.status = SlotStatus.FREE_COLD
                return self._start_and_lease(matching_warm, agent_id, space_id, ttl_seconds)

            # Prefer any FREE_WARM (different Space → stop + relaunch)
            warm = next((s for s in self._slots if s.status == SlotStatus.FREE_WARM), None)
            cold = next((s for s in self._slots if s.status == SlotStatus.FREE_COLD), None)

            if warm is not None:
                self._stop_slot(warm)
                return self._start_and_lease(warm, agent_id, space_id, ttl_seconds)

            if cold is not None:
                # Cap: count of live process trees must stay ≤ K (cold start adds one)
                if self._count_live_processes() >= self.config.K:
                    raise PoolFullError(f"pool at hard K={self.config.K}")
                return self._start_and_lease(cold, agent_id, space_id, ttl_seconds)

            raise PoolFullError(f"pool at hard K={self.config.K}; all slots leased")

    def _reuse_warm_and_lease(
        self,
        slot: SlotState,
        agent_id: str,
        space_id: str,
        ttl_seconds: int | None,
    ) -> dict[str, Any]:
        """Attach a new lease to an existing FREE_WARM process (same Space)."""
        return self._attach_lease(
            slot,
            agent_id,
            space_id,
            ttl_seconds,
            cdp_http_url=slot.cdp_http_url,
            cdp_ws_url=slot.cdp_ws_url,
        )

    def _start_and_lease(
        self,
        slot: SlotState,
        agent_id: str,
        space_id: str,
        ttl_seconds: int | None,
    ) -> dict[str, Any]:
        slot.status = SlotStatus.STARTING
        slot.space_id = space_id
        slot.agent_id = agent_id
        port = self._cdp_port_for(slot.slot_id)
        try:
            handle = self.launcher.launch(space_id, port)
        except Exception:
            # Failed launch must not leave the slot stuck in STARTING
            self._reset_slot_cold(slot)
            raise

        self._handles[slot.slot_id] = handle
        result = self._attach_lease(
            slot,
            agent_id,
            space_id,
            ttl_seconds,
            cdp_http_url=handle.cdp_http_url,
            cdp_ws_url=handle.cdp_ws_url,
        )
        slot.chromium_pid = handle.pid
        slot.cdp_port = handle.cdp_port
        slot.cdp_http_url = handle.cdp_http_url
        slot.cdp_ws_url = handle.cdp_ws_url
        if not self.config.mock:
            slot.rss_bytes = sample_tree_rss(handle.pid)
        return result

    def heartbeat(self, lease_id: str) -> dict[str, Any]:
        with self._lock:
            lease = self._leases.get(lease_id)
            if not lease:
                raise LeaseNotFoundError(lease_id)
            slot = self._find_slot_by_lease(lease_id)
            if not slot or slot.status != SlotStatus.LEASED:
                raise LeaseNotFoundError(lease_id)
            now = time.time()
            # Hard lease TTL: release then fail (do not refresh); never keep warm
            if lease.expires_at is not None and now >= lease.expires_at:
                self._release_locked(lease_id, reason="hard_ttl_expired", allow_warm=False)
                raise LeaseExpiredError(lease_id)
            slot.last_heartbeat = now
            return {
                "lease_id": lease_id,
                "ok": True,
                "last_heartbeat": now,
                "idle_ttl_seconds": self.config.idle_ttl_seconds,
            }

    def release(self, lease_id: str, reason: str = "client_release") -> dict[str, Any]:
        """Explicit client DELETE — may keep warm up to W."""
        with self._lock:
            return self._release_locked(lease_id, reason=reason, allow_warm=True)

    def _release_locked(
        self,
        lease_id: str,
        reason: str = "client_release",
        *,
        allow_warm: bool = False,
    ) -> dict[str, Any]:
        """Release assuming ``self._lock`` is held.

        ``allow_warm`` is True only for explicit client DELETE. Idle / hard-TTL
        evictions always stop Chromium (no FREE_WARM / no stale CDP handoff).
        """
        lease = self._leases.get(lease_id)
        if not lease:
            raise LeaseNotFoundError(lease_id)
        slot = self._find_slot_by_lease(lease_id)
        if not slot:
            raise LeaseNotFoundError(lease_id)

        keep_warm = allow_warm and self._count_warm() < self.config.W

        if keep_warm:
            # Detach lease; keep process as FREE_WARM with Space binding for reuse
            self._clear_lease_fields(slot)
            slot.status = SlotStatus.FREE_WARM
            # Keep space_id so a later lease for the same Space can reuse the process
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
        """Soft-evict idle leases and hard-TTL-expired leases.

        Re-checks staleness / expiry under the lock immediately before teardown
        so a concurrent heartbeat cannot be raced into an eviction.
        Evictions never keep warm (always stop Chromium).
        """
        now = now if now is not None else time.time()
        evicted: list[str] = []
        with self._lock:
            candidates = [
                s.lease_id
                for s in self._slots
                if s.status == SlotStatus.LEASED and s.lease_id
            ]
            for lid in candidates:
                lease = self._leases.get(lid)
                slot = self._find_slot_by_lease(lid)
                if not lease or not slot or slot.status != SlotStatus.LEASED:
                    continue

                # Hard TTL takes precedence
                if lease.expires_at is not None and now >= lease.expires_at:
                    try:
                        self._release_locked(lid, reason="hard_ttl_expired", allow_warm=False)
                        evicted.append(lid)
                    except LeaseNotFoundError:
                        pass
                    continue

                # Idle soft-evict: re-check heartbeat freshness under lock
                if slot.last_heartbeat is None:
                    continue
                if (now - slot.last_heartbeat) <= self.config.idle_ttl_seconds:
                    # Heartbeat refreshed after any stale snapshot — skip
                    continue
                try:
                    self._release_locked(lid, reason="idle_evicted", allow_warm=False)
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
