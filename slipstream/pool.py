"""Browser Pool Manager — lease / heartbeat / release with hard K cap."""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from slipstream.alerts import (
    EVENT_NEED_HUMAN,
    EVENT_TASK_DONE,
    AlertConflictError,
    build_alert_payload,
    build_harness_envelope,
    default_takeover_url,
    parse_alert_request,
)
from slipstream.activity_feed import LeaseActivityFeed, safe_url_summary

# ADV-FEED-001: key names safe to echo in feed summary (non-printable secret path).
_FEED_SAFE_KEY_NAMES = frozenset(
    {
        "Enter",
        "Tab",
        "Escape",
        "Backspace",
        "Delete",
        "ArrowUp",
        "ArrowDown",
        "ArrowLeft",
        "ArrowRight",
        "Home",
        "End",
        "PageUp",
        "PageDown",
    }
)

_ERR_LEASE_INACTIVE = "lease no longer active"
_STATE_AWAITING_HUMAN = "awaiting_human"
_STATE_LEASED = "leased"
_ACTION_CONFIRM = "confirm"
_ACTION_PAUSE = "pause"
from slipstream.watch import (
    WatchAuthError,
    WatchCaptureError,
    WatchForbiddenError,
    WatchGoneError,
    WatchInputError,
    WatchNotFoundError,
    WatchSession,
    capture_jpeg_frame,
    cede_path,
    events_path,
    confirm_path,
    dispatch_cdp_input,
    frame_path,
    input_path,
    mint_watch_token,
    parse_watch_input,
    render_watch_html,
)
from slipstream.config import PoolConfig
from slipstream.domains import (
    DomainAllowlistError,
    check_navigate_url,
    effective_allowed_domains,
    parse_allowed_domains,
)
from slipstream.launcher import ChromiumLauncher, LaunchHandle
from slipstream.models import Lease, SlotState, SlotStatus, new_lease_id
from slipstream.metadata import (
    effective_metadata,
    metadata_matches,
    validate_user_metadata,
)
from slipstream.rss import sample_tree_rss
from slipstream.vault import (
    CredNotFoundError,
    CredVault,
    VaultValidationError,
    material_for_field,
    origins_match,
    parse_fill_body,
)
from slipstream.cdp_inject import CdpInjectError, CdpInjector, default_injector
from slipstream.actions import (
    KIND_CONFIRMATION_REQUIRED,
    STATUS_CONFIRMED,
    STATUS_DENIED,
    STATUS_EXPIRED,
    STATUS_PENDING,
    ConfirmationGoneError,
    ConfirmationNotFoundError,
    ConfirmationStore,
    PendingConfirmation,
    confirm_ttl_seconds,
    mint_confirm_id,
    parse_action_request,
    parse_confirmation_action,
)



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
        # Space-level user_metadata registry (process-lifetime; leases inherit).
        self._space_metadata: dict[str, dict[str, Any]] = {}
        # Space-level allowed_domains (None key absent = inherit config/env).
        self._space_allowed_domains: dict[str, list[str]] = {}
        # Mock navigate recorder (tests); never returned with secrets.
        self._nav_log: list[dict[str, Any]] = []
        # Alerts: per-lease history + task_done idempotency (survives release).
        # Process-lifetime in-memory only (cleared on pool shutdown / process exit).
        # Bounded LRU eviction is deferred — document trust: single long-lived process.
        self._alert_log: dict[str, list[dict[str, Any]]] = {}
        self._task_done_envelopes: dict[str, dict[str, Any]] = {}
        # Watch sessions: tokenized short-TTL observe URLs (survive revoke for 410).
        self._watches: dict[str, WatchSession] = {}
        # Lease-scoped Watch activity feed (CTO-007); bounded + scrubbed.
        self._activity_feeds: dict[str, LeaseActivityFeed] = {}
        self._api_base_url: str = f"http://{self.config.host}:{self.config.port}"
        # Ensure spaces root exists
        self.config.spaces_root.mkdir(parents=True, exist_ok=True)
        # Cred vault lives OUTSIDE spaces_root (never inside user-data-dir) — ADV-002
        self.config.ensure_vault_outside_spaces()
        self.vault = CredVault(self.config.vault_root, mock=self.config.mock)
        self._cdp_injector: CdpInjector = default_injector(mock=self.config.mock)
        # Pair-browse mock recorder (tests); never returned on the wire.
        self._watch_input_log: list[dict[str, Any]] = []
        # Permission ladder: pending confirmations (process-lifetime).
        self._confirmations = ConfirmationStore()

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
                "vault_root": str(self.config.vault_root),
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


    def _space_domains(self, space_id: str) -> list[str] | None:
        """Return Space allowlist if set, else None (fall through to config)."""
        if space_id in self._space_allowed_domains:
            return list(self._space_allowed_domains[space_id])
        return None

    def _effective_domains_for(
        self,
        space_id: str,
        lease_override: list[str] | None,
    ) -> list[str]:
        return effective_allowed_domains(
            lease_domains=lease_override,
            space_domains=self._space_domains(space_id),
            config_domains=list(self.config.allowed_domains),
        )

    def _attach_lease(
        self,
        slot: SlotState,
        agent_id: str,
        space_id: str,
        ttl_seconds: int | None,
        *,
        cdp_http_url: str | None,
        cdp_ws_url: str | None,
        user_metadata: dict[str, Any] | None = None,
        allowed_domains: list[str] | None = None,
    ) -> dict[str, Any]:
        """Wire Lease + slot lease fields; caller sets process/CDP fields as needed."""
        now = time.time()
        lease_id = new_lease_id()
        # Client may request shorter TTLs; never exceed config ceiling (docs align).
        requested = ttl_seconds if ttl_seconds is not None else self.config.lease_hard_ttl_seconds
        hard_ttl = min(requested, self.config.lease_hard_ttl_seconds)
        override = validate_user_metadata(user_metadata) if user_metadata else {}
        space_meta = dict(self._space_metadata.get(space_id, {}))
        domains_override = (
            parse_allowed_domains(allowed_domains) if allowed_domains is not None else None
        )
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
            user_metadata_override=override,
            user_metadata=effective_metadata(space_meta, override),
            allowed_domains_override=domains_override,
            allowed_domains=self._effective_domains_for(space_id, domains_override),
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

        # Revoke any live Watch URL when the lease leaves the pool.
        sess = self._lookup_watch(lease_id)
        if sess is not None:
            self._invalidate_pair_browse_locked(sess, revoke=True)

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
        *,
        user_metadata: dict[str, Any] | None = None,
        allowed_domains: list[str] | None = None,
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
            user_metadata=user_metadata,
            allowed_domains=allowed_domains,
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
        user_metadata: dict[str, Any] | None = None,
        allowed_domains: list[str] | None = None,
    ) -> dict[str, Any]:
        # Validate early (rejects ".", "..", "/", NULs, empty, unsafe)
        space_id = self.config.normalize_space_id(space_id)
        # Validate metadata before taking the lock (raises MetadataValidationError).
        meta_override = validate_user_metadata(user_metadata) if user_metadata else None
        # None = inherit; list (incl. empty) = lease override. Raises DomainAllowlistError.
        domains_override = (
            parse_allowed_domains(allowed_domains) if allowed_domains is not None else None
        )

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
                        if meta_override is not None:
                            existing.user_metadata_override = meta_override
                            existing.user_metadata = effective_metadata(
                                self._space_metadata.get(space_id, {}),
                                meta_override,
                            )
                        else:
                            existing.user_metadata = effective_metadata(
                                self._space_metadata.get(space_id, {}),
                                existing.user_metadata_override,
                            )
                        if domains_override is not None:
                            existing.allowed_domains_override = domains_override
                        existing.allowed_domains = self._effective_domains_for(
                            space_id, existing.allowed_domains_override
                        )
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
                                user_metadata=meta_override,
                                allowed_domains=domains_override,
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
                    slot,
                    agent_id,
                    space_id,
                    ttl_seconds,
                    handle,
                    user_metadata=meta_override,
                    allowed_domains=domains_override,
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



    # --- credentials (vault outside Space; fill via pool CDP) -------------

    def bind_credential(self, space_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Bind a secret to space_id. Response never echoes secret."""
        safe = PoolConfig.normalize_space_id(space_id)
        if not isinstance(body, dict):
            raise VaultValidationError("body must be a JSON object")
        return self.vault.bind(
            safe,
            label=body.get("label"),
            origin=body.get("origin"),
            username=body.get("username"),
            secret=body.get("secret"),
            cred_id=body.get("cred_id"),
        )

    def unbind_credential(self, space_id: str, cred_id: str) -> dict[str, Any]:
        safe = PoolConfig.normalize_space_id(space_id)
        return self.vault.unbind(safe, cred_id)

    def list_credentials(self, space_id: str) -> dict[str, Any]:
        """Metadata only — never secrets, never cookies."""
        safe = PoolConfig.normalize_space_id(space_id)
        return self.vault.list_metadata(safe)

    def fill_credentials(self, lease_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Unlock vault + CDP-inject into leased browser. Agent sees ok/labels only.

        On login/2FA walls the agent cannot clear, raise need_human(reason=login)
        via the alerts API — do not ask the LLM for passwords.
        """
        parsed = parse_fill_body(body)
        cred_id = parsed["cred_id"]
        fields = parsed["fields"]

        with self._lock:
            lease = self._leases.get(lease_id)
            if not lease:
                raise LeaseNotFoundError(lease_id)
            slot = self._find_slot_by_lease(lease_id)
            if not slot or slot.status != SlotStatus.LEASED:
                raise LeaseNotFoundError(lease_id)
            space_id = lease.space_id
            cdp_http = slot.cdp_http_url
            if not cdp_http:
                raise CdpInjectError("lease has no cdp_http_url")

        unlocked = self.vault.unlock_for_fill(space_id, cred_id)
        filled: list[str] = []
        triples: list[tuple[str, str, str]] = []
        try:
            # ADV-010: refuse fill when page origin != bound cred.origin
            bound_origin = unlocked.get("origin", "")
            try:
                page_href = self._cdp_injector.page_url(cdp_http)
            except CdpInjectError:
                raise
            except Exception as e:
                raise CdpInjectError(f"page origin check failed: {e}") from e
            if not origins_match(bound_origin, page_href):
                raise VaultValidationError(
                    f"origin mismatch: cred bound to {bound_origin!r} but page is {page_href!r}"
                )
            for name, selector in fields.items():
                value = material_for_field(name, unlocked)
                triples.append((name, selector, value))
            filled = self._cdp_injector.fill_fields(cdp_http, triples)
        finally:
            # ADV-008: overwrite + clear secret triples and unlocked material
            for i, (n, s, v) in enumerate(triples):
                if v:
                    triples[i] = (n, s, "\0" * len(v))
            triples.clear()
            for k in list(unlocked.keys()):
                val = unlocked.get(k)
                if isinstance(val, str) and val:
                    unlocked[k] = "\0" * len(val)
            unlocked.clear()

        self._record_activity(
            lease_id,
            "fill",
            f"fill {len(filled)} field(s)",
            outcome="ok",
            detail={"fields": list(filled), "cred_id": cred_id},
        )
        return {"ok": True, "filled": filled, "cred_id": cred_id, "lease_id": lease_id}

    def set_api_base_url(self, base_url: str) -> None:
        """Bind public base URL for watch/takeover placeholders (called by PoolServer)."""
        self._api_base_url = base_url.rstrip("/")

    def raise_alert(self, lease_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Raise need_human (pause, keep lease) or task_done (notify once, release).

        Returns harness envelope JSON. Double ``task_done`` is idempotent.
        Rejects secret-like fields. Never dumps cookies/tokens/creds.
        """
        parsed = parse_alert_request(body)
        event = parsed["event"]
        base = self._api_base_url

        # Idempotent task_done after release (or repeat while still leased)
        if event == EVENT_TASK_DONE and lease_id in self._task_done_envelopes:
            prior = self._task_done_envelopes[lease_id]
            # Shallow copy harness flag
            out = {
                "alert": dict(prior["alert"]),
                "harness": dict(prior["harness"]),
            }
            out["harness"]["idempotent"] = True
            return out

        stop_handle = None
        with self._lock:
            lease = self._leases.get(lease_id)
            if not lease:
                if event == EVENT_TASK_DONE and lease_id in self._task_done_envelopes:
                    # Race: released between check and lock — still idempotent
                    prior = self._task_done_envelopes[lease_id]
                    out = {
                        "alert": dict(prior["alert"]),
                        "harness": dict(prior["harness"]),
                    }
                    out["harness"]["idempotent"] = True
                    return out
                raise LeaseNotFoundError(lease_id)
            slot = self._find_slot_by_lease(lease_id)
            if not slot or slot.status != SlotStatus.LEASED:
                raise LeaseNotFoundError(lease_id)

            # Already marked done while somehow still leased (shouldn't stick)
            if event == EVENT_NEED_HUMAN and lease_id in self._task_done_envelopes:
                raise AlertConflictError("lease already completed via task_done")

            watch_token: str | None = None
            if event == EVENT_NEED_HUMAN:
                watch_token = mint_watch_token()
                now = time.time()
                self._watches[lease_id] = WatchSession(
                    lease_id=lease_id,
                    token=watch_token,
                    expires_at=now + float(parsed["ttl_s"]),
                    reason=parsed.get("reason"),
                    detail=parsed.get("detail") or "",
                    revoked=False,
                    takeover_confirmed=False,
                    input_enabled=False,
                    created_at=now,
                )

            payload = build_alert_payload(
                lease_id=lease_id,
                space_id=lease.space_id,
                parsed=parsed,
                base_url=base,
                watch_token=watch_token,
            )
            if watch_token:
                takeover = default_takeover_url(
                    lease_id, token=watch_token, base_url=base
                )
            else:
                # task_done: watch revoked; takeover link is inert
                takeover = f"{base.rstrip('/')}/v1/leases/{lease_id}/watch?mode=takeover"

            if event == EVENT_NEED_HUMAN:
                lease.status = _STATE_AWAITING_HUMAN
                # Refresh heartbeat so soft-idle clock does not immediately fire
                # while the human is being fetched (still honor hard TTL).
                slot.last_heartbeat = time.time()
                # Keep Chromium / lease warm — agent pauses
                envelope = build_harness_envelope(
                    payload,
                    lease_kept=True,
                    lease_released=False,
                    agent_paused=True,
                    action="pause",
                    takeover_url=takeover,
                    idempotent=False,
                )
                self._alert_log.setdefault(lease_id, []).append(payload)
                self._feed_for(lease_id).append(
                    "alert",
                    f"need_human:{parsed.get('reason') or 'other'}",
                    outcome="pending",
                    detail={"event": EVENT_NEED_HUMAN, "reason": parsed.get("reason")},
                )
                return envelope

            # task_done — revoke watch_url, notify, then release once
            sess = self._lookup_watch(lease_id)
            if sess is not None:
                self._invalidate_pair_browse_locked(sess, revoke=True)
            envelope = build_harness_envelope(
                payload,
                lease_kept=False,
                lease_released=True,
                agent_paused=False,
                action="continue",
                takeover_url=takeover,
                idempotent=False,
            )
            self._alert_log.setdefault(lease_id, []).append(payload)
            self._task_done_envelopes[lease_id] = envelope
            self._feed_for(lease_id).append(
                "alert",
                f"task_done:{parsed.get('outcome') or 'ok'}",
                outcome=str(parsed.get("outcome") or "ok")[:32],
                detail={"event": EVENT_TASK_DONE},
            )
            stop_handle = self._detach_lease_locked(lease_id, allow_warm=True)

        if stop_handle is not None:
            self.launcher.stop(stop_handle)
        return envelope


    # --- permission ladder (confirm-actions) ---------------------------

    def request_action(self, lease_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Gate a sensitive act: emit confirmation_required + sibling need_human.

        Soft browse is not gated here. Categories: eval|download|upload|nav_irreversible.
        Lease stays warm on awaiting_human; agent pauses. Auto-deny after TTL (~60s).

        When category is nav_irreversible and body includes ``url``, refuse outside
        the lease allowlist (DomainAllowlistError) before minting confirmation.
        """
        parsed = parse_action_request(body)
        ttl = confirm_ttl_seconds()
        now = time.time()
        confirm_id = mint_confirm_id()
        base = self._api_base_url

        with self._lock:
            lease = self._leases.get(lease_id)
            if not lease:
                raise LeaseNotFoundError(lease_id)
            nav_url = body.get("url") if isinstance(body, dict) else None
            if (
                parsed["category"] == "nav_irreversible"
                and isinstance(nav_url, str)
                and nav_url.strip()
            ):
                check_navigate_url(nav_url.strip(), lease.allowed_domains)
            slot = self._find_slot_by_lease(lease_id)
            if not slot or slot.status != SlotStatus.LEASED:
                raise LeaseNotFoundError(lease_id)
            if lease_id in self._task_done_envelopes:
                raise AlertConflictError("lease already completed via task_done")

            # Expire any overdue pending for this lease first.
            self._expire_confirmations_locked(now)

            pending = PendingConfirmation(
                confirm_id=confirm_id,
                lease_id=lease_id,
                category=parsed["category"],
                summary=parsed["summary"],
                created_at=now,
                expires_at=now + float(ttl),
                status=STATUS_PENDING,
            )

            # Sibling need_human on SAME alerts bus (kind + confirm_id).
            watch_token = mint_watch_token()
            self._watches[lease_id] = WatchSession(
                lease_id=lease_id,
                token=watch_token,
                expires_at=now + float(ttl),
                reason="confirmation_required",
                detail=parsed["summary"],
                revoked=False,
                takeover_confirmed=False,
                input_enabled=False,
                created_at=now,
            )
            alert_parsed = {
                "event": EVENT_NEED_HUMAN,
                "reason": "confirmation_required",
                "detail": parsed["summary"],
                "task_id": None,
                "ttl_s": ttl,
                "outcome": None,
            }
            payload = build_alert_payload(
                lease_id=lease_id,
                space_id=lease.space_id,
                parsed=alert_parsed,
                base_url=base,
                watch_token=watch_token,
                kind=KIND_CONFIRMATION_REQUIRED,
                confirm_id=confirm_id,
            )
            pending.alert_event_id = payload["event_id"]
            takeover = default_takeover_url(
                lease_id, token=watch_token, base_url=base
            )
            lease.status = _STATE_AWAITING_HUMAN
            slot.last_heartbeat = now
            envelope = build_harness_envelope(
                payload,
                lease_kept=True,
                lease_released=False,
                agent_paused=True,
                action="pause",
                takeover_url=takeover,
                idempotent=False,
            )
            self._alert_log.setdefault(lease_id, []).append(payload)
            self._confirmations.add(pending)
            self._feed_for(lease_id).append(
                "confirm",
                f"confirmation_required:{parsed['category']}",
                outcome="pending",
                detail={
                    "confirm_id": confirm_id,
                    "category": parsed["category"],
                },
            )

        out = pending.to_public()
        out["alert"] = envelope["alert"]
        out["harness"] = envelope["harness"]
        return out

    def resolve_confirmation(
        self, lease_id: str, confirm_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """Confirm (allow once) or deny a pending confirmation.

        Confirm resumes the agent (lease → leased) and grants this confirm_id
        once. Deny fails closed and resumes with denied status. Expired → gone.
        """
        action = parse_confirmation_action(body)
        now = time.time()
        with self._lock:
            self._expire_confirmations_locked(now)
            pending = self._confirmations.get(confirm_id)
            if pending is None:
                raise ConfirmationNotFoundError(confirm_id)
            if pending.lease_id != lease_id:
                raise ConfirmationNotFoundError(confirm_id)
            if pending.status != STATUS_PENDING:
                raise ConfirmationGoneError(
                    f"confirmation {confirm_id} already {pending.status}"
                )
            if now >= pending.expires_at:
                pending.status = STATUS_EXPIRED
                self._clear_confirmation_pause_locked(lease_id)
                raise ConfirmationGoneError(
                    f"confirmation {confirm_id} expired"
                )

            lease = self._leases.get(lease_id)
            if not lease:
                raise LeaseNotFoundError(lease_id)

            if action == _ACTION_CONFIRM:
                pending.status = STATUS_CONFIRMED
                decision = "allow"
            else:
                pending.status = STATUS_DENIED
                decision = "deny"

            # Resume agent unless another pending confirmation remains.
            self._clear_confirmation_pause_locked(lease_id)
            self._feed_for(lease_id).append(
                "confirm",
                f"confirmation {decision}:{pending.category}",
                outcome=decision,
                detail={"confirm_id": confirm_id, "category": pending.category},
            )

            return {
                "status": pending.status,
                "confirm_id": confirm_id,
                "category": pending.category,
                "summary": pending.summary,
                "lease_id": lease_id,
                "decision": decision,
                "expires_at": pending.to_public()["expires_at"],
            }

    def resolve_confirmation_by_id(
        self, confirm_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """CLI helper: resolve by confirm_id alone (looks up lease)."""
        with self._lock:
            pending = self._confirmations.get(confirm_id)
            if pending is None:
                raise ConfirmationNotFoundError(confirm_id)
            lease_id = pending.lease_id
        return self.resolve_confirmation(lease_id, confirm_id, body)

    def _expire_confirmations_locked(self, now: float) -> list[PendingConfirmation]:
        """Expire overdue confirmations; clear pause when lease has none left."""
        newly = self._confirmations.expire_due(now)
        touched: set[str] = set()
        for p in newly:
            touched.add(p.lease_id)
        for lid in touched:
            self._clear_confirmation_pause_locked(lid)
        return newly

    def _clear_confirmation_pause_locked(self, lease_id: str) -> None:
        """If no pending confirmations remain, return lease to leased.

        Leaves Watch session intact for observe until TTL/revoke (pair-browse
        confirm is separate). Does not enable input.
        """
        if self._confirmations.lease_pending(lease_id):
            return
        lease = self._leases.get(lease_id)
        if lease is not None and lease.status == _STATE_AWAITING_HUMAN:
            # Only clear if the pause was confirmation-driven (watch reason),
            # or no other need_human reasons — for thin MVP: always clear when
            # no pending confirmations remain AND watch reason is confirmation.
            sess = self._lookup_watch(lease_id)
            if sess is not None and sess.reason == "confirmation_required":
                lease.status = _STATE_LEASED
                # Do not revoke watch here — TTL sweep / task_done handles it.
                # Pair-browse input stays disabled (never was enabled).

    def evict_idle(self, now: float | None = None) -> list[str]:
        """Soft-evict idle leases and hard-TTL-expired leases.

        Soft-idle eviction is skipped while ``lease.status == 'awaiting_human'``
        (need_human pause keeps Chromium); hard TTL still tears down.
        Re-checks staleness / expiry under the lock immediately before teardown
        so a concurrent heartbeat cannot be raced into an eviction.
        Evictions never keep warm (always stop Chromium).
        Stop IO runs outside the lock.
        """
        now = now if now is not None else time.time()
        evicted: list[str] = []
        pending_stops: list[LaunchHandle] = []
        with self._lock:
            # Permission ladder: auto-deny expired confirmations (~60s).
            self._expire_confirmations_locked(now)
            # ADV-PAIR-001: Watch TTL sweep returns drive even before next watch hit.
            for sess in list(self._watches.values()):
                if (not sess.revoked) and now >= sess.expires_at:
                    self._invalidate_pair_browse_locked(sess, revoke=False)
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

                # Soft-idle: skip while awaiting human (lease/Chromium kept).
                # Hard TTL above still applies.
                if lease.status == _STATE_AWAITING_HUMAN:
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


    # --- live Watch (observe-only) ------------------------------------

    def _lookup_watch(self, lease_id: str) -> WatchSession | None:
        """Dict lookup for watch state (name avoids Session.get SSRF false-positive)."""
        return self._watches[lease_id] if lease_id in self._watches else None

    def _invalidate_pair_browse_locked(
        self, sess: WatchSession, *, revoke: bool = False
    ) -> None:
        """ADV-PAIR-001/002: Cede OR Watch TTL (or revoke) returns drive to agent.

        Clears pair-browse flags, bumps ``input_epoch`` so in-flight CDP input
        cannot ack success, and sets ``lease.status`` back to ``leased`` when it
        was ``awaiting_human``. Must hold ``self._lock``.
        """
        sess.input_enabled = False
        sess.takeover_confirmed = False
        sess.input_epoch += 1
        if revoke:
            sess.revoked = True
        lease = self._leases.get(sess.lease_id)
        if lease is not None and lease.status == _STATE_AWAITING_HUMAN:
            lease.status = _STATE_LEASED

    def _get_watch_session_locked(
        self, lease_id: str, token: str | None
    ) -> WatchSession:
        """Validate tokenized watch access; raise Watch* errors."""
        import secrets as _secrets

        sess = self._lookup_watch(lease_id)
        if sess is None:
            raise WatchNotFoundError(lease_id)
        if (
            not token
            or len(token) != len(sess.token)
            or not _secrets.compare_digest(sess.token, token)
        ):
            raise WatchAuthError("invalid or missing watch token")
        now = time.time()
        if sess.revoked:
            raise WatchGoneError("watch_url revoked")
        if now >= sess.expires_at:
            # ADV-PAIR-001: expiry mirrors Cede — return drive, then 410.
            self._invalidate_pair_browse_locked(sess, revoke=False)
            raise WatchGoneError("watch_url expired")
        return sess


    def _feed_for(self, lease_id: str) -> LeaseActivityFeed:
        """Return (creating if needed) the lease activity feed. Caller holds lock."""
        feed = self._activity_feeds.get(lease_id)
        if feed is None:
            feed = LeaseActivityFeed()
            self._activity_feeds[lease_id] = feed
        return feed

    def _record_activity(
        self,
        lease_id: str,
        kind: str,
        summary: str,
        *,
        outcome: str = "ok",
        detail: dict | None = None,
    ) -> None:
        """Append scrubbed feed row (acquires lock)."""
        with self._lock:
            self._feed_for(lease_id).append(
                kind, summary, outcome=outcome, detail=detail
            )

    def get_watch_events(
        self,
        lease_id: str,
        token: str | None,
        *,
        after_seq: int = 0,
        limit: int = 80,
    ) -> dict:
        """Token-gated activity feed JSON — same auth/TTL/revoke as Watch (410)."""
        with self._lock:
            self._get_watch_session_locked(lease_id, token)
            if lease_id not in self._leases:
                sess = self._lookup_watch(lease_id)
                if sess is not None:
                    self._invalidate_pair_browse_locked(sess, revoke=True)
                raise WatchGoneError(_ERR_LEASE_INACTIVE)
            feed = self._activity_feeds.get(lease_id)
            events = (
                feed.list_events(after_seq=after_seq, limit=limit) if feed else []
            )
        return {"lease_id": lease_id, "events": events}

    def get_watch_page(
        self, lease_id: str, token: str | None, *, mode: str | None = None
    ) -> tuple[str, str]:
        """Return (content_type, html_body) for Watch UI (+ pair-browse when enabled)."""
        with self._lock:
            sess = self._get_watch_session_locked(lease_id, token)
            # Lease must still be live for streaming; revoked already handled.
            if lease_id not in self._leases:
                # Session exists but lease gone without revoke race — treat gone
                self._invalidate_pair_browse_locked(sess, revoke=True)
                raise WatchGoneError(_ERR_LEASE_INACTIVE)
            expires_in = max(0, int(sess.expires_at - time.time()))
            reason = sess.reason
            detail = sess.detail
            confirmed = sess.takeover_confirmed
            enabled = sess.input_enabled
            tok = sess.token
        frame = frame_path(lease_id, tok)
        confirm = confirm_path(lease_id, tok) if mode == "takeover" else None
        in_url = input_path(lease_id, tok) if enabled else None
        cede = cede_path(lease_id, tok) if (mode == "takeover" and enabled) else None
        events = events_path(lease_id, tok)
        body = render_watch_html(
            lease_id=lease_id,
            reason=reason,
            detail=detail,
            frame_url=frame,
            confirm_url=confirm,
            mode=mode,
            expires_in_s=expires_in,
            takeover_confirmed=confirmed,
            input_enabled=enabled,
            input_url=in_url,
            cede_url=cede,
            events_url=events,
        )
        return "text/html; charset=utf-8", body

    def _revalidate_watch_after_capture(
        self,
        lease_id: str,
        *,
        slot_id: int,
        cdp_http_url: str,
    ) -> None:
        """ADV-WATCH-001: after CDP IO, refuse revoked / identity-mismatched frames.

        Must hold ``self._lock``. Raises WatchGoneError → HTTP 410. Never soft-
        falls back to mock JPEG after auth (would mask revoke / port reuse).
        """
        sess = self._lookup_watch(lease_id)
        if sess is None or sess.revoked:
            raise WatchGoneError("watch_url revoked during capture")
        if time.time() >= sess.expires_at:
            self._invalidate_pair_browse_locked(sess, revoke=False)
            raise WatchGoneError("watch_url expired during capture")
        lease = self._leases.get(lease_id)
        if not lease:
            raise WatchGoneError(_ERR_LEASE_INACTIVE)
        if lease.slot_id != slot_id:
            raise WatchGoneError("lease/slot identity changed during capture")
        if (lease.cdp_http_url or "") != (cdp_http_url or ""):
            raise WatchGoneError("lease/slot identity changed during capture")
        slot = self._slots[slot_id] if 0 <= slot_id < len(self._slots) else None
        if slot is None or slot.lease_id != lease_id:
            raise WatchGoneError("lease/slot identity changed during capture")

    def get_watch_frame(self, lease_id: str, token: str | None) -> bytes:
        """Return JPEG bytes for the leased CDP viewport (mock JPEG under mock).

        ADV-WATCH-001: capture runs outside the lock; before returning bytes we
        re-check revoked + lease/slot/CDP identity so a concurrent task_done /
        port reuse cannot deliver another session's screenshot (or a mock soft-
        fallback that would look like a live 200).
        """
        with self._lock:
            self._get_watch_session_locked(lease_id, token)
            lease = self._leases.get(lease_id)
            if not lease:
                raise WatchGoneError(_ERR_LEASE_INACTIVE)
            cdp = lease.cdp_http_url or ""
            slot_id = lease.slot_id
            mock = self.config.mock
        # Capture outside lock (CDP IO).
        try:
            jpeg = capture_jpeg_frame(cdp, mock=mock)
        except WatchCaptureError:
            # Prefer 410 if revoke/identity race during a failed capture; never
            # mock soft-fallback after auth (ADV-WATCH-001).
            with self._lock:
                self._revalidate_watch_after_capture(
                    lease_id, slot_id=slot_id, cdp_http_url=cdp
                )
            raise
        with self._lock:
            self._revalidate_watch_after_capture(
                lease_id, slot_id=slot_id, cdp_http_url=cdp
            )
        return jpeg

    def confirm_watch_takeover(
        self, lease_id: str, token: str | None
    ) -> dict[str, Any]:
        """Confirm Take-over → pause agent, enable exclusive pair-browse input."""
        with self._lock:
            sess = self._get_watch_session_locked(lease_id, token)
            lease = self._leases.get(lease_id)
            if not lease:
                self._invalidate_pair_browse_locked(sess, revoke=True)
                raise WatchGoneError(_ERR_LEASE_INACTIVE)
            lease.status = _STATE_AWAITING_HUMAN
            slot = self._find_slot_by_lease(lease_id)
            if slot is not None:
                slot.last_heartbeat = time.time()
            sess.takeover_confirmed = True
            sess.input_enabled = True
            self._feed_for(lease_id).append(
                "confirm",
                "takeover confirmed",
                outcome="ok",
                detail={"action": "pause"},
            )
            return {
                "ok": True,
                "lease_id": lease_id,
                "status": _STATE_AWAITING_HUMAN,
                "agent_paused": True,
                "lease_kept": True,
                "takeover_confirmed": True,
                "input_enabled": True,
                "action": "pause",
                "expires_in_s": max(0, int(sess.expires_at - time.time())),
            }

    def dispatch_watch_input(
        self, lease_id: str, token: str | None, body: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Bridge human click/type/scroll into leased CDP after Take-over confirm.

        Observe-only (pre-confirm / post-cede) → WatchForbiddenError (403).
        Revoked / TTL / concurrent cede (epoch change) → WatchGoneError (410).
        Never echoes typed text. ADV-PAIR-002: capture ``input_epoch`` under lock
        before CDP; re-check after CDP before acknowledging success.
        """
        event = parse_watch_input(body)
        with self._lock:
            sess = self._get_watch_session_locked(lease_id, token)
            if not sess.input_enabled or not sess.takeover_confirmed:
                raise WatchForbiddenError(
                    "input disabled — observe-only until Take-over confirm "
                    "(or after Cede)"
                )
            lease = self._leases.get(lease_id)
            if not lease:
                self._invalidate_pair_browse_locked(sess, revoke=True)
                raise WatchGoneError(_ERR_LEASE_INACTIVE)
            if lease.status != _STATE_AWAITING_HUMAN:
                raise WatchForbiddenError("agent not paused for pair-browse")
            cdp = lease.cdp_http_url or ""
            slot_id = lease.slot_id
            mock = self.config.mock
            epoch = sess.input_epoch
            # Heartbeat warm while human drives
            slot = self._find_slot_by_lease(lease_id)
            if slot is not None:
                slot.last_heartbeat = time.time()

        try:
            result = dispatch_cdp_input(
                cdp, event, mock=mock, mock_log=self._watch_input_log
            )
        except WatchCaptureError:
            with self._lock:
                self._revalidate_watch_after_capture(
                    lease_id, slot_id=slot_id, cdp_http_url=cdp
                )
            raise
        with self._lock:
            self._revalidate_watch_after_capture(
                lease_id, slot_id=slot_id, cdp_http_url=cdp
            )
            sess = self._lookup_watch(lease_id)
            # ADV-PAIR-002: concurrent cede/revoke/TTL bumped epoch → 410, no success.
            if (
                sess is None
                or sess.revoked
                or sess.input_epoch != epoch
                or not sess.input_enabled
            ):
                raise WatchGoneError("pair-browse invalidated during dispatch")
        # Scrubbed ack only — feed never stores typed text (text_len only).
        kind = result["kind"]
        if kind == "click":
            self._record_activity(
                lease_id,
                "click",
                f"click @ ({int(event.get('x', 0))},{int(event.get('y', 0))})",
                outcome="ok",
            )
        elif kind == "type":
            self._record_activity(
                lease_id,
                "type",
                "type",
                outcome="ok",
                detail={"text_len": len(event.get("text") or "")},
            )
        elif kind == "key":
            # ADV-FEED-001: never echo key names (printable chars reconstruct secrets).
            # Allowlist navigation/edit keys only; otherwise key_len in detail.
            raw_key = event.get("key") or ""
            safe = raw_key in _FEED_SAFE_KEY_NAMES
            self._record_activity(
                lease_id,
                "type",
                f"key {raw_key}" if safe else "key",
                outcome="ok",
                detail={"key_len": len(raw_key)},
            )
        elif kind == "scroll":
            self._record_activity(
                lease_id,
                "click",
                "scroll",
                outcome="ok",
                detail={"deltaY": event.get("deltaY")},
            )
        return {"ok": True, "kind": kind, "input_enabled": True}

    def cede_watch_control(
        self, lease_id: str, token: str | None
    ) -> dict[str, Any]:
        """Cede pair-browse: disable input, clear pause; lease stays until task_done.

        ADV-PAIR-004: requires prior Take-over confirm (``takeover_confirmed`` or
        ``input_enabled``); otherwise ``WatchInputError`` → HTTP 400 nothing to cede.
        """
        with self._lock:
            sess = self._get_watch_session_locked(lease_id, token)
            lease = self._leases.get(lease_id)
            if not lease:
                self._invalidate_pair_browse_locked(sess, revoke=True)
                raise WatchGoneError(_ERR_LEASE_INACTIVE)
            if not sess.takeover_confirmed and not sess.input_enabled:
                raise WatchInputError("nothing to cede")
            self._invalidate_pair_browse_locked(sess, revoke=False)
            slot = self._find_slot_by_lease(lease_id)
            if slot is not None:
                slot.last_heartbeat = time.time()
            self._feed_for(lease_id).append(
                "confirm",
                "cede — drive returned to agent",
                outcome="ok",
                detail={"action": "continue"},
            )
            return {
                "ok": True,
                "lease_id": lease_id,
                "status": "leased",
                "agent_paused": False,
                "lease_kept": True,
                "input_enabled": False,
                "takeover_confirmed": False,
                "action": "continue",
                "expires_in_s": max(0, int(sess.expires_at - time.time())),
            }


    # --- navigate (domain allowlist gate) ------------------------------

    def navigate(self, lease_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Top-frame navigate via CDP (or mock). Refuse outside allowlist.

        Empty effective allowlist = unrestricted. iframe/subresource not gated (v1).
        """
        if not isinstance(body, dict):
            raise DomainAllowlistError("body must be a JSON object")
        url = body.get("url")
        if not isinstance(url, str) or not url.strip():
            raise DomainAllowlistError("url must be a non-empty string")
        url = url.strip()

        with self._lock:
            lease = self._leases.get(lease_id)
            if not lease:
                raise LeaseNotFoundError(lease_id)
            slot = self._find_slot_by_lease(lease_id)
            if not slot or slot.status != SlotStatus.LEASED:
                raise LeaseNotFoundError(lease_id)
            patterns = list(lease.allowed_domains)
            cdp_http = slot.cdp_http_url
            mock = self.config.mock

        check_navigate_url(url, patterns)

        if mock:
            with self._lock:
                self._nav_log.append(
                    {"lease_id": lease_id, "url": url, "allowed_domains": patterns}
                )
            self._record_activity(
                lease_id,
                "navigate",
                safe_url_summary(url),
                outcome="ok",
                detail={"mock": True},
            )
            return {
                "ok": True,
                "url": url,
                "matched": True,
                "mock": True,
                "allowed_domains": patterns,
            }

        if not cdp_http:
            raise DomainAllowlistError("lease has no cdp_http_url")
        from slipstream.cdp_http import navigate_via_json_new

        result = navigate_via_json_new(cdp_http, url, allowed_domains=patterns)
        ok = bool(result.get("matched"))
        self._record_activity(
            lease_id,
            "navigate",
            safe_url_summary(url),
            outcome="ok" if ok else "error",
            detail={"matched": ok},
        )
        return {
            "ok": ok,
            "url": url,
            "matched": ok,
            "title": (result.get("title") or "")[:200],
            "observed_url": result.get("url") or "",
            "allowed_domains": patterns,
        }

    # --- user_metadata / list ------------------------------------------

    def set_space_metadata(
        self,
        space_id: str,
        user_metadata: dict[str, Any] | None = None,
        *,
        allowed_domains: list[str] | None = None,
        clear_allowed_domains: bool = False,
    ) -> dict[str, Any]:
        """Set/replace Space-level tags and/or allowed_domains.

        Leases inherit; lease overrides win. ``allowed_domains=None`` leaves the
        Space allowlist unchanged unless ``clear_allowed_domains`` is True.
        """
        space_id = self.config.normalize_space_id(space_id)
        meta = (
            validate_user_metadata(user_metadata)
            if user_metadata is not None
            else None
        )
        domains = (
            parse_allowed_domains(allowed_domains)
            if allowed_domains is not None
            else None
        )
        with self._lock:
            if meta is not None:
                if meta:
                    self._space_metadata[space_id] = meta
                else:
                    self._space_metadata.pop(space_id, None)
            if clear_allowed_domains:
                self._space_allowed_domains.pop(space_id, None)
            elif domains is not None:
                self._space_allowed_domains[space_id] = domains
            space_meta = dict(self._space_metadata.get(space_id, {}))
            for lease in self._leases.values():
                if lease.space_id == space_id:
                    lease.user_metadata = effective_metadata(
                        space_meta, lease.user_metadata_override
                    )
                    try:
                        lease.allowed_domains = self._effective_domains_for(
                            space_id, lease.allowed_domains_override
                        )
                    except DomainAllowlistError:
                        # Space tightened past lease override — drop override, inherit.
                        lease.allowed_domains_override = None
                        lease.allowed_domains = self._effective_domains_for(
                            space_id, None
                        )
            out: dict[str, Any] = {
                "space_id": space_id,
                "user_metadata": dict(self._space_metadata.get(space_id, {})),
            }
            if space_id in self._space_allowed_domains:
                out["allowed_domains"] = list(self._space_allowed_domains[space_id])
            else:
                out["allowed_domains"] = None
            return out

    def get_space_metadata(self, space_id: str) -> dict[str, Any]:
        space_id = self.config.normalize_space_id(space_id)
        with self._lock:
            return {
                "space_id": space_id,
                "user_metadata": dict(self._space_metadata.get(space_id, {})),
                "allowed_domains": list(self._space_allowed_domains[space_id])
                if space_id in self._space_allowed_domains
                else None,
            }

    def list_spaces(self, *, q: str | None = None) -> dict[str, Any]:
        """List known Spaces (tagged and/or currently slotted) filtered by q=."""
        with self._lock:
            known: set[str] = set(self._space_metadata.keys()) | set(
                self._space_allowed_domains.keys()
            )
            for slot in self._slots:
                if slot.space_id:
                    known.add(slot.space_id)
            items: list[dict[str, Any]] = []
            for sid in sorted(known):
                meta = dict(self._space_metadata.get(sid, {}))
                if not metadata_matches(meta, q):
                    continue
                leased = any(
                    s.space_id == sid and s.status == SlotStatus.LEASED
                    for s in self._slots
                )
                warm = any(
                    s.space_id == sid and s.status == SlotStatus.FREE_WARM
                    for s in self._slots
                )
                items.append(
                    {
                        "space_id": sid,
                        "user_metadata": meta,
                        "allowed_domains": list(self._space_allowed_domains[sid])
                        if sid in self._space_allowed_domains
                        else None,
                        "leased": leased,
                        "warm": warm,
                    }
                )
            return {"spaces": items, "q": q}

    def list_leases(self, *, q: str | None = None) -> dict[str, Any]:
        """List active leases filtered by effective user_metadata q=."""
        with self._lock:
            items: list[dict[str, Any]] = []
            for lease in self._leases.values():
                lease.user_metadata = effective_metadata(
                    self._space_metadata.get(lease.space_id, {}),
                    lease.user_metadata_override,
                )
                if not metadata_matches(lease.user_metadata, q):
                    continue
                items.append(lease.to_dict())
            items.sort(key=lambda x: x.get("created_at") or 0)
            return {"leases": items, "q": q}

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
            self._space_metadata.clear()
            self._watches.clear()
            self._watch_input_log.clear()
            self._activity_feeds.clear()
        for h in pending_stops:
            self.launcher.stop(h)
