# ego-agent

Personal multi-agent **CDP browser pool** (research → build).

**Not** a fork of CitroLabs ego lite. Inspired by public MIT harness patterns and peer CDP stacks. Original code only.

## Status

Architecture locked — see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and Firstmate report `ego-arch-pool-001`.

**MVP:** Browser Pool Manager — lease / heartbeat / release, hard **K=5**, warm **W=1**, Space = Chromium `user-data-dir`, Linux-first, local CDP.

**External surface (now):** localhost **HTTP JSON API** only (`docs/POOL_API.md`). **No MCP server yet** — agents and tools talk to the pool over HTTP; MCP may wrap the same lease API later.

## Constraints

- No Electron / Chromium fork / TypeSafe Jev / Open-Jev
- Laya optional later (decision head only — not a browser)
- No proprietary ego binary / ego-lite source theft
- Agents never spawn Chrome themselves — only the pool does

## Layout

```text
ego_pool/          # Pool service package
  config.py        # K=5, W=1, idle_ttl=300, spaces_root, cdp_base_port
  models.py        # SlotState, Lease
  launcher.py      # ChromiumLauncher (CDP + user-data-dir); mock via EGO_POOL_MOCK=1
  cdp_http.py      # Thin DevTools HTTP helpers (smoke/bench; not full driver)
  rss.py           # sample_tree_rss(pid) — /proc tree RSS hook
  pool.py          # BrowserPool lease/heartbeat/release/evict
  api.py           # HTTP JSON API (stdlib)
  __main__.py      # python -m ego_pool
scripts/
  bench_pool.py    # LIVE + MOCK pool speed benchmark
tests/             # pytest (mocked by default; @pytest.mark.live / bench)
benches/           # Sample benchmark outputs (committed samples)
docs/
  ARCHITECTURE.md  # Captain lock
  POOL_API.md      # HTTP endpoints
data/spaces/       # Runtime Space profiles (gitignored)
```

**Space path convention:** `{spaces_root}/{space_id}/` → Chromium `--user-data-dir`.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt   # pytest only; service is stdlib

# Mock mode (no Chrome required)
EGO_POOL_MOCK=1 python -m ego_pool --port 8755

# Real Chrome (system google-chrome / chromium; or EGO_POOL_CHROME=/usr/bin/google-chrome)
python -m ego_pool --port 8755
```

Base URL: `http://127.0.0.1:8755` — see [`docs/POOL_API.md`](docs/POOL_API.md).

Example:

```bash
curl -s -X POST http://127.0.0.1:8755/v1/leases \
  -H 'Content-Type: application/json' \
  -d '{"agent_id":"a1","space_id":"task-1"}'

# Heartbeat + release (DELETE only — no POST /release alias)
curl -s -X POST http://127.0.0.1:8755/v1/leases/<lease_id>/heartbeat
curl -s -X DELETE http://127.0.0.1:8755/v1/leases/<lease_id>
```

Spaces are exclusive while leased (second agent gets HTTP 409). Warm slots (from explicit DELETE only) with a matching live `space_id` are reused without relaunch. Idle / hard-TTL always stop Chromium.

Drive a leased browser via CDP (`cdp_http_url` / DevTools WebSocket) or Playwright `connectOverCDP`. Smoke/bench use thin HTTP helpers in `ego_pool.cdp_http` (PUT `/json/new`).

## Tests

```bash
source .venv/bin/activate

# Unit tests (mocked launcher — no browsers needed)
EGO_POOL_MOCK=1 pytest -q -m "not live and not bench"

# Optional live smoke (requires Chrome on PATH or EGO_POOL_CHROME)
# lease → CDP ready → navigate example.com → title check → heartbeat → release → warm reuse
pytest -q -m live

# Optional bench via pytest (MOCK always; LIVE needs Chrome)
EGO_POOL_MOCK=1 pytest -q -m "bench and not live"
pytest -q -m "bench and live"
```

## Benchmark harness

```bash
source .venv/bin/activate

# MOCK only (fast, no Chrome)
EGO_POOL_MOCK=1 python scripts/bench_pool.py --mode MOCK --out benches/latest.json

# LIVE only (real Chrome; records cold lease, CDP ready, navigate, heartbeat, release, warm reuse)
python scripts/bench_pool.py --mode LIVE --out benches/latest.json

# Both modes
python scripts/bench_pool.py --mode BOTH --out benches/latest.json
```

JSON goes to stdout (and `--out` if set). Sample committed under [`benches/SAMPLE_RESULTS.md`](benches/SAMPLE_RESULTS.md) + [`benches/sample.json`](benches/sample.json). Firstmate run report: `ego-runtime-livebench-001`.

## RSS sampling (K-tuning hook)

`ego_pool.rss.sample_tree_rss(pid) -> int | None` walks `/proc` and sums VmRSS (bytes) for the Chromium process tree. Returns `None` on non-Linux or if the pid is gone. Pool status includes `rss_bytes` per slot when available. Use this later to validate the K=5 RAM budget on Firstmate hardware before locking production K.

## Non-goals (MVP)

Electron, CEF/Tauri, Chromium forks, Jev/Open-Jev, Laya-as-browser, packing all agents into one Chromium, cloud overflow (stretch behind same lease API), MCP (not yet — HTTP is the external surface).
