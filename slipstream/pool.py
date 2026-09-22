"""Browser Pool Manager — lease / heartbeat / release with hard K cap."""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from slipstream.config import PoolConfig
from slipstream.launcher import ChromiumLauncher, LaunchHandle
from slipstream.models import Lease, SlotState, SlotStatus, new_lease_id
from slipstream.rss import sample_tree_rss


class PoolFullError(Exception):
    """Raised when live slots == K and no free slot can be allocated."""


class LeaseNotFoundError(Exception):
    pass


class LeaseExpiredError(Exception):
    """Raised when a heartbeat hits a hard-TTL-expired lease (already released)."""


class SpaceInUseError(Exception):
    """Raised when another agent already holds a lease on this space_id."""


class BrowserPool:
    """Owns ≤K Chromium process trees; agents lease slots, never own PIDs.

    Locking: the pool RLock protects slot / lease bookkeeping only. Chromium
    ``launcher.stop`` / ``launcher.launch`` (and any CDP wait/sleep) run
    *outside* the lock so concurrent heartbeats, status, and other leases
    can proceed while a slot is mid-launch (status ``STARTING``).
    """

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

    def _clear_lease_fields(self, slot: SlotState) -> None:
        slot.lease_id = None
        slot.agent_id = None
        slot.last_heartbeat = None
        slot.leased_at = None

    def _clear_process_fields(self, slot: SlotState) -> None:
        slot.chromium_pid = None
        slot.cdp_port = None
        slot.cdp_http_url = None
        slot.cdp_ws_url = None
        slot.rss_bytes = None

    def _detach_lease_locked(
        self,
        lease_id: str,
        *,
        allow_warm: bool,
    ) -> LaunchHandle | None:
        """Under lock: detach lease bookkeeping; return handle to stop *outside* lock.

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
        del self._leases[lease_id]

        if keep_warm:
            self._clear_lease_fields(slot)
            slot.status = SlotStatus.FREE_WARM
            # Keep space_id so a later lease for the same Space can reuse the process
            return None

        handle = self._handles.pop(slot.slot_id, None)
        self._clear_process_fields(slot)
        self._clear_lease_fields(slot)
        slot.space_id = None
        slot.status = SlotStatus.FREE_COLD
        return handle

    def _reserve_starting_locked(
        self,
        slot: SlotState,
        agent_id: str,
        space_id: str,
    ) -> tuple[int, LaunchHandle | None]:
        """Under lock: take ownership of ``slot`` for a cold launch attempt.

        Pops any existing handle (caller must stop it outside the lock).
        Sets status STARTING and binds space/agent. Returns (cdp_port, handle_to_stop).
        """
        handle = self._handles.pop(slot.slot_id, None)
        self._clear_process_fields(slot)
        self._clear_lease_fields(slot)
        slot.status = SlotStatus.STARTING
        slot.space_id = space_id
        slot.agent_id = agent_id
        return self._cdp_port_for(slot.slot_id), handle

    def _finish_launch_locked(
        self,
        slot: SlotState,
        agent_id: str,
        space_id: str,
        ttl_seconds: int | None,
        handle: LaunchHandle,
    ) -> dict[str, Any]:
        """Under lock: attach lease after a successful launch outside the lock."""
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

        # Handles that must be stopped outside the lock (released/replaced trees).
        pending_stops: list[LaunchHandle] = []
        # If set: (slot_id, cdp_port) reserved as STARTING for launch outside lock.
        launch_job: tuple[int, int] | None = None
        # Warm reuse completed under lock (no IO).
        early_result: dict[str, Any] | None = None

        bookkeeping_error: Exception | None = None
        with self._lock:
            now = time.time()
            # Idempotent: same agent+space already leased → return existing
            # unless hard TTL has expired (then release and fall through).
            for lid, existing in list(self._leases.items()):
                if existing.agent_id == agent_id and existing.space_id == space_id:
                    slot = self._find_slot_by_lease(lid)
                    if slot and slot.status == SlotStatus.LEASED:
                        if existing.expires_at is not None and now >= existing.expires_at:
                            h = self._detach_lease_locked(lid, allow_warm=False)
                            if h is not None:
                                pending_stops.append(h)
                            break
                        # Dead Chromium under an active lease → stop + fall through
                        # to cold start (mirror dead-warm path); never hand off a
                        # stale CDP endpoint.
                        if not self._handle_alive(slot):
                            h = self._detach_lease_locked(lid, allow_warm=False)
                            if h is not None:
                                pending_stops.append(h)
                            break
                        slot.last_heartbeat = now
                        early_result = existing.to_dict()
                        break

            if early_result is None:
                try:
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
                            early_result = self._attach_lease(
                                matching_warm,
                                agent_id,
                                space_id,
                                ttl_seconds,
                                cdp_http_url=matching_warm.cdp_http_url,
                                cdp_ws_url=matching_warm.cdp_ws_url,
                            )
                        else:
                            port, h = self._reserve_starting_locked(
                                matching_warm, agent_id, space_id
                            )
                            if h is not None:
                                pending_stops.append(h)
                            launch_job = (matching_warm.slot_id, port)
                    else:
                        # Prefer any FREE_WARM (different Space → stop + relaunch)
                        warm = next(
                            (s for s in self._slots if s.status == SlotStatus.FREE_WARM),
                            None,
                        )
                        cold = next(
                            (s for s in self._slots if s.status == SlotStatus.FREE_COLD),
                            None,
                        )

                        if warm is not None:
                            port, h = self._reserve_starting_locked(
                                warm, agent_id, space_id
                            )
                            if h is not None:
                                pending_stops.append(h)
                            launch_job = (warm.slot_id, port)
                        elif cold is not None:
                            # Cap: count of live process trees must stay ≤ K
                            if self._count_live_processes() >= self.config.K:
                                raise PoolFullError(f"pool at hard K={self.config.K}")
                            port, h = self._reserve_starting_locked(
                                cold, agent_id, space_id
                            )
                            if h is not None:
                                pending_stops.append(h)
                            launch_job = (cold.slot_id, port)
                        else:
                            raise PoolFullError(
                                f"pool at hard K={self.config.K}; all slots leased"
                            )
                except (SpaceInUseError, PoolFullError) as exc:
                    bookkeeping_error = exc

        # Dropped lock — stop / launch IO (and any CDP wait) happens here.
        for h in pending_stops:
            self.launcher.stop(h)

        if bookkeeping_error is not None:
            raise bookkeeping_error

        if early_result is not None:
            return early_result

        assert launch_job is not None
        slot_id, port = launch_job
        try:
            handle = self.launcher.launch(space_id, port)
        except Exception:
            # Failed launch must not leave the slot stuck in STARTING
            with self._lock:
                self._reset_slot_cold(self._slots[slot_id])
            raise

        with self._lock:
            slot = self._slots[slot_id]
            # Ownership check: still our STARTING reservation?
            if (
                slot.status != SlotStatus.STARTING
                or slot.space_id != space_id
                or slot.agent_id != agent_id
            ):
                # Lost reservation (should be rare); tear down the orphan tree outside.
                orphan = handle
            else:
                return self._finish_launch_locked(
                    slot, agent_id, space_id, ttl_seconds, handle
                )

        self.launcher.stop(orphan)
        raise PoolFullError(
            f"slot {slot_id} lost STARTING reservation during launch for space_id={space_id!r}"
        )

    def heartbeat(self, lease_id: str) -> dict[str, Any]:
        stop_handle: LaunchHandle | None = None
        expired = False
        result: dict[str, Any] | None = None
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
                stop_handle = self._detach_lease_locked(
                    lease_id, allow_warm=False
                )
                expired = True
            else:
                slot.last_heartbeat = now
                result = {
                    "lease_id": lease_id,
                    "ok": True,
                    "last_heartbeat": now,
                    "idle_ttl_seconds": self.config.idle_ttl_seconds,
                }

        if stop_handle is not None:
            self.launcher.stop(stop_handle)
        if expired:
            raise LeaseExpiredError(lease_id)
        assert result is not None
        return result

    def release(self, lease_id: str, reason: str = "client_release") -> dict[str, Any]:
        """Explicit client DELETE — may keep warm up to W."""
        with self._lock:
            lease = self._leases.get(lease_id)
            if not lease:
                raise LeaseNotFoundError(lease_id)
            slot_id = lease.slot_id
            stop_handle = self._detach_lease_locked(lease_id, allow_warm=True)
            slot = self._slots[slot_id]
            kept_warm = slot.status == SlotStatus.FREE_WARM
            result = {
                "lease_id": lease_id,
                "released": True,
                "reason": reason,
                "kept_warm": kept_warm,
            }

        if stop_handle is not None:
            self.launcher.stop(stop_handle)
        return result

    def evict_idle(self, now: float | None = None) -> list[str]:
        """Soft-evict idle leases and hard-TTL-expired leases.

        Re-checks staleness / expiry under the lock immediately before teardown
        so a concurrent heartbeat cannot be raced into an eviction.
        Evictions never keep warm (always stop Chromium).
        Stop IO runs outside the lock.
        """
        now = now if now is not None else time.time()
        evicted: list[str] = []
        pending_stops: list[LaunchHandle] = []
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
                        h = self._detach_lease_locked(lid, allow_warm=False)
                        if h is not None:
                            pending_stops.append(h)
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
                    h = self._detach_lease_locked(lid, allow_warm=False)
                    if h is not None:
                        pending_stops.append(h)
                    evicted.append(lid)
                except LeaseNotFoundError:
                    pass

        for h in pending_stops:
            self.launcher.stop(h)
        return evicted

    def shutdown(self) -> None:
        pending_stops: list[LaunchHandle] = []
        with self._lock:
            for slot in self._slots:
                handle = self._handles.pop(slot.slot_id, None)
                if handle is not None:
                    pending_stops.append(handle)
                self._clear_process_fields(slot)
                self._clear_lease_fields(slot)
                slot.space_id = None
                slot.status = SlotStatus.FREE_COLD
            self._leases.clear()
        for h in pending_stops:
            self.launcher.stop(h)
