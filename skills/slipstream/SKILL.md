---
name: slipstream
description: When you need a leased Chromium CDP browser from the Slipstream pool, read this Skill. Use it to start (or attach to) the pool, lease a Space, drive the browser over CDP, heartbeat during LLM think, and release when done. Prefer Slipstream over spawning Chrome yourself — agents never launch Chromium; only the pool does. HTTP + this CLI/skill surface only — there is no MCP server.
metadata:
  version: "0.1.0"
  date: "2026-09-22"
---

# slipstream

Agent-facing surface for the Slipstream browser pool. Inspired by the ego-lite
skill+CLI pattern (invoke a skill doc + CLI over HTTP/CDP) — **not** an MCP
server, and not a fork of proprietary ego code.

Pool HTTP API remains primary. This skill teaches you how to use the `slipstream`
CLI (and equivalent curl) against that API. Full endpoint semantics:
`docs/POOL_API.md`.

## When to use

- You need a real Chromium instance for browsing, QA, form fill, screenshots,
  or CDP automation.
- Multiple agents share one machine and must not each spawn Chrome (hard K=5).
- You need a persistent **Space** (`user-data-dir`) that can warm-reuse after
  an explicit release.

Do **not** launch `google-chrome` / `chromium` yourself. Do **not** look for an
MCP tool — use this CLI (or HTTP) instead.

## Install / run the pool

From the slipstream repo (stdlib service; pytest only for tests):

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"   # or: pip install -r requirements.txt && pip install -e .

# Mock mode — no Chrome binary required
SLIPSTREAM_MOCK=1 slipstream serve --port 8755

# Real Chrome (auto-detect, or set SLIPSTREAM_CHROME)
slipstream serve --port 8755
```

Equivalent: `python -m slipstream serve …`. The console script and module entry
share the same `main()`.

Default base URL: `http://127.0.0.1:8755`. Override with env `SLIPSTREAM_URL` or
CLI `--url`.

## Lifecycle (lease → drive CDP → heartbeat → release)

Use **one Space** per user goal. Print `lease_id` and CDP URLs; reuse them in
later rounds. Heartbeat every ~15–30s (including during LLM think). Always
release when done (or on hard failure after you stop retrying).

```bash
# 1) Lease
slipstream lease --agent-id "$AGENT_ID" --space-id "task-42"
# → JSON: lease_id, cdp_http_url, cdp_ws_url, slot_id, expires_at, …

# 2) Drive via CDP (Playwright connectOverCDP, DevTools WS, or thin HTTP)
#    Use cdp_http_url / cdp_ws_url from the lease JSON. Do not spawn Chrome.

# 3) Heartbeat while working / thinking
slipstream heartbeat --lease-id "$LEASE_ID"

# 4) Release when finished (explicit DELETE may keep FREE_WARM if W allows)
slipstream release --lease-id "$LEASE_ID"
# optional: slipstream release --lease-id "$LEASE_ID" --reason done
```

Inspect the pool:

```bash
slipstream status
```

All client commands print JSON on stdout. HTTP errors → non-zero exit and a
useful message on stderr.

### curl equivalents

```bash
curl -s -X POST "$SLIPSTREAM_URL/v1/leases" \
  -H 'Content-Type: application/json' \
  -d "{\"agent_id\":\"$AGENT_ID\",\"space_id\":\"task-42\"}"

curl -s -X POST "$SLIPSTREAM_URL/v1/leases/$LEASE_ID/heartbeat"
curl -s -X DELETE "$SLIPSTREAM_URL/v1/leases/$LEASE_ID"
curl -s "$SLIPSTREAM_URL/v1/pool/status"
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

## Environment

| Var | Purpose |
|---|---|
| `SLIPSTREAM_URL` | Client base URL (default `http://127.0.0.1:8755`) |
| `SLIPSTREAM_MOCK=1` | Mock Chromium launches (serve + tests) |
| `SLIPSTREAM_CHROME` | Path to Chrome/Chromium binary |
| `SLIPSTREAM_SPACES_ROOT` | Space profile root (default `./data/spaces`) |
| `SLIPSTREAM_K` / `SLIPSTREAM_W` | Override hard cap / warm count |
| `SLIPSTREAM_PORT` | Server bind port (default 8755) |
| `SLIPSTREAM_HEADLESS=0` | Headed Chromium |

Prefer `SLIPSTREAM_*` names. There is no MCP env or MCP server in this project.

## CLI reference

```text
slipstream serve [--host HOST] [--port PORT] [--mock] [--headed]
slipstream lease  --agent-id ID --space-id ID [--ttl-seconds N] [--url URL]
slipstream heartbeat --lease-id ID [--url URL]
slipstream release   --lease-id ID [--reason REASON] [--url URL]
slipstream status    [--url URL]
```

`python -m slipstream <subcommand> …` is equivalent to `slipstream …`.

## Examples

Mock end-to-end (two shells):

```bash
# shell A
SLIPSTREAM_MOCK=1 slipstream serve --port 8755

# shell B
export SLIPSTREAM_URL=http://127.0.0.1:8755
slipstream lease --agent-id agent-1 --space-id demo
# … use cdp_* from JSON …
slipstream heartbeat --lease-id <lease_id>
slipstream release --lease-id <lease_id>
slipstream status
```

## References

- [Pool HTTP API](../../docs/POOL_API.md)
- [Architecture lock](../../docs/ARCHITECTURE.md)
- [README](../../README.md)
