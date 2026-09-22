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
  version: "0.5.0"
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

## Lifecycle (lease → drive CDP → heartbeat → release)

Use **one Space** per user goal. Print `lease_id` and CDP URLs; reuse them in
later rounds. Heartbeat every ~15–30s (including during LLM think). Always
release when done (or on hard failure after you stop retrying).

```bash
# 1) Lease
slipstream lease --agent-id "$AGENT_ID" --space-id "task-42"
# → JSON: lease_id, cdp_http_url, cdp_ws_url, slot_id, expires_at, …

# 2) Drive via CDP — attach a peer driver (do NOT spawn Chrome)
#    Playwright:
#      playwright.chromium.connect_over_cdp(cdp_http_url)
#    Vercel agent-browser:
#      agent-browser --cdp "$cdp_http_url" open https://example.com
#    Browser Use:
#      BU_CDP_URL="$cdp_http_url" browser-use <<'PY'
#      # … your steps …
#      PY

# 3) Heartbeat while working / thinking
slipstream heartbeat --lease-id "$LEASE_ID"

# 4a) If blocked — raise need_human (keeps lease; pause CDP drive)
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

**Login / 2FA / CAPTCHA:** if fill is not enough (or no bind exists), raise
`need_human` with `reason=login` (or `other`). Lease stays warm; pause CDP;
captain uses Watch / Take-over. Prefer session reuse on later leases of the
same Space after a successful human or fill login.

**Refuse:** free-read secret endpoints (`error=refused`), cookie/`storageState`
dumps to the agent, secrets in alert payloads / fill bodies.

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

Slipstream owns **pool lease**. Peers own **page drive**:

| Driver | Attach |
|--------|--------|
| Playwright | `chromium.connect_over_cdp(cdp_http_url)` |
| Vercel agent-browser | `agent-browser --cdp "$cdp_http_url" …` |
| Browser Use | `BU_CDP_URL` / `BU_CDP_WS` pointing at lease CDP |

Thin in-house helpers (`slipstream.cdp_http`) are for smoke/bench only.

## Subagent / harness rules

- Use a **unique `agent_id`** per agent/subagent instance.
- Use a **stable `space_id`** per user goal (reuse after release for warm).
- On **409** `space_in_use`: wait or pick another Space — never steal.
- On **503** `pool_full`: backoff or ask the main agent to free a slot.
- Always **release** on the failure path after you stop retrying.
- Hermes / Cursor / OpenHands / Claude Code: install this skill + call the
  `slipstream` CLI via the shell tool. Prefer shell + peer CDP driver over
  registering a second browser launcher.

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

Prefer `SLIPSTREAM_*` names. There is no MCP env or MCP server in this project.

## CLI reference

```text
slipstream serve   [--host HOST] [--port PORT] [--mock] [--headed]
slipstream lease   --agent-id ID --space-id ID [--ttl-seconds N] [--url URL]
slipstream heartbeat --lease-id ID [--url URL]
slipstream alert   need-human|done --lease-id ID [options] [--url URL]
slipstream release --lease-id ID [--reason REASON] [--url URL]
slipstream status  [--url URL]
slipstream doctor  [--url URL] [--json]
slipstream watch-status [--json]
slipstream cred    bind|unbind|list|fill …
```

`python -m slipstream <subcommand> …` is equivalent to `slipstream …`.

## Non-goals (do not invent APIs)

- No Electron / Chromium fork / Jev family.
- No Monid / paid marketplace.
- No MCP server (skill+CLI+HTTP only).
- No free-read of Space cookies / credential dumps.
- Simultaneous human+agent drive, pair-browse UI polish, optional stuck/captcha chips
  lock are **later**.
- Compose `/watch` remains upstream (see **Compose /watch**; doctor WARN if
  missing). Alerts + credential vault/fill **are** on the CLI/HTTP surface.

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
