"""Lease-scoped Watch evidence stills (JPEG) keyed by activity-feed seq.

Thin MVP: capture viewport JPEG on selected feed events, store under
``{artifacts_root}/leases/{lease_id}/evidence/`` with O_NOFOLLOW discipline
matching downloads. Token-gated list + frame bytes; revoke → 410. No video.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from slipstream.activity_feed import scrub_feed_detail, scrub_summary
from slipstream.downloads import (
    ArtifactNotFoundError,
    DownloadValidationError,
    KIND_EVIDENCE,
    ensure_lease_artifact_dirs,
    walk_lease_kind_dir,
)

DEFAULT_MAX_EVIDENCE = 32
_DEFAULT_AUTO = frozenset({"navigate", "confirm", "alert"})
_EV_NAME_RE = re.compile(r"^ev_(\d{8})\.jpg$")
_SAFE_REF_RE = re.compile(r"^[A-Za-z0-9._:/#()\[\] -]{1,64}$")


def evidence_max_keep() -> int:
    raw = os.environ.get("SLIPSTREAM_EVIDENCE_MAX", "").strip()
    try:
        n = int(raw) if raw else DEFAULT_MAX_EVIDENCE
    except ValueError:
        n = DEFAULT_MAX_EVIDENCE
    return max(1, min(n, 200))


def evidence_auto_kinds() -> frozenset[str]:
    """Kinds that auto-capture. ``SLIPSTREAM_EVIDENCE_AUTO=0`` disables."""
    raw = os.environ.get("SLIPSTREAM_EVIDENCE_AUTO", "").strip()
    if not raw:
        return _DEFAULT_AUTO
    if raw in ("0", "false", "off", "no"):
        return frozenset()
    if raw in ("1", "true", "on", "yes"):
        return _DEFAULT_AUTO
    return frozenset(
        p.strip().lower() for p in raw.replace(";", ",").split(",") if p.strip()
    )


def _ref_label(item: Any) -> str | None:
    if isinstance(item, str):
        label = item
    elif isinstance(item, dict):
        cand = item.get("label") or item.get("ref")
        label = cand if isinstance(cand, str) else None
    else:
        label = None
    if not label:
        return None
    cleaned = scrub_summary(label)[:64]
    return cleaned if cleaned and _SAFE_REF_RE.match(cleaned) else None


def scrub_annotation(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Public annotation only — never secrets; refs are short labels."""
    if not raw:
        return {}
    scrubbed = scrub_feed_detail(
        {k: v for k, v in raw.items() if k not in ("refs", "seq", "ts")}
    )
    out: dict[str, Any] = {}
    if isinstance(raw.get("seq"), int):
        out["seq"] = int(raw["seq"])
    if isinstance(raw.get("ts"), (int, float)):
        out["ts"] = float(raw["ts"])
    for key, lim in (("kind", 32), ("summary", 160), ("outcome", 32)):
        val = scrubbed.get(key)
        if isinstance(val, str):
            out[key] = scrub_summary(val)[:lim]
    refs_in = raw.get("refs")
    if isinstance(refs_in, list):
        refs = [r for r in (_ref_label(x) for x in refs_in[:12]) if r]
        if refs:
            out["refs"] = refs
    return out


def evidence_filename(seq: int) -> str:
    return f"ev_{int(seq):08d}.jpg"


def evidence_meta_filename(seq: int) -> str:
    return f"ev_{int(seq):08d}.json"


def _unlink_quiet(handle: Any, name: str) -> None:
    try:
        os.unlink(name, dir_fd=handle.fd)  # skylos: ignore[SKY-D215] unlinkat under held kind_fd
    except OSError:
        pass


def _prune_locked(handle: Any, *, keep: int) -> None:
    seqs = sorted(
        int(m.group(1)) for name in handle.listdir() if (m := _EV_NAME_RE.match(name))
    )
    for seq in seqs[: max(0, len(seqs) - keep)]:
        _unlink_quiet(handle, evidence_filename(seq))
        _unlink_quiet(handle, evidence_meta_filename(seq))


def _read_json_sidecar(handle: Any, seq: int) -> dict[str, Any]:
    """Best-effort scrubbed sidecar. Leaf symlink / open fail → {} (ADV-EV-002)."""
    meta_name = evidence_meta_filename(seq)
    if not handle.name_exists(meta_name):
        return {}
    try:
        fd, st = handle.open_reg(meta_name)
    except (ArtifactNotFoundError, DownloadValidationError, OSError):
        return {}
    try:
        raw = os.read(  # skylos: ignore[SKY-P401] size-capped sidecar (<=8KiB)
            fd, min(int(st.st_size), 8192)
        )
    except OSError:
        return {}
    finally:
        os.close(fd)
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return scrub_annotation(parsed) if isinstance(parsed, dict) else {}


def store_evidence_jpeg(
    artifacts_root: Path,
    lease_id: str,
    *,
    seq: int,
    jpeg: bytes,
    annotation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write JPEG + scrubbed JSON sidecar; prune to bounded retention."""
    if not isinstance(jpeg, (bytes, bytearray)) or len(jpeg) < 4:
        raise DownloadValidationError("evidence jpeg required")
    payload = bytes(jpeg)
    if payload[:2] != b"\xff\xd8" or len(payload) > 8 * 1024 * 1024:
        raise DownloadValidationError("evidence must be JPEG under 8MiB")
    seq_i = int(seq)
    if seq_i < 1:
        raise DownloadValidationError("evidence seq required")
    ensure_lease_artifact_dirs(artifacts_root, lease_id)
    name = evidence_filename(seq_i)
    meta_name = evidence_meta_filename(seq_i)
    ann = scrub_annotation(annotation)
    ann.setdefault("seq", seq_i)
    ann.setdefault("ts", time.time())
    meta_bytes = json.dumps(ann, separators=(",", ":"), sort_keys=True).encode("utf-8")
    with walk_lease_kind_dir(
        artifacts_root, lease_id, KIND_EVIDENCE, create=True
    ) as handle:
        if handle.name_exists(name):
            _unlink_quiet(handle, name)
        if handle.name_exists(meta_name):
            _unlink_quiet(handle, meta_name)
        handle.excl_create_write(name, payload)
        handle.excl_create_write(meta_name, meta_bytes)
        _prune_locked(handle, keep=evidence_max_keep())
    return {
        "id": f"ev_{seq_i:08d}",
        "seq": seq_i,
        "filename": name,
        "bytes": len(payload),
        "kind": "evidence",
        "annotation": ann,
    }


def list_evidence_markers(
    artifacts_root: Path,
    lease_id: str,
) -> dict[str, Any]:
    """List evidence markers (seq + scrubbed annotation); no absolute paths.

    ADV-EV-002: bad leaf (sidecar symlink / open fail) is skipped or treated as
    a marker without annotation — never raises ArtifactNotFoundError for the
    whole list (API would 404).
    """
    markers: list[dict[str, Any]] = []
    try:
        with walk_lease_kind_dir(
            artifacts_root, lease_id, KIND_EVIDENCE, create=False
        ) as handle:
            for name in handle.listdir():
                m = _EV_NAME_RE.match(name)
                if not m:
                    continue
                seq_i = int(m.group(1))
                # Skip non-regular JPEG leaf (symlink / open fail).
                try:
                    fd, _st = handle.open_reg(name)
                    os.close(fd)
                except (ArtifactNotFoundError, DownloadValidationError, OSError):
                    continue
                row: dict[str, Any] = {
                    "seq": seq_i,
                    "id": f"ev_{seq_i:08d}",
                    "filename": name,
                }
                ann = _read_json_sidecar(handle, seq_i)
                for key, val in ann.items():
                    if key != "seq":
                        row[key] = val
                if ann:
                    row["annotation"] = ann
                markers.append(row)
    except (DownloadValidationError, ArtifactNotFoundError):
        markers = []
    markers.sort(key=lambda r: int(r["seq"]))
    return {"lease_id": lease_id, "markers": markers, "count": len(markers)}


def read_evidence_jpeg(
    artifacts_root: Path,
    lease_id: str,
    seq: int,
) -> bytes:
    """Return JPEG bytes for feed seq (O_NOFOLLOW openat)."""
    seq_i = int(seq)
    if seq_i < 1:
        raise DownloadValidationError("evidence seq required")
    name = evidence_filename(seq_i)
    with walk_lease_kind_dir(
        artifacts_root, lease_id, KIND_EVIDENCE, create=False
    ) as handle:
        if not handle.name_exists(name):
            raise ArtifactNotFoundError("evidence not found")
        fd, st = handle.open_reg(name)
        try:
            return os.read(  # skylos: ignore[SKY-P401] size-capped evidence JPEG via fstat
                fd, int(st.st_size)
            )
        finally:
            os.close(fd)


def clear_lease_evidence(artifacts_root: Path, lease_id: str) -> None:
    """Best-effort delete all evidence stills for a lease (revoke / release)."""
    try:
        with walk_lease_kind_dir(
            artifacts_root, lease_id, KIND_EVIDENCE, create=False
        ) as handle:
            for name in list(handle.listdir()):
                if name.startswith("ev_") and name.endswith((".jpg", ".json")):
                    _unlink_quiet(handle, name)
    except DownloadValidationError:
        return
