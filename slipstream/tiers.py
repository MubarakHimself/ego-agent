"""Space/lease tiers + cloud-overflow provider stub.

Tiers: ephemeral | named | attach (ui-peers three-tier). Thin attach-my-Chrome
binds a lease to an existing CDP endpoint without spawning pool Chromium
(``SLIPSTREAM_ALLOW_ATTACH=1``, default off).

Cloud overflow: Browserbase-class ``RemoteCdpProvider`` shape with **mock only**
(no paid keys / no real Browserbase HTTP). Enable via ``SLIPSTREAM_CLOUD_OVERFLOW``.
"""

from __future__ import annotations

import abc
import ipaddress
import os
import socket
import threading
import urllib.parse
import uuid
from dataclasses import dataclass
from typing import Any

TIER_EPHEMERAL = "ephemeral"
TIER_NAMED = "named"
TIER_ATTACH = "attach"
SPACE_TIERS = frozenset({TIER_EPHEMERAL, TIER_NAMED, TIER_ATTACH})

# Shown on status/list/sessions for attach leases (shared browser + honor-system).
ATTACH_RISK_LABEL = (
    "shared-browser; honor-system raw CDP / ladder bypass when driving attached "
    "Chrome directly (or with SLIPSTREAM_EXPOSE_RAW_CDP); release detaches — "
    "does not quit user Chrome"
)

# Align with CLI `_host_is_loopback`: no 0.0.0.0 (unspecified, not loopback).
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_ALLOWED_SCHEMES = frozenset({"http", "https", "ws", "wss"})
_REFUSED_SCHEMES = frozenset({"file", "javascript", "data", "blob", "about", "ftp"})

_ERR_HOST = "cdp_url host "


class TierError(ValueError):
    """Invalid tier / attach endpoint (maps to 400)."""


class AttachDisabledError(RuntimeError):
    """Attach requested but SLIPSTREAM_ALLOW_ATTACH is not enabled (maps to 403)."""


def attach_allowed() -> bool:
    """True when attach tier is enabled (safer default: off)."""
    return os.environ.get("SLIPSTREAM_ALLOW_ATTACH", "").strip() == "1"


def parse_tier(raw: Any, *, default: str = TIER_EPHEMERAL) -> str:
    """Parse tier string; ``None``/omit → default. Reject unknown."""
    if raw is None:
        return default
    if not isinstance(raw, str):
        raise TierError("tier must be a string")
    t = raw.strip().lower()
    if not t:
        return default
    if t not in SPACE_TIERS:
        raise TierError(
            f"tier must be one of {sorted(SPACE_TIERS)} (got {raw!r})"
        )
    return t


def _attach_allow_hosts() -> set[str]:
    """Loopback + optional SLIPSTREAM_ATTACH_ALLOW_HOSTS (comma/space list)."""
    hosts = set(_LOOPBACK_HOSTS)
    extra = os.environ.get("SLIPSTREAM_ATTACH_ALLOW_HOSTS", "")
    for part in extra.replace(",", " ").split():
        h = part.strip().lower().strip("[]")
        if h:
            hosts.add(h)
    return hosts



def _is_forbidden_attach_ip(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Refuse unspecified / link-local / multicast / classic metadata."""
    return bool(
        addr.is_unspecified
        or addr.is_multicast
        or addr.is_link_local
        or addr == ipaddress.ip_address("169.254.169.254")
    )


def _check_resolved_attach_ip(
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
    *,
    host_l: str,
    explicit: bool,
) -> None:
    if _is_forbidden_attach_ip(addr):
        raise TierError(
            f"cdp_url peer {host_l!r} refused (link-local/metadata/unspecified)"
        )
    if addr.is_loopback or explicit:
        return
    raise TierError(f"cdp_url peer {host_l!r} is not loopback")


def assert_attach_peer_allowed(host: str) -> None:
    """Hostname allowlist + resolve; loopback unless ATTACH_ALLOW_HOSTS.

    Explicit allowlist hosts may be non-loopback; link-local/metadata/unspecified
    are always refused.
    """
    host_l = (host or "").lower().strip("[]")
    if not host_l:
        raise TierError("cdp_url must include a host")
    allow = _attach_allow_hosts()
    if host_l not in allow:
        raise TierError(
            f"{_ERR_HOST}{host_l!r} not allowlisted (loopback or "
            "SLIPSTREAM_ATTACH_ALLOW_HOSTS)"
        )
    explicit = host_l not in _LOOPBACK_HOSTS
    try:
        literal = ipaddress.ip_address(host_l)
    except ValueError:
        literal = None
    if literal is not None:
        _check_resolved_attach_ip(literal, host_l=host_l, explicit=explicit)
        return
    try:
        infos = socket.getaddrinfo(host_l, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise TierError(f"{_ERR_HOST}{host_l!r} could not be resolved") from exc
    ips = {info[4][0] for info in infos}
    if not ips:
        raise TierError(f"{_ERR_HOST}{host_l!r} resolved to no addresses")
    for ip_s in ips:
        _check_resolved_attach_ip(
            ipaddress.ip_address(ip_s), host_l=host_l, explicit=explicit
        )


def cdp_http_from_ws(url: str) -> str:
    """Map ws(s) CDP URL to http(s) base for /json/version probes."""
    p = urllib.parse.urlparse(url)
    scheme = "https" if p.scheme == "wss" else "http"
    # Keep netloc only — drop /devtools/... path for version probe base.
    return urllib.parse.urlunparse((scheme, p.netloc, "", "", "", ""))


def resolve_attach_cdp(
    *,
    cdp_url: str | None = None,
    cdp_port: int | None = None,
) -> tuple[str, int | None]:
    """Validate and normalize attach CDP to an http(s) base URL + optional port.

    Accepts ``cdp_url`` (http/https/ws/wss) or ``cdp_port`` (→ http://127.0.0.1:PORT).
    Refuses file: / javascript: / data: and non-allowlisted hosts.
    Resolves hostnames and refuses link-local / metadata / unspecified peers.
    """
    if cdp_url is not None and cdp_port is not None:
        raise TierError("provide cdp_url or cdp_port, not both")
    if cdp_url is None and cdp_port is None:
        raise TierError("attach tier requires cdp_url or cdp_port")

    if cdp_port is not None:
        if isinstance(cdp_port, bool) or not isinstance(cdp_port, int):
            raise TierError("cdp_port must be an integer")
        if cdp_port < 1 or cdp_port > 65535:
            raise TierError("cdp_port out of range")
        return f"http://127.0.0.1:{cdp_port}", cdp_port

    if not isinstance(cdp_url, str) or not cdp_url.strip():
        raise TierError("cdp_url must be a non-empty string")
    raw = cdp_url.strip()
    parsed = urllib.parse.urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    if scheme in _REFUSED_SCHEMES or scheme not in _ALLOWED_SCHEMES:
        raise TierError(
            f"cdp_url scheme not allowed (got {scheme!r}; use http/https/ws/wss)"
        )
    host = (parsed.hostname or "").lower().strip("[]")
    if not host:
        raise TierError("cdp_url must include a host")
    assert_attach_peer_allowed(host)
    if scheme in ("ws", "wss"):
        http_url = cdp_http_from_ws(raw)
    else:
        # Drop path/query/fragment — CDP HTTP base is origin only.
        http_url = urllib.parse.urlunparse(
            (scheme, parsed.netloc, "", "", "", "")
        )
    port = parsed.port
    if port is None:
        port = 443 if scheme in ("https", "wss") else 80
    return http_url.rstrip("/"), port


def risk_label_for_tier(tier: str) -> str | None:
    if tier == TIER_ATTACH:
        return ATTACH_RISK_LABEL
    return None


def tier_blurb_table() -> str:
    """Markdown table for SKILL / POOL_API."""
    return (
        "| Tier | Meaning |\n"
        "| --- | --- |\n"
        "| `ephemeral` | **Default.** Pool-spawned Chromium; Space dir is a disposable "
        "profile label (no attach). |\n"
        "| `named` | Durable/named Space profile id (`user-data-dir` under "
        "`spaces_root`) — login-once / warm reuse. |\n"
        "| `attach` | Attach to **existing** Chrome via user CDP URL/port — "
        "**no** pool Chromium spawn. Requires `SLIPSTREAM_ALLOW_ATTACH=1`. |\n"
    )


# --- cloud overflow (mock provider) ------------------------------------

_MODE_ON = "1"
_MODE_ALWAYS = "always"
_PROVIDER_MOCK = "mock"
_TRUTHY = frozenset({_MODE_ON, "true", "yes", "on"})


class CloudProviderError(RuntimeError):
    """Unsupported / misconfigured cloud provider (maps to 503 or 400)."""


@dataclass(frozen=True)
class CloudSession:
    """Remote CDP session handle returned by a provider."""

    session_id: str
    cdp_http_url: str
    cdp_ws_url: str | None = None
    provider: str = _PROVIDER_MOCK


class RemoteCdpProvider(abc.ABC):
    """Browserbase-class shape: create/release remote CDP sessions."""

    name: str

    @abc.abstractmethod
    def create_session(
        self, *, agent_id: str, space_id: str, **kwargs: Any
    ) -> CloudSession:
        """Allocate a remote CDP session; return http/ws endpoints + id."""

    @abc.abstractmethod
    def release_session(self, session_id: str) -> None:
        """Release a previously created remote session (idempotent preferred)."""


def cloud_overflow_mode() -> str:
    """Return '', '1', or 'always' from SLIPSTREAM_CLOUD_OVERFLOW (default off)."""
    raw = os.environ.get("SLIPSTREAM_CLOUD_OVERFLOW", "").strip().lower()
    if raw in _TRUTHY:
        return _MODE_ON
    if raw == _MODE_ALWAYS:
        return _MODE_ALWAYS
    return ""


def cloud_overflow_enabled() -> bool:
    """True when overflow is on (pool_full and/or always)."""
    return cloud_overflow_mode() in (_MODE_ON, _MODE_ALWAYS)


def cloud_overflow_always() -> bool:
    """True when every non-attach lease should use the cloud provider."""
    return cloud_overflow_mode() == _MODE_ALWAYS


def cloud_provider_name() -> str:
    """SLIPSTREAM_CLOUD_PROVIDER (default mock). Only mock supported this PR."""
    return (
        os.environ.get("SLIPSTREAM_CLOUD_PROVIDER", _PROVIDER_MOCK).strip().lower()
        or _PROVIDER_MOCK
    )


class MockCloudProvider(RemoteCdpProvider):
    """In-process stub — fabricates fake CDP endpoints; records create/release."""

    name = _PROVIDER_MOCK

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._n = 0
        self.created: list[CloudSession] = []
        self.released: list[str] = []
        self._alive: set[str] = set()

    def create_session(
        self, *, agent_id: str, space_id: str, **kwargs: Any
    ) -> CloudSession:
        del agent_id, space_id, kwargs  # shape-compatible; unused by mock
        with self._lock:
            self._n += 1
            n = self._n
            sid = f"mock-{uuid.uuid4().hex[:12]}"
            # High ephemeral ports — not bound; mock CDP only (no Chrome spawn).
            port = 19000 + (n % 1000)
            http = f"http://127.0.0.1:{port}"
            ws = f"ws://127.0.0.1:{port}/devtools/browser/{sid}"
            sess = CloudSession(
                session_id=sid,
                cdp_http_url=http,
                cdp_ws_url=ws,
                provider=self.name,
            )
            self.created.append(sess)
            self._alive.add(sid)
            return sess

    def release_session(self, session_id: str) -> None:
        with self._lock:
            self.released.append(session_id)
            self._alive.discard(session_id)


def build_cloud_provider(name: str | None = None) -> RemoteCdpProvider:
    """Factory — only ``mock`` supported (no paid keys / real HTTP)."""
    resolved = (name or cloud_provider_name()).strip().lower() or _PROVIDER_MOCK
    if resolved != _PROVIDER_MOCK:
        raise CloudProviderError(
            f"unsupported SLIPSTREAM_CLOUD_PROVIDER={resolved!r}; "
            "only 'mock' is available in this release (real providers later)"
        )
    return MockCloudProvider()

