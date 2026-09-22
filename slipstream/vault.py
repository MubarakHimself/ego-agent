"""Space-bound credential vault — secrets NEVER enter agent/LLM context.

Store location is **outside** Space ``user-data-dir`` (``VAULT_ROOT`` /
``SLIPSTREAM_VAULT_ROOT``). Space profiles keep session cookies only.

Backends (prefer OS keyring; file Fernet fallback; memory for mock/tests):
  - keyring (optional): secret material under service ``slipstream``
  - Fernet file blobs (optional cryptography) keyed by keyring master or
    ``SLIPSTREAM_VAULT_KEY`` (tests / CI only — never commit real keys)
  - in-memory when ``mock=True`` / ``SLIPSTREAM_MOCK=1`` and no keyring/key

Public surface: bind / unbind / list-metadata / unlock-for-fill.
There is **no** free-read secret API and **no** cookie dump.
"""

from __future__ import annotations

import json
import re
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from slipstream.alerts import AlertValidationError, reject_secret_fields

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class VaultError(Exception):
    """Base vault failure."""


class VaultUnavailableError(VaultError):
    """No keyring and no test key — cannot persist secrets."""


class CredNotFoundError(VaultError):
    """Unknown cred_id for this space."""


class VaultValidationError(VaultError):
    """Bad bind/fill request."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_KEYRING_SERVICE = "slipstream"
_KEYRING_MASTER = "slipstream-master"


def new_cred_id() -> str:
    return f"cred_{uuid.uuid4().hex[:16]}"


def _normalize_origin(origin: str) -> str:
    o = (origin or "").strip()
    if not o:
        raise VaultValidationError("origin required")
    if len(o) > 500:
        raise VaultValidationError("origin too long")
    lowered = o.lower()
    for bad in ("password=", "token=", "secret=", "authorization="):
        if bad in lowered:
            raise VaultValidationError("origin must not carry secrets")
    return o


def _require_nonempty_str(name: str, value: Any, *, max_len: int = 500) -> str:
    if not isinstance(value, str) or not value.strip():
        raise VaultValidationError(f"{name} must be a non-empty string")
    s = value.strip()
    if len(s) > max_len:
        raise VaultValidationError(f"{name} too long (max {max_len})")
    return s


_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _safe_vault_id(name: str, value: str, *, max_len: int) -> str:
    """Strict charset for path components — no separators, '..', or odd unicode."""
    s = _require_nonempty_str(name, value, max_len=max_len)
    if ".." in s or "/" in s or "\\" in s or "\x00" in s:
        raise VaultValidationError(f"{name} must not contain path separators or '..'")
    if not _SAFE_ID_RE.match(s):
        raise VaultValidationError(
            f"{name} must match [A-Za-z0-9._-]+ (got {s!r})"
        )
    return s


def _resolve_policy_path(path: Path) -> Path:
    """Resolve vault path (Skylos PATH_SANITIZERS-recognized name)."""
    return Path(path).expanduser().resolve()


def _write_bytes_nofollow(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """Atomic-ish create/truncate write that refuses symlink follow (O_NOFOLLOW)."""
    path = _resolve_policy_path(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    # Callers must pass a path already contained under vault_root/blobs (resolve+relative_to).
    fd = os.open(path, flags, mode)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _mkdir_private(path: Path) -> None:
    """Create directory with mode 0o700 (owner-only). ADV-004."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def origins_match(bound_origin: str, page_url: str) -> bool:
    """True if page URL is same-origin as the credential bind origin (ADV-010)."""
    try:
        a = urlparse((bound_origin or "").strip())
        b = urlparse((page_url or "").strip())
    except Exception:
        return False
    if not a.scheme or not a.netloc or not b.scheme or not b.netloc:
        return False
    return a.scheme.lower() == b.scheme.lower() and a.netloc.lower() == b.netloc.lower()


def assert_vault_key_allowed(*, mock: bool) -> None:
    """Refuse SLIPSTREAM_VAULT_KEY unless mock or explicit allow (ADV-011)."""
    if not os.environ.get("SLIPSTREAM_VAULT_KEY", "").strip():
        return
    if mock or os.environ.get("SLIPSTREAM_MOCK", "") == "1":
        return
    if os.environ.get("SLIPSTREAM_ALLOW_VAULT_KEY", "") == "1":
        return
    raise VaultUnavailableError(
        "SLIPSTREAM_VAULT_KEY refused outside mock/tests; "
        "set SLIPSTREAM_ALLOW_VAULT_KEY=1 to override, or use OS keyring"
    )


# ---------------------------------------------------------------------------
# Secret backends
# ---------------------------------------------------------------------------


class SecretBackend(Protocol):
    def put(self, space_id: str, cred_id: str, payload: dict[str, str]) -> None: ...

    def get(self, space_id: str, cred_id: str) -> dict[str, str] | None: ...

    def delete(self, space_id: str, cred_id: str) -> None: ...


class MemorySecretBackend:
    """Process-memory only — mock / unit tests."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, str]] = {}

    def _key(self, space_id: str, cred_id: str) -> str:
        return f"{space_id}:{cred_id}"

    def put(self, space_id: str, cred_id: str, payload: dict[str, str]) -> None:
        self._data[self._key(space_id, cred_id)] = dict(payload)

    def get(self, space_id: str, cred_id: str) -> dict[str, str] | None:
        item = self._data.get(self._key(space_id, cred_id))
        return dict(item) if item else None

    def delete(self, space_id: str, cred_id: str) -> None:
        self._data.pop(self._key(space_id, cred_id), None)


class KeyringSecretBackend:
    """OS keyring (Secret Service / Keychain / Credential Locker)."""

    def __init__(self, keyring_mod: Any):
        self._kr = keyring_mod

    def _user(self, space_id: str, cred_id: str) -> str:
        return f"space:{space_id}:cred:{cred_id}"

    def put(self, space_id: str, cred_id: str, payload: dict[str, str]) -> None:
        self._kr.set_password(
            _KEYRING_SERVICE, self._user(space_id, cred_id), json.dumps(payload)
        )

    def get(self, space_id: str, cred_id: str) -> dict[str, str] | None:
        raw = self._kr.get_password(_KEYRING_SERVICE, self._user(space_id, cred_id))
        if not raw:
            return None
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        return {str(k): str(v) for k, v in data.items()}

    def delete(self, space_id: str, cred_id: str) -> None:
        try:
            self._kr.delete_password(_KEYRING_SERVICE, self._user(space_id, cred_id))
        except Exception:
            pass


class FernetFileSecretBackend:
    """Encrypted blobs under vault_root/blobs/ — key from env or keyring master."""

    def __init__(self, blobs_dir: Path, fernet: Any):
        self._dir = blobs_dir
        _mkdir_private(self._dir)
        self._fernet = fernet

    def _path(self, space_id: str, cred_id: str) -> Path:
        sid = _safe_vault_id("space_id", space_id, max_len=200)
        cid = _safe_vault_id("cred_id", cred_id, max_len=80)
        root = self._dir.resolve()
        path = (root / f"{sid}__{cid}.bin").resolve()
        # Containment: resolved path must stay under blobs_dir.
        if not path.is_relative_to(root):
            raise VaultValidationError("blob path escapes vault blobs dir")
        return path

    def put(self, space_id: str, cred_id: str, payload: dict[str, str]) -> None:
        token = self._fernet.encrypt(json.dumps(payload).encode("utf-8"))
        path = self._path(space_id, cred_id)
        # Refuse pre-existing symlink at the target (lstat / is_symlink).
        if path.exists() or path.is_symlink():
            if path.is_symlink():
                raise VaultValidationError("refusing symlink vault blob path")
        _write_bytes_nofollow(path, token, mode=0o600)

    def get(self, space_id: str, cred_id: str) -> dict[str, str] | None:
        p = self._path(space_id, cred_id)
        if p.is_symlink():
            raise VaultValidationError("refusing symlink vault blob path")
        if not p.is_file():
            return None
        # Bound read size for Skylos SKY-D215 read guards (st_size / MAX).
        MAX_BYTES = 1_048_576
        st = p.stat()
        if st.st_size > MAX_BYTES:
            raise VaultValidationError("vault blob too large")
        data = json.loads(self._fernet.decrypt(p.read_bytes()).decode("utf-8"))
        if not isinstance(data, dict):
            return None
        return {str(k): str(v) for k, v in data.items()}

    def delete(self, space_id: str, cred_id: str) -> None:
        p = self._path(space_id, cred_id)
        if p.is_symlink():
            raise VaultValidationError("refusing symlink vault blob path")
        if p.is_file():
            p.unlink()


def _try_import_keyring() -> Any | None:
    try:
        import keyring  # type: ignore

        return keyring
    except Exception:
        return None


def _fernet_from_key_material(key_b64: str) -> Any:
    from cryptography.fernet import Fernet  # type: ignore

    return Fernet(key_b64.encode("ascii") if isinstance(key_b64, str) else key_b64)


def _load_or_create_master_key(keyring_mod: Any | None, *, mock: bool = False) -> str | None:
    """Return url-safe Fernet key string, or None if unavailable."""
    env = os.environ.get("SLIPSTREAM_VAULT_KEY", "").strip()
    if env:
        assert_vault_key_allowed(mock=mock)
        return env
    if keyring_mod is not None:
        try:
            existing = keyring_mod.get_password(_KEYRING_SERVICE, _KEYRING_MASTER)
            if existing:
                return existing
        except Exception:
            pass
        try:
            from cryptography.fernet import Fernet  # type: ignore

            fresh = Fernet.generate_key().decode("ascii")
            keyring_mod.set_password(_KEYRING_SERVICE, _KEYRING_MASTER, fresh)
            return fresh
        except Exception:
            return None
    return None


def build_secret_backend(
    *,
    vault_root: Path,
    mock: bool = False,
    force_memory: bool = False,
) -> tuple[SecretBackend, str]:
    """Pick backend. Returns (backend, name)."""
    if force_memory:
        return MemorySecretBackend(), "memory"
    if os.environ.get("SLIPSTREAM_VAULT_KEY", "").strip():
        assert_vault_key_allowed(mock=mock)
    if mock and not os.environ.get("SLIPSTREAM_VAULT_KEY"):
        # Mock default: memory (deterministic tests; no OS keyring required)
        return MemorySecretBackend(), "memory"

    kr = _try_import_keyring()
    if kr is not None:
        try:
            kr.get_password(_KEYRING_SERVICE, "__slipstream_probe__")
            return KeyringSecretBackend(kr), "keyring"
        except Exception:
            pass

    key_mat = _load_or_create_master_key(kr, mock=mock)
    if key_mat:
        try:
            fernet = _fernet_from_key_material(key_mat)
            return (
                FernetFileSecretBackend(vault_root / "blobs", fernet),
                "fernet_file",
            )
        except Exception as e:
            if not mock:
                raise VaultUnavailableError(f"fernet backend failed: {e}") from e

    if mock:
        return MemorySecretBackend(), "memory"

    raise VaultUnavailableError(
        "credential vault unavailable: install keyring (preferred) or set "
        "SLIPSTREAM_VAULT_KEY for tests only (with SLIPSTREAM_ALLOW_VAULT_KEY=1)"
    )


# ---------------------------------------------------------------------------
# Vault
# ---------------------------------------------------------------------------


class CredVault:
    """Pool-owned vault bound to ``space_id``. Metadata on disk; secrets in backend."""

    def __init__(
        self,
        vault_root: Path,
        *,
        mock: bool = False,
        backend: SecretBackend | None = None,
        backend_name: str | None = None,
    ):
        self.vault_root = Path(vault_root)
        _mkdir_private(self.vault_root)
        self._meta_path = self.vault_root / "bindings.json"
        self._lock = threading.RLock()
        if backend is not None:
            self._backend = backend
            self.backend_name = backend_name or "custom"
        else:
            self._backend, self.backend_name = build_secret_backend(
                vault_root=self.vault_root, mock=mock
            )
        if not self._meta_path.is_file():
            self._write_meta({"spaces": {}})

    def _read_meta(self) -> dict[str, Any]:
        try:
            raw = json.loads(self._meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"spaces": {}}
        if not isinstance(raw, dict):
            return {"spaces": {}}
        spaces = raw.get("spaces")
        if not isinstance(spaces, dict):
            raw["spaces"] = {}
        return raw

    def _write_meta(self, data: dict[str, Any]) -> None:
        # Containment under vault_root; refuse symlink tmp/meta (O_NOFOLLOW).
        root = self.vault_root.resolve()
        meta = self._meta_path.resolve()
        tmp = (self._meta_path.with_suffix(".tmp")).resolve()
        if not meta.is_relative_to(root) or not tmp.is_relative_to(root):
            raise VaultValidationError("meta path escapes vault_root")
        if tmp.is_symlink() or meta.is_symlink():
            raise VaultValidationError("refusing symlink vault meta path")
        payload = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")
        _write_bytes_nofollow(tmp, payload, mode=0o600)
        tmp.replace(meta)
        try:
            os.chmod(meta, 0o600)
        except OSError:
            pass

    def bind(
        self,
        space_id: str,
        *,
        label: str,
        origin: str,
        username: str,
        secret: str,
        cred_id: str | None = None,
    ) -> dict[str, Any]:
        """Store secret material; return public ack (never echoes secret)."""
        space_id = _safe_vault_id("space_id", space_id, max_len=200)
        label = _require_nonempty_str("label", label, max_len=200)
        origin = _normalize_origin(origin)
        username = _require_nonempty_str("username", username, max_len=500)
        if not isinstance(secret, str) or secret == "":
            raise VaultValidationError("secret must be a non-empty string")
        if len(secret) > 8192:
            raise VaultValidationError("secret too long")

        cid = cred_id or new_cred_id()
        cid = _safe_vault_id("cred_id", cid, max_len=80)

        with self._lock:
            self._backend.put(
                space_id,
                cid,
                {"username": username, "secret": secret},
            )
            meta = self._read_meta()
            spaces = meta.setdefault("spaces", {})
            bucket = spaces.setdefault(space_id, {})
            bucket[cid] = {
                "label": label,
                "origin": origin,
                "has_secret": True,
            }
            self._write_meta(meta)

        return {"cred_id": cid, "label": label, "origin": origin, "bound": True}

    def unbind(self, space_id: str, cred_id: str) -> dict[str, Any]:
        space_id = _safe_vault_id("space_id", space_id, max_len=200)
        cred_id = _safe_vault_id("cred_id", cred_id, max_len=80)
        with self._lock:
            meta = self._read_meta()
            spaces = meta.get("spaces") or {}
            bucket = spaces.get(space_id) or {}
            if cred_id not in bucket:
                raise CredNotFoundError(cred_id)
            del bucket[cred_id]
            if not bucket:
                spaces.pop(space_id, None)
            self._write_meta(meta)
            self._backend.delete(space_id, cred_id)
        return {"unbound": True, "cred_id": cred_id, "space_id": space_id}

    def list_metadata(self, space_id: str) -> dict[str, Any]:
        """Metadata only — never secret, never cookie jar, never username."""
        space_id = _safe_vault_id("space_id", space_id, max_len=200)
        with self._lock:
            meta = self._read_meta()
            bucket = (meta.get("spaces") or {}).get(space_id) or {}
            items = []
            for cid, info in bucket.items():
                items.append(
                    {
                        "cred_id": cid,
                        "label": info.get("label", ""),
                        "origin": info.get("origin", ""),
                        "has_secret": bool(info.get("has_secret", True)),
                    }
                )
            items.sort(key=lambda x: x["cred_id"])
            return {"items": items, "space_id": space_id}

    def unlock_for_fill(self, space_id: str, cred_id: str) -> dict[str, str]:
        """Pool-only: return username+secret for CDP inject. Caller must zero."""
        space_id = _safe_vault_id("space_id", space_id, max_len=200)
        cred_id = _safe_vault_id("cred_id", cred_id, max_len=80)
        with self._lock:
            meta = self._read_meta()
            bucket = (meta.get("spaces") or {}).get(space_id) or {}
            if cred_id not in bucket:
                raise CredNotFoundError(cred_id)
            info = bucket[cred_id]
            payload = self._backend.get(space_id, cred_id)
            if not payload or "secret" not in payload:
                raise CredNotFoundError(cred_id)
            return {
                "username": str(payload.get("username", "")),
                "secret": str(payload["secret"]),
                "origin": str(info.get("origin", "")),
            }

    def refuse_free_read(self, *_args: Any, **_kwargs: Any) -> None:
        raise VaultValidationError(
            "refused: free-read of secrets is not available (use fill)"
        )

    def refuse_cookie_dump(self, *_args: Any, **_kwargs: Any) -> None:
        raise VaultValidationError(
            "refused: cookie / storageState dump to agent is not available"
        )


def parse_fill_body(body: dict[str, Any]) -> dict[str, Any]:
    """Validate fill request — selectors only; no secret values from agent.

    ADV-007: reuse alerts.reject_secret_fields recursively on the body *except*
    ``fields`` keys (logical names like ``password`` map to CSS selectors).
    Expanded top-level refuse set still fails closed for common leaks.
    """
    if not isinstance(body, dict):
        raise VaultValidationError("body must be a JSON object")
    expanded_bad = (
        "secret",
        "password",
        "passwd",
        "token",
        "cookie",
        "cookies",
        "storage_state",
        "authorization",
        "bearer",
        "api_key",
        "private_key",
        "credential",
        "credentials",
        "jwt",
        "access_key",
        "auth_header",
    )
    for bad in expanded_bad:
        if bad in body:
            raise VaultValidationError(
                f"refused: fill must not include {bad!r}; pass cred_id + selectors only"
            )
    # Recursive reject on non-fields keys (nested secret dumps)
    body_for_reject = {k: v for k, v in body.items() if k != "fields"}
    try:
        reject_secret_fields(body_for_reject)
    except AlertValidationError as e:
        raise VaultValidationError(str(e)) from e
    # fields values must be strings (selectors), not nested secret objects
    fields = body.get("fields")
    if isinstance(fields, dict):
        for _fname, fval in fields.items():
            if isinstance(fval, (dict, list)):
                try:
                    reject_secret_fields(fval, path="fields")
                except AlertValidationError as e:
                    raise VaultValidationError(str(e)) from e
    cred_id = body.get("cred_id")
    if not isinstance(cred_id, str) or not cred_id.strip():
        raise VaultValidationError("cred_id required")
    if not isinstance(fields, dict) or not fields:
        raise VaultValidationError("fields must be a non-empty object of name→selector")
    cleaned: dict[str, str] = {}
    for name, selector in fields.items():
        if not isinstance(name, str) or not name.strip():
            raise VaultValidationError("field names must be non-empty strings")
        if not isinstance(selector, str) or not selector.strip():
            raise VaultValidationError(f"selector for {name!r} must be a non-empty string")
        if len(selector) > 300:
            raise VaultValidationError(f"selector for {name!r} too long")
        cleaned[name.strip()] = selector.strip()
    out: dict[str, Any] = {"cred_id": cred_id.strip(), "fields": cleaned}
    # Optional post-inject mode (ADV-001): scrub_memory is always applied in pool;
    # pause_cdp is documented / deferred.
    mode = body.get("post_inject")
    if mode is not None:
        if mode not in ("scrub_memory", "none"):
            raise VaultValidationError(
                "post_inject must be 'scrub_memory' or 'none' "
                "(pause_cdp readback mitigation is deferred)"
            )
        out["post_inject"] = mode
    return out




def material_for_field(name: str, unlocked: dict[str, str]) -> str:
    """Map logical field name → unlocked material."""
    n = name.lower()
    if n in ("username", "user", "email", "login"):
        return unlocked.get("username", "")
    if n in ("password", "pass", "secret", "passwd"):
        return unlocked.get("secret", "")
    raise VaultValidationError(
        f"unsupported fill field {name!r}; use username|password (or aliases)"
    )
