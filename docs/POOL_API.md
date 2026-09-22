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
| `vault_root` | `./data/vault` | Credential vault (**must** be outside `spaces_root`; fail-closed at config/pool init) |
| `artifacts_root` | `./data/artifacts` | Lease downloads/uploads (**must** be outside `spaces_root` + `vault_root`) |
| `cdp_base_port` | 9222 | Operator-internal Chromium bind; **not** on public status/lease unless `SLIPSTREAM_EXPOSE_RAW_CDP=1`. Do **not** derive CDP from public JSON or `slot_id` (no `9222+slot_id` recipe) |
| `host` / `port` | `127.0.0.1` / `8755` | API bind |

Env overrides: `SLIPSTREAM_MOCK=1`, `SLIPSTREAM_CHROME`, `SLIPSTREAM_SPACES_ROOT`, `SLIPSTREAM_VAULT_ROOT` / `VAULT_ROOT`, `SLIPSTREAM_ARTIFACTS_ROOT`, `SLIPSTREAM_K`, `SLIPSTREAM_W`, `SLIPSTREAM_PORT`, `SLIPSTREAM_HEADLESS=0`, `SLIPSTREAM_CONFIRM_TTL` (pending confirm + unused grant seconds, default 60), `SLIPSTREAM_EXPOSE_RAW_CDP=1` (include cdp_* URLs **and** `cdp_port`/`cdp_base_port`/`chromium_pid` on lease/status JSON; raw CDP nav/eval honor-system — do not derive CDP from default public JSON), `SLIPSTREAM_ALLOWED_DOMAINS` (comma/space-separated top-frame host patterns; empty/unset = unrestricted unless Space/lease lockdown applies), `SLIPSTREAM_CONTENT_BOUNDARIES=1` (wrap page-derived skill/CLI echoes in nonce markers).

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
  "created_at": 0,
  "expires_at": 0
}
```

**Raw CDP tip:** omitted by default (Firstmate B / ADV-PL-001-BYPASS-RAW-CDP-PORT). Set `SLIPSTREAM_EXPOSE_RAW_CDP=1` to include `cdp_http_url` / `cdp_ws_url` / `cdp_port` / `chromium_pid` on lease and status slot JSON, and `cdp_base_port` on pool status. Without the escape hatch, **do not derive CDP from public JSON** — status/lease omit ports and pid (no `9222+slot_id` reconstruct path; do not treat `slot_id` as a CDP address). Prefer pool HTTP `navigate` / `eval` / `credentials/fill` + act/confirm so the ladder is server-enforced. With the escape hatch, raw CDP nav/eval is **honor-system** (vault fill stays pool-only).

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
    "watch_url": "http://127.0.0.1:8755/v1/leases/…/watch?token=…",
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

→ `400` `invalid_alert` for unknown event, bad reason, client-supplied `watch_url`/`status`, or **secret-like fields** (`cookie`, `password`, `token`, `private_key`, `jwt`, `bearer`, `access_key`, `credential`, `authorization`, … — including nested keys and **camelCase** compounds like `accessToken` / `sessionToken` / `clientSecret` / `myPassword` / `cookieJar` — ADV-META-001; token-boundary match so `secretary_note` is allowed). Free-text `detail`/`summary` scrub extends the same stems (`jwt=` / `access_key=` / … — ADV-PL-002).

→ `404` if the lease is unknown (except idempotent `task_done` replay). After a completed `task_done` the lease is released, so a later `need_human` for that `lease_id` is **`404`** (not `409`).

**Refuse in payload keys:** cookies, passwords, tokens, JWTs, bearers, private/access keys, auth headers, credential-store dumps, secret-bearing paths, raw CDP auth. Matching is exact / token-boundary (not bare substring).

**Trust boundary (free-text):** `detail` and `outcome.summary` are caller-controlled strings. Callers must not put secrets there. The server lightly scrubs `password=` / `cookie=` / `token=` / `secret=` / `authorization=` / `bearer=` patterns to `[REDACTED]` but this is defense-in-depth, not a guarantee — treat alert text as untrusted for secret storage.

**Server-derived fields:** `status` (`awaiting_human` / `done` / `failed`) and `watch_url` (short-TTL **tokenized** local Watch — observe-only until Take-over confirm enables pair-browse). Soft-idle eviction is skipped while `lease.status == awaiting_human` (hard TTL still applies).

### Live Watch + thin pair-browse v1

### Watch activity feed (thin dock)

Lease-scoped append-only event log beside the Watch JPEG (CTO cut `activity-feed-007` / EgoRuntime `activity-feed-001`).

- `GET /v1/leases/{id}/watch/events?token=…&after_seq=0` — JSON `{lease_id, events:[{seq,ts,kind,summary,outcome,detail}]}`
- Kinds: `navigate`, `click`, `type`, `fill`, `alert`, `confirm`, `captcha` (timestamp + safe summary + outcome)
- Same watch token TTL/revoke as Watch HTML/frame — stale/revoked → `410`; bad token → `401`
- Secrets redacted: no cookies/passwords/tokens/vault/CDP auth; `type`/`fill` expose lengths/labels only
- Bounded ring buffer (no video/ffmpeg). Soft browse stays free; feed is observe opacity for captain
- Reuses alerts bus for `need_human` / `task_done` — **no** second notification path

### Dual timeline / thin session scrubber

ui-peers steal #8 / EgoRuntime `session-replay-001`: live Watch viewport clock vs activity-feed event timestamps, with a thin scrubber that seeks/highlights feed markers (JPEG stays the live frame for v1).

- `GET /v1/leases/{id}/watch/timeline?token=…` — JSON `{lease_id, markers:[{seq,ts,kind,summary}], count, first_ts, last_ts, latest_seq, wall_ts}`
- Same token TTL/revoke → `410`; bad token → `401`; summaries already scrubbed
- Watch HTML shows dual clocks + range scrubber; seek highlights the matching Activity row — **not** full video replay


On `need_human` the pool mints an unguessable token and returns:

- `watch_url` = `/v1/leases/{id}/watch?token=…` (TTL from `ttl_s`, default 300s)
- `takeover_url` = same + `&mode=takeover` (Confirm Take-over → pause agent + enable exclusive pair-browse)

| Method | Path | Result |
|--------|------|--------|
| `GET` | `/v1/leases/{id}/watch?token=…` | HTML shell embedding JPEG frame; observe-only until confirm |
| `GET` | `/v1/leases/{id}/watch/frame?token=…` | `image/jpeg` viewport via CDP `Page.captureScreenshot` (mock JPEG under `SLIPSTREAM_MOCK`) |
| `GET` | `/v1/leases/{id}/watch/timeline?token=…` | Dual-timeline markers JSON for feed scrubber (same TTL/revoke) |
| `GET` | `/v1/leases/{id}/watch/evidence?token=…` | Evidence markers JSON (seq-keyed stills); `&seq=N` → JPEG |
| `POST` | `/v1/leases/{id}/watch/confirm?token=…` | Confirm Take-over → `{action: pause, agent_paused: true, input_enabled: true, lease_kept: true}` |
| `POST` | `/v1/leases/{id}/watch/input?token=…` | Bridge click/type/key/scroll into leased CDP (requires confirm; exclusive pause) |
| `POST` | `/v1/leases/{id}/watch/cede?token=…` | Disable input, clear pause → `{action: continue, agent_paused: false, input_enabled: false}`; lease stays until `task_done`. Requires prior Confirm (`takeover_confirmed` / `input_enabled`); otherwise `400` nothing to cede |

**Input body (JSON):** `{ "kind": "click"|"type"|"key"|"scroll", … }` — click needs `x,y`; type needs `text` (≤64); key needs `key`; scroll needs `deltaX`/`deltaY`. Response is a scrubbed ack (`ok` + `kind`) — **never** echoes typed text. Secret-like fields (`password`, `cookie`, `token`, …) are refused (`400`). Mock recorder stores `text_len` only (never full typed text).

**Gate:** observe-only / post-cede / unpaused → `403` on `/watch/input`. Agent stays `awaiting_human` (paused exclusive) from confirm until **Cede or Watch TTL** (both clear `input_enabled` / `takeover_confirmed` and set `lease.status=leased` so the agent can resume). Concurrent cede/revoke/TTL during in-flight CDP input bumps an `input_epoch`; post-CDP revalidation fails → `410` (no success ack).

→ `401` missing/wrong/cross-lease token (including `/watch/input` and `/watch/cede`) · `403` input while observe-only / after Cede · `400` bad input **or cede without confirm** · `410` after `task_done`, lease release, Watch TTL expiry, concurrent cede/epoch race mid-input, or revoke/identity race mid-frame · `404` unknown lease / no watch session.

**Credential-in-URL (v1):** `watch_url` carries `?token=…` — treat the whole URL as a **screen-share secret** (anyone with the link can see live viewport screenshots for the TTL). Do not paste into group chat / tickets / logs. **HttpOnly cookie migration** (token out of the URL / HTML) is deferred — wait for Firstmate / captain before implementing.

**Sensitive frames (ADV-WATCH-007):** `/watch/frame` JPEG bytes are screenshots of whatever the leased Chromium is showing (may include PII, account UI, partial secrets on-screen). Responses use `Cache-Control: no-store`, `X-Frame-Options: DENY`, and `Content-Security-Policy: frame-ancestors 'none'` on Watch HTML / frame / confirm. After CDP capture the pool re-validates `session.revoked` + lease/slot/CDP identity before returning bytes (concurrent `task_done` / port reuse → `410`, never a mock soft-fallback after auth).

**Never on the Watch page / frame:** cookies, passwords, tokens-as-JSON, vault dumps, raw CDP auth / `cdp_http_url` / debugger WS. CDP `webSocketDebuggerUrl` must be loopback `ws`/`wss` before connect.

**Chromium `--remote-allow-origins=*` (v1 residual risk):** pool launch still passes `*` so localhost CDP WebSockets (Watch screenshot + cred fill) work. On a shared host this widens who may attach to the debugging port if they can reach loopback. **Tighten** (explicit origin allowlist) is deferred — wait for Firstmate. Mitigations today: loopback-only CDP bind, pool API on `127.0.0.1`, WS debugger URL allowlist (loopback ws/wss only).

**Defer:** simultaneous human+agent drive, session replay, cloud overflow, MCP wrapper, full WS dashboard polish, token→HttpOnly cookie, `--remote-allow-origins` tighten.

**In-memory alert state:** `_alert_log`, `_task_done_envelopes`, and `_watches` are **process-lifetime** maps (survive lease release for idempotent `task_done` / revoked-watch `410`; cleared on pool shutdown / process exit). Bounded LRU eviction is deferred.


### Evidence panel (annotated / timed stills)

Thin evidence-first Watch side panel (no video):

- Auto-capture JPEG stills on selected feed kinds (`navigate` / `confirm` / `alert` by default) keyed by feed `seq`
- Stored under `{artifacts_root}/leases/{lease_id}/evidence/` with the same O_NOFOLLOW openat discipline as downloads (bounded retention)
- `GET /v1/leases/{id}/watch/evidence?token=…` → `{lease_id, markers:[{seq,ts,kind,summary,refs,id}], count}`
- `GET /v1/leases/{id}/watch/evidence?token=…&seq=N` → `image/jpeg` (token / TTL / revoke → 410 like Watch frame)
- Watch HTML: Evidence aside; click a feed row (or scrub to a marker) to show the matching still + scrubbed refs
- Annotations are scrubbed (never cookies / passwords / tokens / vault / CDP). Env: `SLIPSTREAM_EVIDENCE_AUTO` (kinds or `0`), `SLIPSTREAM_EVIDENCE_MAX` (default 32)


### Permission ladder (confirm-actions — server-enforced)

Gate irreversible/sensitive acts **before** CDP. Soft browse
(snapshot / click / scroll / wait) stays free. Categories:

| Category | Intent | CDP enforcement |
|----------|--------|-----------------|
| `fill` | credentials fill (`POST …/credentials/fill`) | **refused** without prior confirm |
| `eval` | `Runtime.evaluate` / script (`POST …/eval`) | **refused** without prior confirm |
| `download` | file download | act/confirm (artifact paths separately gated) |
| `upload` | file upload / `DOM.setFileInputFiles` | act/confirm (upload drop separately gated) |
| `nav_irreversible` | all pool `POST …/navigate` (top-frame) | **refused** without prior confirm |

```http
POST /v1/leases/{lease_id}/actions
{"category":"eval","summary":"probe document.title"}
→ 202
{
  "status": "confirmation_required",
  "confirm_id": "c_…",
  "category": "eval",
  "summary": "probe document.title",
  "lease_id": "…",
  "expires_at": "2026-09-22T21:00:00+03:00",
  "alert": { "event":"need_human", "kind":"confirmation_required", "confirm_id":"c_…",
             "reason":"confirmation_required", "watch_url":"…", "status":"awaiting_human", … },
  "harness": { "action":"pause", "agent_paused":true, "lease_kept":true,
               "watch_url":"…", "takeover_url":"…" }
}
```

Sibling **`need_human`** on the **same alerts bus** (`kind=confirmation_required` +
`confirm_id`) — captain gets Watch / Take-over URL. No second notification path.
Lease stays warm (`awaiting_human`); soft-idle eviction skipped (hard TTL still applies).

```http
POST /v1/leases/{lease_id}/confirmations/{confirm_id}
{"action":"confirm"}   # or "deny"
→ 200 {"status":"confirmed","decision":"allow", …}   # once
→ 200 {"status":"denied","decision":"deny", …}
→ 410 confirmation_gone (expired / already resolved)
```

CLI convenience (same resolve): `POST /v1/confirmations/{confirm_id}` with `{action}`.

**TTL:** pending confirmations auto-deny after ~60s (`SLIPSTREAM_CONFIRM_TTL`, default 60). Unused one-shot **grants** use the same TTL (confirm then long delay does not allow forever).
**Non-TTY:** `slipstream act … --confirm-interactive` auto-denies when stdin is not a TTY.
**Secrets:** reuse alerts denylist/scrub (incl. `jwt=` / `access_key=` / `private_key=` / `api_key=` / `passwd=` — ADV-PL-002) — never put passwords/cookies/tokens in `summary`.

**Server-enforced (ADV-PL-001) on pool HTTP:** `POST /actions` → captain `confirm` grants a **one-shot** allowance for that category on the lease. Pool helpers for **fill / eval / navigate** check preconditions (cdp present, origin for fill, domain allowlist for nav) **before** consuming; they consume that allowance or return **403** `confirmation_required` with **no CDP side-effect**. Post-consume CDP failure **refunds** the grant. A second gated act without a fresh confirm is refused (re-gate). Deny / pending TTL / unused-grant TTL stay fail-closed. Soft browse remains free. Domain allowlist on navigate still applies when set.

**Raw CDP escape hatch:** `SLIPSTREAM_EXPOSE_RAW_CDP=1` puts `cdp_http_url` / `cdp_ws_url` / `cdp_port` / `chromium_pid` (and status `cdp_base_port`) back on wire JSON. On that path, Playwright/`connect_over_cdp` nav/eval are **honor-system** (not consume_once). Vault credential materialization remains pool `credentials/fill` only. Default public JSON is not a CDP map — do not reconstruct endpoints from it.

```http
POST /v1/leases/{id}/credentials/fill   # needs fill confirm
POST /v1/leases/{id}/eval {"expression":"document.title"}  # needs eval confirm
POST /v1/leases/{id}/navigate {"url":"https://…"}  # needs nav_irreversible confirm
→ 403 {"error":"confirmation_required","category":"fill|eval|nav_irreversible",…}
```

**Defer:** once/always/never policy matrix, Comet UI.

CLI:

```bash
slipstream act --lease-id "$L" --category fill --summary "fill login form"
slipstream act --lease-id "$L" --category eval --summary "probe title"
# → confirmation_required JSON (exit 0); then:
slipstream confirm c_…
slipstream deny c_…
# interactive (TTY prompt; Non-TTY → deny):
slipstream act --lease-id "$L" --category eval --summary "…" --confirm-interactive
```

### Domain allowlist (top-frame navigate)

Pattern-steal Browserbase `allowedDomains` / agent-browser domain allowlist.

When an allowlist is set, **top-frame** navigations outside the list are **refused** (`403 domain_not_allowed`). **No allowlist configured anywhere = unrestricted.**

Sources (narrowing only): effective = **Space ∩ config**, then lease may only **narrow** further. Lease `allowed_domains=[]` **inherits** parent (does **not** clear Space/config lockdown). Lease patterns outside Space∩config are **rejected** (`400 invalid_allowed_domains`).

When allowlist is active: only `http`/`https` with a host; `file:` / `javascript:` / `data:` / scheme-relative `//…` refused. Backslash / `%5C` in authority and userinfo are refused (parser differential).

```http
PUT /v1/spaces/{space_id}
{"allowed_domains":["example.com","*.example.org"]}

POST /v1/leases
{"agent_id":"a1","space_id":"task-42","allowed_domains":["github.com"]}

POST /v1/leases/{lease_id}/navigate
{"url":"https://www.example.com/path"}
→ 200 {"ok":true,"url":"…","matched":true,…}
→ 403 {"error":"domain_not_allowed","host":"evil.example",…}
```

Patterns: bare `example.com` matches itself + subdomains (Browserbase-style); `*.example.com` matches bare + subdomains. Non-`http(s)` schemes are **not** allowed when an allowlist is active (fail-closed).

**v1 limitation (like BB experimental):** iframe / subframe loads and subresource requests (scripts, XHR, images) are **not** blocked. WebRTC/UDP containment deferred.

CLI: `slipstream navigate --lease-id ID --page-url URL` · `slipstream lease … --allowed-domains a.com,b.com` · `slipstream spaces set --space-id S --allowed-domains a.com`.

`nav_irreversible` with a body `url` outside the allowlist is also refused (`403`) before minting confirmation.

### Content-boundary markers (thin)

agent-browser `--content-boundaries` pattern: nonce-wrapped markers separate untrusted page output from trusted tool output in skill/CLI echoes (prompt-injection hygiene).

Enable: `SLIPSTREAM_CONTENT_BOUNDARIES=1`. Helper: `slipstream.boundaries.wrap_page_content(text, origin=…)`. Minimal — not a full sandbox.

### Credential vault (bound to Space)

### Credential vault (bound to Space)

Vault root is **outside** Space `user-data-dir` (`SLIPSTREAM_VAULT_ROOT` / `VAULT_ROOT`, default `./data/vault`). Pool init **fail-closes** if `vault_root` resolves inside (or equal to) `spaces_root`. Space profiles keep **session cookies only** — never a password dump. Vault dirs/files use POSIX `0700` / `0600` when the OS supports chmod.

Prefer OS keyring (`keyring` optional extra). Fallback: Fernet-encrypted blobs keyed by keyring master or `SLIPSTREAM_VAULT_KEY` (**mock/tests only**, or set `SLIPSTREAM_ALLOW_VAULT_KEY=1`). Mock mode uses in-memory secrets.

**Trust boundary (API / LLM never plaintext):** bind accepts secrets in the HTTP body to the **pool process only**. List / fill / unbind responses, skill returns, and LLM context never include username or secret material — only ids, labels, origins, and `ok`/`filled` labels. Agents must not re-read filled DOM values back into prompts. Optional fill `post_inject: "scrub_memory"` (default behavior) zeroes process buffers after CDP inject; full pause-CDP / anti-readback is deferred.

**Auth (v1 loopback-trust):** bind/unbind are trusted on the loopback API bind (`127.0.0.1`). There is **no** captain-token yet — do not expose the pool port beyond loopback. Captain-token / ACL gate is later.

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
# → 400 origin mismatch when page location.href origin ≠ cred.origin
```

**Refuse:** `GET/POST …/credentials/secret|cookies|storage_state|dump` → `404 {"error":"refused"}`. Fill body must not include secret-like keys (recursive; expanded denylist) — only `cred_id` + selectors (and optional `post_inject`).


### Login-once + signed-in badge

Thin Browserbase-style **login once into a Space profile**, then show a **signed-in** metadata badge — **never** free-read cookies to the agent.

**Persist is automatic:** Space = `{spaces_root}/{space_id}/` = Chromium `--user-data-dir`. After a human logs in via Watch/Take-over, session cookies stay in that profile for later leases of the same Space. Vault fill stays separate.

**Flow**

1. Agent raises `need_human` with `reason=login` **or** `POST /v1/spaces/{space_id}/login-once` (creates lease + need_human).
2. Captain opens `watch_url` → Confirm Take-over → human signs in in the live viewport.
3. Cede (return drive) **or** mark badge: `POST /v1/spaces/{space_id}/signed-in` (or Watch form **Mark Space signed-in**).
4. Later leases of that Space reuse the profile; list/lease/Watch show `signed_in` (+ optional `signed_in_host`).

```http
POST /v1/spaces/{space_id}/login-once
{"agent_id":"agent-1","detail":"sign in to github","host":"github.com","ttl_s":300}
→ 200 {lease, alert, harness, flow:"login_once", next:"…", host_hint?}

POST /v1/spaces/{space_id}/signed-in
{"signed_in":true,"host":"github.com"}
→ 200 {space_id, signed_in:true, signed_in_host?, user_metadata, persist_note}

POST /v1/spaces/{space_id}/signed-in
{"signed_in":false}
→ 200 {space_id, signed_in:false, …}

POST /v1/leases/{lease_id}/watch/mark-signed-in?token=…
# form or JSON; marks the lease's Space (same badge fields).
# Requires Take-over confirm + need_human reason=login (ADV-LOGIN-002).
```

`GET /v1/spaces` / `GET /v1/leases` include `signed_in` (+ `signed_in_host` when set). Watch header shows a **signed-in** chip. `user_metadata` mirrors `signed_in=true` / `signed_in_host` for `q=` filters.

**Refuse:** still no cookie / `storage_state` / secret free-read (`…/credentials/cookies|secret|dump` → `404 refused`). Mark body must not include secret-like keys.

CLI: `slipstream spaces login-once …`, `slipstream spaces signed-in --space-id S [--host …] [--clear]`.

**Login / 2FA:** if the agent cannot complete auth after fill (or before bind), raise `need_human` with `reason=login` (or `other` for CAPTCHA/2FA). Lease stays warm; captain Watch / Take-over. Never paste passwords into chat/alerts.

CLI (`cred` subcommands — also listed under CLI client below):

```bash
# Prefer env/file/prompt — bare --secret needs SLIPSTREAM_ALLOW_SECRET_ARGV=1
slipstream cred bind --space-id "$S" --label work-gh --origin https://github.com \
  --username "$USER" --secret-env SLIPSTREAM_BIND_SECRET
# or: --secret-file /path/to/secret   or: --prompt
slipstream cred list --space-id "$S"
slipstream cred fill --lease-id "$L" --cred-id "$C" \
  --fields '{"username":"#login","password":"#password"}'
slipstream cred unbind --space-id "$S" --cred-id "$C"
```

Env: `SLIPSTREAM_VAULT_ROOT` / `VAULT_ROOT`, `SLIPSTREAM_VAULT_KEY` (mock/tests; or `SLIPSTREAM_ALLOW_VAULT_KEY=1`), `SLIPSTREAM_ALLOW_SECRET_ARGV=1`, optional extras `pip install 'slipstream[vault]'` / `'slipstream[cdp]'`.


### CAPTCHA chips (ui-peers §5.11)

Thin solve-status chips on the alerts/activity bus (Browserbase-style
`captcha_solving_started|finished|failed`). **No paid captcha-solve SaaS** —
optional client stub / detect hook reports events. Unsolved (explicit `failed`
or timeout) escalates to **`need_human`** (`reason=captcha`) with Watch /
Take-over via the existing alerts spine.

```
POST /v1/leases/{lease_id}/captcha
{
  "event": "started" | "finished" | "failed",
  // aliases: captcha_solving_started|finished|failed
  "detail": "optional non-secret note",
  "provider": "optional stub label",
  "timeout_s": 60
}
→ 200 {
  "lease_id",
  "captcha": { "state": "solving|solved|failed|escalated", "event", "detail",
               "timeout_s", "escalated", "started_at?", "timeout_remaining_s?" },
  "alert"?:   { …need_human payload… },   // only on fail / timeout escalate
  "harness"?: { …pause + watch_url + takeover_url… }
}
```

| Event | State | need_human? |
|-------|-------|-------------|
| `started` / `captcha_solving_started` | `solving` | No — chip on Watch/activity |
| `finished` / `captcha_solving_finished` | `solved` | **No** |
| `failed` / `captcha_solving_failed` | `failed` → `escalated` | **Yes** (`reason=captcha`) — **not** if already `solved` (late failed ignored) |
| timeout while `solving` (`SLIPSTREAM_CAPTCHA_TIMEOUT`, default 60s; heartbeat / Watch) | `failed` → `escalated` | **Yes** — aborted if concurrent `finished`→`solved` (re-check under lock) |

Secrets refused (alerts denylist) plus captcha `detail` scrubs unlabeled JWT-like /
`sk-` / `AKIA` / `ghp_` shapes. Chip/banner HTML escapes dynamic provider/detail.
Watch HTML shows a high-salience chip/banner when state is solving or
failed/escalated. Activity feed kind `captcha`. CLI:
`slipstream captcha started|finished|failed --lease-id …`.


### Downloads / uploads as session artifacts (thin)

Browserbase-style **lease-scoped files** (peers-deep #7). Chrome downloads are
directed into `{artifacts_root}/leases/{lease_id}/downloads/` via CDP
`Browser.setDownloadBehavior` (mock records the call). Agents list/fetch by
**artifact id** — absolute host paths are omitted by default (optional
`?rel_path=1` returns lease-relative `downloads/…` only).

```http
GET /v1/leases/{lease_id}/downloads
GET /v1/leases/{lease_id}/downloads?agent_id=…&rel_path=1
→ 200 { "lease_id", "downloads": [
    { "id", "filename", "bytes", "sha256", "created_at", "kind":"download", "path?" }
  ], "total" }

GET /v1/leases/{lease_id}/downloads/{artifact_id}
→ 200 application/octet-stream  (X-Slipstream-Sha256)
→ 200 application/json when Accept: application/json  { "lease_id", "download": {…} }
```

Optional thin **upload drop** (same discipline):

```http
POST /v1/leases/{lease_id}/uploads
{ "filename": "drop.bin", "content_b64": "…", "agent_id": "…"  // required }
→ 201 { "lease_id", "upload": { id, filename, bytes, sha256, created_at, kind:"upload" } }

GET /v1/leases/{lease_id}/uploads
GET /v1/leases/{lease_id}/uploads/{artifact_id}
```

Safety: Path containment + `O_NOFOLLOW` / `O_DIRECTORY|O_NOFOLLOW` on kind dirs; directory symlink containment refused; file symlink escape refused; secret-looking
filenames denylisted; **required** `agent_id` must match lease owner (omit → 401/403). Env:
`SLIPSTREAM_ARTIFACTS_ROOT`, `SLIPSTREAM_MAX_UPLOAD_BYTES` (default 5 MiB).

CLI: `slipstream downloads list|get` · `slipstream uploads list|put`.

**Not included:** cloud object storage, full upload product polish, Electron, MCP, Monid.

### Ops session list (thin)

Browserbase-style **fleet / ops list** (ui-peers steal #7). Joins active leases with status, duration, tags, signed-in badge, and **watch_url only when a valid tokenized Watch already exists** (need_human / awaiting_human). Treat `watch_url` as a screen-share secret — never log it; do not invent long-lived public URLs.

```http
GET /v1/ops/sessions
GET /v1/ops/sessions?q=env=staging
→ 200 { "sessions": [
    { "space_id", "lease_id", "agent_id?", "status", "leased_at", "duration_s",
      "user_metadata", "signed_in", "signed_in_host?", "watch_url?" }
  ], "q" }

GET /v1/ops/
→ 200 text/html  (loopback table; Open Watch button when watch_url present)
```

`GET /v1/leases` also includes `duration_s` and optional `watch_url` (same secret rule).

CLI: `slipstream sessions` (table; watch presence only) · `slipstream sessions --json` (includes tokenized URLs).

**Not included here:** full dashboard WS, Monid, Electron, MCP. Downloads: see above. CAPTCHA chips: see above.

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
| `slipstream act --lease-id … --category … --summary …` | `POST /v1/leases/{id}/actions` |
| `slipstream confirm\|deny <confirm_id>` | `POST /v1/confirmations/{id}` |
| `slipstream release --lease-id …` | `DELETE /v1/leases/{id}` |
| `slipstream status` | `GET /v1/pool/status` |
| `slipstream doctor [--json]` | local preflight + `GET /healthz` (Chrome/CDP/spaces/skill) |
| `slipstream cred bind\|unbind\|list\|fill …` | credential vault (bind/unbind captain/local; list/fill agent-safe) |

Uses stdlib `urllib`. Env: `SLIPSTREAM_URL` for base URL. Non-zero exit + stderr on HTTP errors.
`doctor` also probes an ephemeral Chrome CDP endpoint and checks Spaces root + skill path (no lease required).

## Driver notes

- **Default:** drive via pool HTTP — `POST …/navigate`, `POST …/eval`, `POST …/credentials/fill`, plus `act` / `confirm` (ladder server-enforced).
- **Escape hatch:** `SLIPSTREAM_EXPOSE_RAW_CDP=1` restores lease `cdp_http_url` / `cdp_ws_url` / `cdp_port` / status `chromium_pid` (and status `cdp_base_port`) for Playwright `connectOverCDP` / agent-browser. Ladder is honor-system for raw CDP nav/eval; vault fill stays pool-only. Default status/lease omit ports and pid — **do not derive CDP from public JSON** (no `9222+slot_id` recipe).
- Thin in-house `slipstream.cdp_http` helpers remain for smoke/bench when raw CDP is exposed.

## RSS sampling hook

`slipstream.rss.sample_tree_rss(pid)` sums `/proc` VmRSS across the process tree (Linux). Returns `None` if unavailable. Exposed on slot status as `rss_bytes` for later K tuning — see README.


## user_metadata tags + list filter (session-metadata)

Browserbase-style ops tags for fleets. **No secrets** — denylist keys
(`password` / `cookie` / `token` / `jwt` / `bearer` / camelCase `accessToken` / …) are refused (same
rules as alerts).

### Shape

- JSON **object**; nested objects OK; **string leaves only** (no arrays /
  numbers / bools — stringify them).
- Serialized size ≤ **512** chars (`json.dumps` separators compact).
- Prefer **Space-level** tags (persist for the Space id); leases **inherit**; effective Space∪lease merge re-checked ≤512 chars (ADV-META-002)
  and may **override** per-lease.

### Attach

`PUT /v1/spaces/{space_id}`

```json
{ "user_metadata": { "env": "staging", "team": "fleet", "run": { "id": "r1" } } }
```

→ `200` `{ "space_id", "user_metadata" }`

`POST /v1/leases` optional field:

```json
{
  "agent_id": "agent-1",
  "space_id": "task-42",
  "user_metadata": { "env": "canary" }
}
```

Lease response includes effective `user_metadata` (Space ∪ lease override).

### List / filter (`q=`)

| Method | Path | Notes |
|--------|------|-------|
| `GET` | `/v1/spaces?q=…` | Known Spaces (tagged and/or currently slotted) |
| `GET` | `/v1/leases?q=…` | Active leases; matches **effective** metadata |

**`q` grammar** (space-separated AND):

| Token | Match |
|-------|--------|
| `key=value` or `key:value` | Exact equality on top-level or dotted path (`run.id=r1`) |
| `user_metadata['env']:'staging'` | Browserbase-style path equality |
| bare `substring` | Any string leaf **contains** the token |

Omit `q` → return all. No match → empty list (not 404).

→ `400` `invalid_user_metadata` when attach payload violates rules / denylist.

### CLI

```bash
slipstream spaces set --space-id task-42 --tag env=staging --tag team=fleet
slipstream spaces list --q 'env=staging'
slipstream lease --agent-id a1 --space-id task-42 --tag env=canary
slipstream leases list --tag env=canary
```

`--tag` is sugar for `q=` / metadata keys; `--metadata '{"env":"staging"}'` also works.
