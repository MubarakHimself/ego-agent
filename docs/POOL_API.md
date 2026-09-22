# Pool HTTP API (MVP)

Transport: **localhost HTTP/JSON** via Python stdlib `ThreadingHTTPServer`.

**No MCP** — this HTTP API is the external surface for agents/tools. The agent-facing **`slipstream` CLI** (`lease` / `heartbeat` / `alert` / `release` / `status` / `doctor`) is another client of these same endpoints (see `skills/slipstream/SKILL.md`; alias intent `slipstream-browser`). There is no MCP server in this project.

Default base URL: `http://127.0.0.1:8755` (override for CLI clients with `SLIPSTREAM_URL` or `--url`).

Agents **must not** spawn Chromium themselves — only this service launches browsers (hard K-cap).

MVP is always **isolated** mode (one process tree per Space). There is no `mode` request field.

## Config defaults

| Key | Default | Notes |
|---|---|---|
| `K` | 5 | Hard max live Chromium process trees |
| `W` | 1 | Warm idle slots after **explicit client DELETE** only |
| `idle_ttl_seconds` | 300 | Soft-evict without heartbeat (~5 min); always stops Chromium; **skipped** while lease is `awaiting_human` |
| `lease_hard_ttl_seconds` | 1800 | Hard lease ceiling (enforced on heartbeat / idle sweep / re-lease); always stops Chromium |
| `spaces_root` | `./data/spaces` | Space = `{spaces_root}/{space_id}/` = Chromium `--user-data-dir` |
| `cdp_base_port` | 9222 | Slot *i* uses port `9222 + i` |
| `host` / `port` | `127.0.0.1` / `8755` | API bind |

Env overrides: `SLIPSTREAM_MOCK=1`, `SLIPSTREAM_CHROME`, `SLIPSTREAM_SPACES_ROOT`, `SLIPSTREAM_K`, `SLIPSTREAM_W`, `SLIPSTREAM_PORT`, `SLIPSTREAM_HEADLESS=0`.

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

### `POST /v1/leases/{lease_id}/alerts`

Raise a lease-scoped alert. Ship-now events: `need_human`, `task_done`.

```json
{
  "event": "need_human",
  "reason": "captcha",
  "detail": "short human-safe string",
  "task_id": "optional",
  "ttl_s": 300
}
```

Request must **not** include `watch_url` or `status` — both are server-derived.

→ `200` harness envelope:

```json
{
  "alert": {
    "event": "need_human",
    "event_id": "…",
    "ts": "2026-09-22T17:30:00+03:00",
    "lease_id": "…",
    "space_id": "…",
    "task_id": null,
    "reason": "captcha",
    "detail": "…",
    "status": "awaiting_human",
    "watch_url": "http://127.0.0.1:8755/v1/leases/…/watch",
    "ttl_s": 300,
    "outcome": null
  },
  "harness": {
    "action": "pause",
    "agent_paused": true,
    "lease_kept": true,
    "lease_released": false,
    "captain_message": "Need human (captcha) on lease … — [Watch](…) · [Take-over](…)",
    "watch_url": "…",
    "takeover_url": "…?mode=takeover",
    "idempotent": false
  }
}
```

| Event | Lease effect | Harness `action` |
|-------|--------------|------------------|
| `need_human` | **Keep** lease / Chromium; mark `awaiting_human` | `pause` |
| `task_done` | Notify once, then **release** (warm rules like DELETE) | `continue` |

`task_done` body uses `outcome: {"ok": bool, "summary": "…"}` (no secrets). A second `task_done` for the same `lease_id` returns the prior envelope with `harness.idempotent=true` (safe; no double-release error).

→ `400` `invalid_alert` for unknown event, bad reason, client-supplied `watch_url`/`status`, or **secret-like fields** (`cookie`, `password`, `token`, `private_key`, `jwt`, `bearer`, `access_key`, `credential`, `authorization`, … — including nested keys; token-boundary match so `secretary_note` is allowed).

→ `404` if the lease is unknown (except idempotent `task_done` replay). After a completed `task_done` the lease is released, so a later `need_human` for that `lease_id` is **`404`** (not `409`).

**Refuse in payload keys:** cookies, passwords, tokens, JWTs, bearers, private/access keys, auth headers, credential-store dumps, secret-bearing paths, raw CDP auth. Matching is exact / token-boundary (not bare substring).

**Trust boundary (free-text):** `detail` and `outcome.summary` are caller-controlled strings. Callers must not put secrets there. The server lightly scrubs `password=` / `cookie=` / `token=` patterns to `[REDACTED]` but this is defense-in-depth, not a guarantee — treat alert text as untrusted for secret storage.

**Server-derived fields:** `status` (`awaiting_human` / `done` / `failed`) and `watch_url` (short-TTL local handoff placeholder until live pair-browse UI exists). Soft-idle eviction is skipped while `lease.status == awaiting_human` (hard TTL still applies).

**In-memory alert state:** `_alert_log` and `_task_done_envelopes` are **process-lifetime** maps (survive lease release for idempotent `task_done` replay; cleared on pool shutdown / process exit). Bounded LRU eviction is deferred.

### Credential vault (bound to Space)

Vault root is **outside** Space `user-data-dir` (`SLIPSTREAM_VAULT_ROOT` / `VAULT_ROOT`, default `./data/vault`). Space profiles keep **session cookies only** — never a password dump.

Prefer OS keyring (`keyring` optional extra). Fallback: Fernet-encrypted blobs keyed by keyring master or `SLIPSTREAM_VAULT_KEY` (**tests / CI only**). Mock mode uses in-memory secrets.

```http
POST /v1/spaces/{space_id}/credentials/bind
{"label":"work-github","origin":"https://github.com","username":"…","secret":"…"}
→ {"cred_id":"cred_…","label":"…","origin":"…","bound":true}

POST /v1/spaces/{space_id}/credentials/{cred_id}/unbind
→ {"unbound":true,"cred_id":"…","space_id":"…"}

GET  /v1/spaces/{space_id}/credentials
→ {"items":[{"cred_id":"…","label":"…","origin":"…","has_secret":true}],"space_id":"…"}
# metadata only — never secret, never username, never cookie jar

POST /v1/leases/{lease_id}/credentials/fill
{"cred_id":"…","fields":{"username":"#user","password":"#pass"}}
→ {"ok":true,"filled":["username","password"],"cred_id":"…","lease_id":"…"}
# pool unlocks vault + CDP inject; agent sees ok/labels only
```

**Refuse:** `GET/POST …/credentials/secret|cookies|storage_state|dump` → `404 refused`. Fill body must not include `password`/`secret`/`token`/`cookie` keys — only `cred_id` + selectors.

**Login / 2FA:** if the agent cannot complete auth after fill (or before bind), raise `need_human` with `reason=login` (or `other` for CAPTCHA/2FA). Lease stays warm; captain Watch / Take-over. Never paste passwords into chat/alerts.

CLI:

```bash
slipstream cred bind --space-id "$S" --label work-gh --origin https://github.com \
  --username "$USER" --secret "$PASS"   # captain/local only
slipstream cred list --space-id "$S"
slipstream cred fill --lease-id "$L" --cred-id "$C" \
  --fields '{"username":"#login","password":"#password"}'
slipstream cred unbind --space-id "$S" --cred-id "$C"
```

Env: `SLIPSTREAM_VAULT_ROOT` / `VAULT_ROOT`, `SLIPSTREAM_VAULT_KEY` (tests only), optional extras `pip install 'slipstream[vault]'` / `'slipstream[cdp]'`.

### `DELETE /v1/leases/{lease_id}`

Optional JSON body: `{"reason":"done"}`.

On **explicit client DELETE**: keep process as `FREE_WARM` (with `space_id` retained for reuse) if warm count &lt; W, else kill process → `FREE_COLD` (profile remains on disk under `spaces_root`).

Idle eviction (`idle_evicted`) and hard-TTL expiry (`hard_ttl_expired`) **always** stop Chromium — no FREE_WARM / no stale CDP handoff.

### `GET /v1/pool/status`

Returns `{K,W,live,warm,leased,mock,slots[…]}` including per-slot `rss_bytes` when samplable.

### `GET /healthz`

Liveness probe.

## Concurrency

The pool uses a single `threading.RLock` for slot / lease bookkeeping only.
**Chromium `stop` / `launch` (and any CDP wait) run outside the lock.** While a
slot is `STARTING`, other agents can still heartbeat existing leases, read
`GET /v1/pool/status`, and lease *different* `space_id`s. Space exclusivity
still applies: a second lease for the same `space_id` gets `409` while the
first is `LEASED` or `STARTING`. Callers see no new request fields — semantics
are transparent aside from lower latency under concurrent load.

## CLI client

Console script / module entry (`slipstream` or `python -m slipstream`):

| Command | HTTP |
|---|---|
| `slipstream serve` | starts this API server |
| `slipstream lease --agent-id … --space-id …` | `POST /v1/leases` |
| `slipstream heartbeat --lease-id …` | `POST /v1/leases/{id}/heartbeat` |
| `slipstream alert need-human\|done --lease-id …` | `POST /v1/leases/{id}/alerts` |
| `slipstream release --lease-id …` | `DELETE /v1/leases/{id}` |
| `slipstream status` | `GET /v1/pool/status` |
| `slipstream doctor [--json]` | local preflight + `GET /healthz` (Chrome/CDP/spaces/skill) |

Uses stdlib `urllib`. Env: `SLIPSTREAM_URL` for base URL. Non-zero exit + stderr on HTTP errors.
`doctor` also probes an ephemeral Chrome CDP endpoint and checks Spaces root + skill path (no lease required).

## Driver notes

- Drive leased browsers via CDP (`cdp_http_url` / DevTools WebSocket).
- Playwright `connectOverCDP` is fine for tests.
- Thin in-house CDP client is the intended production driver (not shipped in this scaffold).

## RSS sampling hook

`slipstream.rss.sample_tree_rss(pid)` sums `/proc` VmRSS across the process tree (Linux). Returns `None` if unavailable. Exposed on slot status as `rss_bytes` for later K tuning — see README.
