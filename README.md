# ego-agent

Personal multi-agent **CDP browser pool** (research → build).

**Not** a fork of CitroLabs ego lite. Inspired by public MIT harness patterns and peer CDP stacks. Original code only.

## Status

Architecture locked — see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and Firstmate report `ego-arch-pool-001`.

**MVP scaffold (this branch):** Browser Pool Manager stub — lease / heartbeat / release, hard **K=5**, warm **W=1**, Space = Chromium `user-data-dir`, Linux-first, local CDP.

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
  rss.py           # sample_tree_rss(pid) — /proc tree RSS hook
  pool.py          # BrowserPool lease/heartbeat/release/evict
  api.py           # HTTP JSON API (stdlib)
  __main__.py      # python -m ego_pool
tests/             # pytest (mocked by default; @pytest.mark.live for Chrome)
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

# Real Chrome (system google-chrome / chromium)
python -m ego_pool --port 8755
```

Base URL: `http://127.0.0.1:8755` — see [`docs/POOL_API.md`](docs/POOL_API.md).

Example:

```bash
curl -s -X POST http://127.0.0.1:8755/v1/leases \
  -H 'Content-Type: application/json' \
  -d '{"agent_id":"a1","space_id":"task-1","mode":"isolated"}'
```

## Tests

```bash
source .venv/bin/activate
# Unit tests (mocked launcher — no browsers needed)
EGO_POOL_MOCK=1 pytest -q -m "not live"

# Optional live smoke (requires Chrome on PATH)
pytest -q -m live
```

## RSS sampling (K-tuning hook)

`ego_pool.rss.sample_tree_rss(pid) -> int | None` walks `/proc` and sums VmRSS (bytes) for the Chromium process tree. Returns `None` on non-Linux or if the pid is gone. Pool status includes `rss_bytes` per slot when available. Use this later to validate the K=5 RAM budget on Firstmate hardware before locking production K.

## Non-goals (MVP)

Electron, CEF/Tauri, Chromium forks, Jev/Open-Jev, Laya-as-browser, packing all agents into one Chromium, cloud overflow (stretch behind same lease API).
