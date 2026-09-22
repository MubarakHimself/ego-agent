---
name: slipstream
description: >
  When you need a leased Chromium CDP browser from the Slipstream pool
  (multi-agent Spaces, K-cap), read this Skill. Prefer Slipstream over
  spawning Chrome yourself — agents never launch Chromium; only the pool
  does. Alias intent: slipstream-browser (do not collide with Hermes built-in
  browser). HTTP + this CLI/skill surface only — there is no MCP server.
  For video watch intents, compose a separate /watch skill (not Slipstream).
metadata:
  version: "0.6.0"
  date: "2026-09-22"
---

# slipstream

Agent-facing surface for the Slipstream browser pool. Usable by a **main agent
or a subagent** / Hermes-style harness via shell. Inspired by the ego-lite
skill+CLI pattern — **not** an MCP server, and not a fork of proprietary ego
code.

Pool HTTP API remains primary. This skill teaches you how to use the
`slipstream` CLI (and equivalent curl) against that API. Full endpoint
semantics: `docs/POOL_API.md`.

**Naming:** skill `name` is `slipstream`. Treat `slipstream-browser` as an
alias for harness routing when many “browser” skills coexist. Do **not**
register a skill named `browser` (collides with Hermes built-in).

## When to use / when not

**Use when:**
- You need a real Chromium instance for browsing, QA, form fill, screenshots,
  or CDP automation.
- Multiple agents share one machine and must not each spawn Chrome (hard K=5).
- You need a persistent **Space** (`user-data-dir`) that can warm-reuse after
  an explicit release.

**Do not use when:**
- A plain HTTP fetch/`curl` is enough for a static public page.
- The intent is "watch / summarize this video" (URL or local path) — **compose**
  upstream Claude `/watch` (see **Compose /watch** below). Route the intent;
  do **not** invent a Slipstream video API and do **not** vendor watch scripts.
- You are looking for an MCP tool — there is none; use this CLI (or HTTP).

Do **not** launch `google-chrome` / `chromium` yourself.

## Install / doctor

From the slipstream repo (stdlib service; pytest only for tests):

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Preflight (real path — mock OFF)
slipstream doctor
slipstream doctor --json
```

`doctor` checks: Chrome binary, ephemeral CDP probe (`/json/version`), pool
`GET /healthz`, Spaces root writable, this skill file present, and optional
composed `/watch` (`watch_compose` — **PASS** if found at a known path,
**WARN** if missing; never fails; never vendored). Also:
`slipstream watch-status` (thin compose-only status). Exit 0 if no failures
(warnings/skips allowed). Pasteable agent install: [`docs/install.md`](../../docs/install.md).

**Versions:** package `slipstream.__version__` is `0.1.0`. Skill
`metadata.version` (`0.2.0`) is the skill-doc revision and may differ —
agents should not assume they are equal. Skill ships as package data under
`skills.slipstream` so non-editable installs keep `skill_path`.

**URL allowlist:** `--url` / `SLIPSTREAM_URL` default to loopback
(`127.0.0.1` / `::1` / `localhost`) only. Set `SLIPSTREAM_ALLOW_REMOTE_URL=1`
to reach a remote pool.

Start the pool (**real Chrome** out-of-box; mock is CI/unit only):

```bash
# Real Chrome (auto-detect, or set SLIPSTREAM_CHROME)
slipstream serve --port 8755

# Mock mode — tests / no Chrome only (NOT the out-of-box path)
SLIPSTREAM_MOCK=1 slipstream serve --port 8755
```

Equivalent: `python -m slipstream serve …`. Default base URL:
`http://127.0.0.1:8755`. Override with env `SLIPSTREAM_URL` or CLI `--url`.

## Lifecycle (lease → pool HTTP drive → heartbeat → release)

Use **one Space** per user goal. Print `lease_id`; reuse it in later rounds.
Heartbeat every ~15–30s (including during LLM think). Always release when done
(or on hard failure after you stop retrying).

```bash
# 1) Lease
slipstream lease --agent-id "$AGENT_ID" --space-id "task-42"
# → JSON: lease_id, slot_id, expires_at, … (no cdp_* URLs by default)

# 2) Drive via pool HTTP (ladder-enforced) — do NOT spawn Chrome
#    slipstream act … && slipstream confirm c_…
#    slipstream navigate --lease-id "$LEASE_ID" --url "https://example.com"
#    slipstream eval --lease-id "$LEASE_ID" --expression "document.title"
#    slipstream cred fill --lease-id "$LEASE_ID" …

#    Escape hatch (honor-system for raw CDP nav/eval):
#      SLIPSTREAM_EXPOSE_RAW_CDP=1 slipstream lease …
#      # then playwright.chromium.connect_over_cdp(cdp_http_url) / agent-browser --cdp

# 3) Heartbeat while working / thinking
slipstream heartbeat --lease-id "$LEASE_ID"

# 4a) If blocked — raise need_human (keeps lease; pause drive)
# slipstream alert need-human --lease-id "$LEASE_ID" --reason captcha

# 4b) Prefer task_done (notifies + releases once) when the skill finishes
slipstream alert done --lease-id "$LEASE_ID" --summary "finished"
# or plain release if you already notified elsewhere:
# slipstream release --lease-id "$LEASE_ID" --reason done
```

Inspect the pool:

```bash
slipstream status
```

All client commands print JSON on stdout (except human `doctor`). HTTP errors →
non-zero exit and a useful message on stderr.

### curl equivalents

```bash
curl -s -X POST "$SLIPSTREAM_URL/v1/leases" \
  -H 'Content-Type: application/json' \
  -d "{\"agent_id\":\"$AGENT_ID\",\"space_id\":\"task-42\"}"

curl -s -X POST "$SLIPSTREAM_URL/v1/leases/$LEASE_ID/heartbeat"
curl -s -X DELETE "$SLIPSTREAM_URL/v1/leases/$LEASE_ID"
curl -s "$SLIPSTREAM_URL/v1/pool/status"
curl -s "$SLIPSTREAM_URL/healthz"
```

(`SLIPSTREAM_URL` defaults to `http://127.0.0.1:8755` if unset.)


## Permission ladder (confirm-actions — server-enforced)

Before irreversible CDP acts, call `slipstream act`, wait for captain
`confirm`, then call the CDP endpoint. Soft browse (snapshot / click /
scroll / wait) stays free. Gated categories:

- `fill` — credentials fill (`cred fill`)
- `eval` — script / `Runtime.evaluate` (`slipstream eval`)
- `download` — file download
- `upload` — file upload / set file input
- `nav_irreversible` — all pool navigates (`slipstream navigate`)

```bash
slipstream act --lease-id "$LEASE_ID" --category eval --summary "probe document.title"
# → JSON status=confirmation_required + confirm_id; agent pauses
#   (sibling need_human on alerts bus → Watch / Take-over URL)

slipstream confirm c_…     # allow once; grants one-shot CDP allowance
slipstream deny c_…        # fail closed; no allowance

# After confirm, the matching CDP path may run once:
slipstream eval --lease-id "$LEASE_ID" --expression "document.title"
# Second eval without a fresh confirm → 403 confirmation_required
```

Pending confirmations **auto-deny after ~60s** (`SLIPSTREAM_CONFIRM_TTL`).
Unused one-shot grants use the **same TTL**. `--confirm-interactive` prompts
on a TTY; **Non-TTY → deny**.

Never put secrets/cookies/passwords in `--summary` (scrub also redacts
`jwt=` / `access_key=` / `private_key=` / `api_key=` / `passwd=`).

**Server-enforced (ADV-PL-001) on pool HTTP:** pool **refuses** fill / eval /
navigate without a prior confirm for that category (403
`confirmation_required`, no CDP side-effect). Confirm is one-shot then re-gate.
Domain allowlist on navigate still applies when set. Raw CDP
(`SLIPSTREAM_EXPOSE_RAW_CDP=1`) is honor-system for nav/eval; vault fill stays
pool-only.

Defer: once/always/never policy matrix, Comet UI.

## Domain allowlist (top-frame)

Restrict top-frame navigation when set. **No allowlist anywhere = unrestricted.** Lease `[]` inherits Space∩config (never clears lockdown); lease may only narrow. Active allowlist: http(s) + host only; no `\`/`%5C`/userinfo.

```bash
export SLIPSTREAM_ALLOWED_DOMAINS="example.com,*.example.org"
# or per-Space / per-lease:
slipstream spaces set --space-id task-42 --allowed-domains example.com,github.com
slipstream lease --agent-id a1 --space-id task-42 --allowed-domains example.com
slipstream navigate --lease-id "$LEASE_ID" --page-url https://www.example.com/
# outside list → refuse (domain_not_allowed)
```

**Limitation (v1):** iframe/subresource loads are not blocked (Browserbase experimental mirror).

## Content-boundary markers

Opt-in prompt-injection hygiene for page-derived skill/CLI echoes:

```bash
export SLIPSTREAM_CONTENT_BOUNDARIES=1
# wraps page text as:
# --- SLIPSTREAM_PAGE_CONTENT nonce=… origin=https://… ---
# …untrusted page output…
# --- END_SLIPSTREAM_PAGE_CONTENT nonce=… ---
```

Helper: `from slipstream.boundaries import wrap_page_content`. Minimal — not a sandbox.

## Compose /watch (video intents — do not reimplement)

**Rule:** Slipstream **routes** video intents to an installed upstream `/watch`
skill. It does **not** embed or vendor `bradautomates/claude-video` scripts
(no yt-dlp / ffmpeg / Whisper reimplementation here).

### When to route

Hand off to `/watch` when the user or agent intent is about a **video URL or
local media path**, for example:

- YouTube / Vimeo / Loom / TikTok / X / Twitch clip / other yt-dlp URLs
- Local `.mp4` / `.mov` / `.mkv` / `.webm` (or similar)
- Asks like “what’s in this video”, “summarize this clip”, “what happens at
  0:42”, “bug repro from this screen recording”

Keep **interactive web** (forms, QA, login, CDP automation) on a Slipstream
**Space lease**. Live human observe of a leased page is Slipstream’s tokenized
Watch JPEG/HTML (`need_human`) — that is **not** the Claude `/watch` skill.

**Activity feed (Watch dock):** the Watch page shows a chronological lease-scoped action feed beside the live JPEG (`navigate` / `click` / `type` / `fill` / `alert` / `confirm` / `captcha`). Poll `GET …/watch/events?token=…` (same token TTL/revoke as Watch → 410). Entries are scrubbed — never cookies, passwords, typed text, vault, or CDP auth (`type`/`fill` → length/labels only). Soft browse remains free; feed is captain observe opacity. Reuses the alerts bus for `need_human` / `task_done` (no second notification path).

**Dual timeline / thin session scrubber:** Watch shows a live viewport clock beside an event-clock driven by activity-feed timestamps, plus a thin scrubber over `GET …/watch/timeline?token=…` markers (`[{seq,ts,kind,summary}]`). Seeking highlights the matching feed row; the JPEG stays the live frame (no ffmpeg / video recording). Same token TTL/revoke → 410.

**Evidence panel (annotated / timed stills):** Watch shows an Evidence aside beside the activity feed. Selected feed events auto-capture a JPEG still keyed by feed `seq` (default kinds: navigate / confirm / alert; `SLIPSTREAM_EVIDENCE_AUTO=0` disables; `SLIPSTREAM_EVIDENCE_MAX` bounds retention). Poll `GET …/watch/evidence?token=…` for markers; `…&seq=N` returns the JPEG (same token TTL/revoke → 410). Click a feed row to load matching evidence + scrubbed refs — never secrets. No ffmpeg / video / Monid / Electron / MCP.


### Install upstream `/watch`

```bash
# Agent Skills CLI (Codex / Cursor / Gemini / 50+ hosts)
npx skills add bradautomates/claude-video -g

# Claude Code marketplace
#   /plugin marketplace add bradautomates/claude-video
#   /plugin install watch@claude-video
```

Override detection: `SLIPSTREAM_WATCH_SKILL=/path/to/watch/SKILL.md`.

Doctor known paths include `~/.claude|codex|cursor|agents|openclaw|gemini|slipstream/skills/watch/SKILL.md`
and Claude Code plugin cache. Missing `/watch` → `watch_compose` **WARN** only.

```bash
slipstream doctor            # watch_compose OK|WARN
slipstream watch-status      # compose-only
slipstream watch-status --json
```

### How to invoke (after install)

1. Confirm skill present (`doctor` / `watch-status`).
2. Read the installed `/watch` `SKILL.md` (harness shows the path).
3. Follow that skill’s contract (user-invocable `/watch`, or
   `python3 "$SKILL_DIR/scripts/watch.py" "<url-or-path>" …` after setup).
4. Prefer **native captions** (free). Whisper keys are optional upstream —
   Slipstream never requires paid Whisper.

### Non-goals

- Do **not** copy `watch.py` / `download.py` / `frames.py` into this repo
- Do **not** call Monid or paid tool marketplaces for video
- Do **not** treat Slipstream pool HTTP as a video API


## CAPTCHA chips (§5.11)

Report solve lifecycle (no paid SaaS). Unsolved → `need_human` + Watch.

```bash
# Detect / stub started — high-salience chip on activity + Watch (when open)
slipstream captcha started --lease-id "$LEASE_ID" --detail "challenge visible"
# Solved locally / by human — does NOT raise need_human
slipstream captcha finished --lease-id "$LEASE_ID"
# Explicit fail — escalates need_human(reason=captcha) with Watch/Take-over
slipstream captcha failed --lease-id "$LEASE_ID" --detail "unsolved"

# HTTP
curl -sS -X POST "$SLIPSTREAM_URL/v1/leases/$LEASE_ID/captcha" \
  -H 'content-type: application/json' \
  -d '{"event":"started","detail":"recaptcha"}'
```

Timeout: `SLIPSTREAM_CAPTCHA_TIMEOUT` (default 60s) while `solving` → escalate on
heartbeat / Watch. Secrets refused; captcha detail also scrubs unlabeled JWT-like tokens. Chip/banner HTML escapes dynamic fields. Late `failed` after `finished` does not escalate. No Monid / 2captcha / anti-captcha wiring.

## Downloads / uploads (session artifacts)

Lease-scoped files (Browserbase-style). Chrome downloads go to a pool-owned
dir under `SLIPSTREAM_ARTIFACTS_ROOT` (outside Space + vault). List/fetch by
artifact id — **no absolute paths** to the agent by default.

```bash
slipstream downloads list --lease-id "$LEASE_ID" --agent-id "$AGENT_ID"
slipstream downloads get --lease-id "$LEASE_ID" --artifact-id dl_… --agent-id "$AGENT_ID" -o ./out.bin
# thin upload drop
slipstream uploads put --lease-id "$LEASE_ID" --filename drop.bin --file ./local.bin
slipstream uploads list --lease-id "$LEASE_ID"
```

Required `?agent_id=` must match lease owner (omit → 401/403). Symlink/path escape refused; secret
filenames denylisted. No cloud storage in MVP.

## Alerts (need_human + task_done)

Ship-now takeover / done signals on the lease HTTP spine. **Never** put cookies,
passwords, tokens, JWTs, bearers, private/access keys, auth headers, credential
dumps, or secret paths in alert **keys**. Free-text `detail` / `summary` is a
trust boundary — do not put secrets there (server lightly scrubs
`password=`/`cookie=`/`token=`/`secret=`/`authorization=`/`bearer=`). Do not send `watch_url` or `status`
(server-derived). Soft-idle eviction is skipped while awaiting human; hard TTL
still applies.

| Event | Effect |
|-------|--------|
| `need_human` | Pause the agent; **lease stays warm** (Chromium kept; soft-idle skipped). Captain gets a one-liner + Watch / Take-over links. |
| `task_done` | Notify once, then **release** the lease. Double-fire is idempotent/safe. Later `need_human` on that id is `404`. |

```bash
# Agent blocked (CAPTCHA / login / ambiguous UI / …) — pause & keep session
slipstream alert need-human --lease-id "$LEASE_ID" --reason captcha \
  --detail "Cloudflare challenge on checkout"

# Skill finished — notify once then release
slipstream alert done --lease-id "$LEASE_ID" --summary "Filled form; submitted"
# declared fail:
slipstream alert done --lease-id "$LEASE_ID" --fail --summary "Blocked by paywall"
```

Harness JSON (`alert` + `harness`) tells the caller to `pause` or `continue`.
Fields include `captain_message`, `watch_url`, `takeover_url`, `lease_kept` /
`lease_released`. Server-derived `watch_url` is a short-TTL **tokenized** local
Watch (JPEG/HTML). Open it for live frames; Take-over confirm pauses the
agent and enables exclusive pair-browse (click/type/scroll into leased CDP).
Cede returns drive; revoked on `task_done` / TTL (`401`/`410`).
Never put secrets on the page. No client override of `watch_url`.

curl:

```bash
curl -s -X POST "$SLIPSTREAM_URL/v1/leases/$LEASE_ID/alerts" \
  -H 'Content-Type: application/json' \
  -d '{"event":"need_human","reason":"captcha","detail":"challenge visible"}'

curl -s -X POST "$SLIPSTREAM_URL/v1/leases/$LEASE_ID/alerts" \
  -H 'Content-Type: application/json' \
  -d '{"event":"task_done","outcome":{"ok":true,"summary":"done"}}'
```

Reasons for `need_human`: `captcha` | `login` | `ambiguous_ui` | `stuck` | `other`.

## Credentials (vault + fill)

Secrets live in a **vault outside** the Space profile (`SLIPSTREAM_VAULT_ROOT`;
must not sit under `SLIPSTREAM_SPACES_ROOT`). The Space `user-data-dir` keeps
**session cookies only** after login — never a password dump. Agents **never**
free-read secrets or cookie jars.

**Trust boundary:** API responses and the LLM **never** receive plaintext
username/secret — only `cred_id` / labels / origins / `filled` names. Pool
CDP-injects in-process and scrubs memory after fill. Do not read filled DOM
values back into prompts (full pause-CDP anti-readback is later).

**v1 auth:** bind/unbind trust the loopback pool bind; no captain-token yet —
do not expose the pool beyond localhost.

| Call | Who | Notes |
|------|-----|-------|
| `cred bind` | Captain / local tool | Prefer `--secret-env` / `--secret-file` / `--prompt` (not argv) |
| `cred list` | Agent ok | Metadata (`cred_id`, label, origin, `has_secret`) |
| `cred fill` | **Agent** | `cred_id` + CSS selectors only — pool CDP-injects; origin must match page |
| `cred unbind` | Captain / local | Removes binding |

```bash
# Captain/local bind (no secret on argv)
slipstream cred bind --space-id "$SPACE" --label work-gh \
  --origin https://github.com --username "$USER" --secret-env SLIPSTREAM_BIND_SECRET

# Agent on a login form — fill with selectors; NEVER ask LLM for the password
slipstream cred fill --lease-id "$LEASE_ID" --cred-id "$CRED_ID" \
  --fields '{"username":"#login_field","password":"#password"}'
# → {"ok":true,"filled":["username","password"]}
# → 400 if page origin ≠ cred.origin
```


## Login-once + signed-in badge

Human logs in **once** into a Space via Watch/Take-over; session cookies persist in the Space Chromium profile (`--user-data-dir`). Mark a **signed-in** badge afterward (metadata / host label only — **never** dump cookies to the agent). Vault fill stays separate.

```bash
# Option A — existing path
slipstream alert --lease-id "$LEASE_ID" need_human --reason login --detail "please sign in"

# Option B — convenience: lease + need_human(login)
slipstream spaces login-once --space-id "$SPACE" --agent-id agent-1 --host github.com

# After captain Take-over login (or Watch "Mark Space signed-in"):
slipstream spaces signed-in --space-id "$SPACE" --host github.com
# Clear badge:
slipstream spaces signed-in --space-id "$SPACE" --clear

# Badge visible on:
#   GET /v1/spaces  → signed_in / signed_in_host
#   GET /v1/leases  → same
#   Watch header chip
```

Filter: `slipstream spaces list --q signed_in=true`.

**Login / 2FA / CAPTCHA:** if fill is not enough (or no bind exists), raise
`need_human` with `reason=login` (or `other`). Lease stays warm; pause CDP;
captain uses Watch / Take-over. Prefer session reuse on later leases of the
same Space after a successful human or fill login.

**Refuse:** free-read secret endpoints (`error=refused`), cookie/`storageState`
dumps to the agent, secrets in alert payloads / fill bodies.

## Ops session list

Thin fleet view of active leases (status, duration, tags, signed-in, Watch when minted):

```bash
slipstream sessions                  # table (watch column = yes/—; no secret URL)
slipstream sessions --json           # includes watch_url when need_human minted it
# HTML (loopback pool): open http://127.0.0.1:8755/v1/ops/
# JSON: GET /v1/ops/sessions
```

`watch_url` is a short-TTL screen-share secret — only present when already minted; never invent public URLs; do not log the token.

## Exclusivity and warm rules

- **Space exclusivity:** a `space_id` may be leased by only one agent at a time.
  A second agent gets HTTP **409** `space_in_use`. Same `(agent_id, space_id)`
  while still leased is idempotent (returns the existing lease) unless the hard
  TTL expired or the Chromium handle is dead.
- **Warm reuse:** only an **explicit** client `release` (DELETE) may leave a
  `FREE_WARM` slot (up to W=1) with that `space_id`. A later lease for the same
  `space_id` reuses the live process (no relaunch) when the warm handle is
  healthy. Idle eviction and hard-TTL expiry **always** stop Chromium — no
  stale CDP handoff.
- **Pool full:** hard K=5 → HTTP **503** `pool_full`. Wait/retry or release
  another lease; do not spawn your own browser.
- **Agents never own a permanent browser PID** — only leases.

## Driver attach recipes

Slipstream owns **pool lease**. **Default drive** is pool HTTP (navigate / eval /
cred fill + act/confirm) so the permission ladder is server-enforced.

| Path | When |
|------|------|
| Pool HTTP | Default — ladder consume_once is real |
| Raw CDP peers | Only with `SLIPSTREAM_EXPOSE_RAW_CDP=1` (lease returns cdp_*); nav/eval honor-system; vault fill still pool-only |

Raw CDP peers (escape hatch): Playwright `connect_over_cdp`, Vercel
`agent-browser --cdp`, Browser Use `BU_CDP_URL`. Thin `slipstream.cdp_http`
helpers are for smoke/bench when raw CDP is exposed.

## Subagent / harness rules

- Use a **unique `agent_id`** per agent/subagent instance.
- Use a **stable `space_id`** per user goal (reuse after release for warm).
- On **409** `space_in_use`: wait or pick another Space — never steal.
- On **503** `pool_full`: backoff or ask the main agent to free a slot.
- Always **release** on the failure path after you stop retrying.
- Install this skill and call the `slipstream` CLI via the shell tool.
  Prefer shell + pool HTTP drive (or raw CDP only with EXPOSE_RAW_CDP) over registering a second browser launcher.



## Session tags (`user_metadata`)

Ops labels for fleets (Browserbase-style). Attach on a **Space** (inherited by
leases) and/or override on **lease**. String leaves only; nested objects OK;
≤512 chars serialized (Space∪lease **effective** merge re-checked — ADV-META-002).
**Never** put secrets in metadata — keys like
`password` / `cookie` / `token` / `jwt` / `bearer` / camelCase `accessToken` / `sessionToken` / `clientSecret` are refused.

```bash
# Tag a Space, then filter
slipstream spaces set --space-id task-42 --tag env=staging --tag team=fleet
slipstream spaces list --q 'env=staging'

# Lease override + list active leases
slipstream lease --agent-id "$AGENT_ID" --space-id task-42 --tag env=canary
slipstream leases list --tag env=canary
```

`q=` tokens (AND): `key=value` / dotted `run.id=r1` / Browserbase
`user_metadata['env']:'staging'` / bare substring. See `docs/POOL_API.md`.

## Environment

| Var | Purpose |
|---|---|
| `SLIPSTREAM_URL` | Client base URL (default `http://127.0.0.1:8755`; loopback-only unless allow-remote) |
| `SLIPSTREAM_ALLOW_REMOTE_URL=1` | Escape hatch: allow non-loopback `--url` / `SLIPSTREAM_URL` |
| `SLIPSTREAM_MOCK=1` | Mock Chromium launches (**tests/CI only**) |
| `SLIPSTREAM_CHROME` | Path to Chrome/Chromium binary (explicit missing → fail; no PATH fallthrough) |
| `SLIPSTREAM_SPACES_ROOT` | Space profile root (default `./data/spaces`) |
| `SLIPSTREAM_VAULT_ROOT` / `VAULT_ROOT` | Cred vault root (default `./data/vault`; must be outside spaces) |
| `SLIPSTREAM_ALLOW_SECRET_ARGV=1` | Allow bare `--secret` on `cred bind` argv |
| `SLIPSTREAM_ALLOW_VAULT_KEY=1` | Allow `SLIPSTREAM_VAULT_KEY` outside mock |
| `SLIPSTREAM_SKILL_PATH` | Override skill file path for `doctor` |
| `SLIPSTREAM_WATCH_SKILL` | Override path to composed `/watch` skill for `doctor` |
| `SLIPSTREAM_K` / `SLIPSTREAM_W` | Override hard cap / warm count |
| `SLIPSTREAM_PORT` | Server bind port (default 8755) |
| `SLIPSTREAM_HEADLESS=0` | Headed Chromium |
| `SLIPSTREAM_CONFIRM_TTL` | Pending confirm-actions TTL seconds (default 60) |

Prefer `SLIPSTREAM_*` names. There is no MCP env or MCP server in this project.

## CLI reference

```text
slipstream serve   [--host HOST] [--port PORT] [--mock] [--headed]
slipstream lease   --agent-id ID --space-id ID [--ttl-seconds N] [--url URL]
slipstream heartbeat --lease-id ID [--url URL]
slipstream alert   need-human|done --lease-id ID [options] [--url URL]
slipstream act     --lease-id ID --category CAT --summary TEXT [--confirm-interactive]
slipstream navigate --lease-id ID --page-url URL
slipstream confirm CONFIRM_ID [--url URL]
slipstream deny    CONFIRM_ID [--url URL]
slipstream release --lease-id ID [--reason REASON] [--url URL]
slipstream status  [--url URL]
slipstream doctor  [--url URL] [--json]
slipstream watch-status [--json]
slipstream cred    bind|unbind|list|fill …
slipstream downloads list|get …
slipstream uploads   list|put …
```

`python -m slipstream <subcommand> …` is equivalent to `slipstream …`.

## Non-goals (do not invent APIs)

- No Electron / Chromium fork / Jev family.
- No Monid / paid marketplace.
- No MCP server (skill+CLI+HTTP only).
- No free-read of Space cookies / credential dumps.
- Simultaneous human+agent drive / pair-browse UI polish are **later**.
  Downloads/uploads as session artifacts **are** on the CLI/HTTP surface.
- Compose `/watch` remains upstream (see **Compose /watch**; doctor WARN if
  missing). Alerts, credential vault/fill, confirm-actions, domain allowlist
  navigate, and content-boundary helpers **are** on the CLI/HTTP surface.
  Once-always-never domain modes
  are **later**.

## Examples

Out-of-box (two shells):

```bash
# shell A
slipstream doctor          # expect Chrome + CDP ok; pool may WARN until serve
slipstream serve --port 8755

# shell B
export SLIPSTREAM_URL=http://127.0.0.1:8755
slipstream doctor          # pool_healthz should OK
slipstream lease --agent-id agent-1 --space-id demo
# … attach driver to cdp_* …
slipstream heartbeat --lease-id <lease_id>
slipstream release --lease-id <lease_id>
slipstream status
```

Mock end-to-end (CI only):

```bash
SLIPSTREAM_MOCK=1 slipstream serve --port 8755
```

## References

- [Install (agent paste)](../../docs/install.md)
- [Pool HTTP API](../../docs/POOL_API.md)
- [Architecture lock](../../docs/ARCHITECTURE.md)
- [README](../../README.md)
