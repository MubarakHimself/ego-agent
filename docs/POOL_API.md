# Pool HTTP API (MVP)

Transport: **localhost HTTP/JSON** via Python stdlib `ThreadingHTTPServer`.

Default base URL: `http://127.0.0.1:8755`

Agents **must not** spawn Chromium themselves — only this service launches browsers (hard K-cap).

MVP is always **isolated** mode (one process tree per Space). There is no `mode` request field.

## Config defaults

| Key | Default | Notes |
|---|---|---|
| `K` | 5 | Hard max live Chromium process trees |
| `W` | 1 | Warm idle slots after **explicit client DELETE** only |
| `idle_ttl_seconds` | 300 | Soft-evict without heartbeat (~5 min); always stops Chromium |
| `lease_hard_ttl_seconds` | 1800 | Hard lease ceiling (enforced on heartbeat / idle sweep / re-lease); always stops Chromium |
| `spaces_root` | `./data/spaces` | Space = `{spaces_root}/{space_id}/` = Chromium `--user-data-dir` |
| `cdp_base_port` | 9222 | Slot *i* uses port `9222 + i` |
| `host` / `port` | `127.0.0.1` / `8755` | API bind |

Env overrides: `EGO_POOL_MOCK=1`, `EGO_POOL_CHROME`, `EGO_POOL_SPACES_ROOT`, `EGO_POOL_K`, `EGO_POOL_W`, `EGO_POOL_PORT`, `EGO_POOL_HEADLESS=0`.

## Endpoints

### `POST /v1/leases`

```json
{
  "agent_id": "agent-1",
  "space_id": "task-42",
  "ttl_seconds": 1800
}
```

→ `200`

```json
{
  "lease_id": "…",
  "slot_id": 0,
  "agent_id": "agent-1",
  "space_id": "task-42",
  "status": "leased",
  "cdp_http_url": "http://127.0.0.1:9222",
  "cdp_ws_url": "ws://…",
  "created_at": 0,
  "expires_at": 0
}
```

→ `400` for invalid `space_id` (empty / `.` / `..` / path separators `/` `\` / NULs / unsafe) or non-integer `ttl_seconds`. Distinct ids are never rewritten — bad ids are rejected.

→ `409` when `space_id` is already leased by another agent (`{"error":"space_in_use"}`). Same `(agent_id, space_id)` while leased remains idempotent (`200`) unless the hard TTL has expired **or** the Chromium handle is dead — then the old lease is released (process stopped, `allow_warm=False`) and a fresh lease is issued (cold start).

Optional `ttl_seconds` may be shorter than `lease_hard_ttl_seconds`; requests above the ceiling are **clamped** to `lease_hard_ttl_seconds` (never reject solely for being too long).

→ `503` when pool at hard K (`{"error":"pool_full"}`) or Chromium launch fails (`{"error":"launch_failed"}`, including `OSError` / other launch exceptions).

**Warm reuse:** if a `FREE_WARM` slot already holds the requested `space_id` **and** its process is still alive, the existing process is reused (no stop/relaunch). A dead warm process is stopped and cold-started. A warm slot bound to a different Space is stopped and relaunched.

### `POST /v1/leases/{lease_id}/heartbeat`

Renews soft idle window. Agents should heartbeat every ~15–30s (including during LLM think).

→ `410` if the hard lease TTL has expired (lease is released with reason `hard_ttl_expired` and Chromium stopped before the error is returned).

→ `404` if the lease is unknown.

### `DELETE /v1/leases/{lease_id}`

Optional JSON body: `{"reason":"done"}`.

On **explicit client DELETE**: keep process as `FREE_WARM` (with `space_id` retained for reuse) if warm count &lt; W, else kill process → `FREE_COLD` (profile remains on disk under `spaces_root`).

Idle eviction (`idle_evicted`) and hard-TTL expiry (`hard_ttl_expired`) **always** stop Chromium — no FREE_WARM / no stale CDP handoff.

### `GET /v1/pool/status`

Returns `{K,W,live,warm,leased,mock,slots[…]}` including per-slot `rss_bytes` when samplable.

### `GET /healthz`

Liveness probe.

## Driver notes

- Drive leased browsers via CDP (`cdp_http_url` / DevTools WebSocket).
- Playwright `connectOverCDP` is fine for tests.
- Thin in-house CDP client is the intended production driver (not shipped in this scaffold).

## RSS sampling hook

`ego_pool.rss.sample_tree_rss(pid)` sums `/proc` VmRSS across the process tree (Linux). Returns `None` if unavailable. Exposed on slot status as `rss_bytes` for later K tuning — see README.
