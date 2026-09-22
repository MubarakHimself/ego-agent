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
  version: "0.2.0"
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
- The intent is “watch / summarize this video” — **compose** upstream
  Claude `/watch` (`bradautomates/claude-video`; e.g.
  `npx skills add bradautomates/claude-video -g`). Route the intent; do
  **not** invent a Slipstream video API and do **not** vendor watch scripts.
  `slipstream doctor` warns (not fails) if `/watch` is missing.
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
`GET /healthz`, Spaces root writable, this skill file present. Exit 0 if no
failures (warnings/skips allowed).

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

# 4) Release when finished
slipstream release --lease-id "$LEASE_ID"
# optional: slipstream release --lease-id "$LEASE_ID" --reason done
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
| `SLIPSTREAM_URL` | Client base URL (default `http://127.0.0.1:8755`) |
| `SLIPSTREAM_MOCK=1` | Mock Chromium launches (**tests/CI only**) |
| `SLIPSTREAM_CHROME` | Path to Chrome/Chromium binary |
| `SLIPSTREAM_SPACES_ROOT` | Space profile root (default `./data/spaces`) |
| `SLIPSTREAM_SKILL_PATH` | Override skill file path for `doctor` |
| `SLIPSTREAM_K` / `SLIPSTREAM_W` | Override hard cap / warm count |
| `SLIPSTREAM_PORT` | Server bind port (default 8755) |
| `SLIPSTREAM_HEADLESS=0` | Headed Chromium |

Prefer `SLIPSTREAM_*` names. There is no MCP env or MCP server in this project.

## CLI reference

```text
slipstream serve   [--host HOST] [--port PORT] [--mock] [--headed]
slipstream lease   --agent-id ID --space-id ID [--ttl-seconds N] [--url URL]
slipstream heartbeat --lease-id ID [--url URL]
slipstream release --lease-id ID [--reason REASON] [--url URL]
slipstream status  [--url URL]
slipstream doctor  [--url URL] [--json]
```

`python -m slipstream <subcommand> …` is equivalent to `slipstream …`.

## Non-goals (do not invent APIs)

- No Electron / Chromium fork / Jev family.
- No Monid / paid marketplace.
- No MCP server (skill+CLI+HTTP only).
- No free-read of Space cookies / credential dumps.
- Credentials vault, takeover/done alerts, live pair-browse, compose `/watch`
  are **later / compose** — not part of this skill’s CLI surface yet.

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

- [Pool HTTP API](../../docs/POOL_API.md)
- [Architecture lock](../../docs/ARCHITECTURE.md)
- [README](../../README.md)
