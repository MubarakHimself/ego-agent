# Slipstream

Fast multi-agent browser lanes over CDP. Inspired by public ego-lite patterns — not a fork of CitroLabs ego lite.

# slipstream

Personal multi-agent **CDP browser pool** (research → build).

**Not** a fork of CitroLabs ego lite. Inspired by public MIT harness patterns and peer CDP stacks. Original code only.

## Out-of-box (one path)

Clean venv → install → doctor (live) **or** chrome-less demo. **No MCP.**

```bash
git clone https://github.com/MubarakHimself/slipstream.git && cd slipstream
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Live preflight — needs Chrome (or SLIPSTREAM_CHROME).
# Bare `slipstream doctor` (mock unset): missing Chrome → FAIL exit 1.
# WARN/SKIP (e.g. pool not up yet) still allow exit 0 when Chrome is present.
slipstream doctor

# Chrome-less path (do not use bare doctor):
make demo
# or: SLIPSTREAM_MOCK=1 slipstream doctor   # chrome missing → WARN, exit 0
#     SLIPSTREAM_MOCK=1 slipstream serve --port 8755
# equivalent: SLIPSTREAM_MOCK=1 python scripts/oob_smoke.py

# Live serve (after Chrome + doctor PASS):
# unset SLIPSTREAM_MOCK
# slipstream serve --port 8755
```

Live Chrome: unset `SLIPSTREAM_MOCK`, install `google-chrome` / `chromium` (or set `SLIPSTREAM_CHROME`), then `slipstream serve --port 8755`. Fuller notes: [`docs/install.md`](docs/install.md).

## Status

Architecture locked — see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and Firstmate report `ego-arch-pool-001`.

**MVP:** Browser Pool Manager — lease / heartbeat / release, hard **K=5**, warm **W=1**, Space = Chromium `user-data-dir`, Linux-first, local CDP.

**External surface:** localhost **HTTP JSON API** (`docs/POOL_API.md`) is primary. Agents should use the **`slipstream` CLI** + skill doc ([`skills/slipstream/SKILL.md`](skills/slipstream/SKILL.md); alias intent `slipstream-browser`) — skill+CLI over HTTP/CDP. Includes **`slipstream doctor`** preflight. **No MCP server** — do not look for one; MCP was explicitly deferred/cancelled.

## Constraints

- No Electron / Chromium fork / TypeSafe Jev / Open-Jev
- Laya optional later (decision head only — not a browser)
- No proprietary ego binary / ego-lite source theft
- Agents never spawn Chrome themselves — only the pool does
- No MCP server in this repo

## Layout

```text
slipstream/          # Pool service package
  config.py          # K=5, W=1, idle_ttl=300, spaces_root, cdp_base_port
  models.py          # SlotState, Lease
  launcher.py        # ChromiumLauncher (CDP + user-data-dir); mock via SLIPSTREAM_MOCK=1
  cdp_http.py        # Thin DevTools HTTP helpers (smoke/bench; not full driver)
  rss.py             # sample_tree_rss(pid) — /proc tree RSS hook
  pool.py            # BrowserPool lease/heartbeat/alert/release/evict
  alerts.py          # need_human + task_done (no secrets in payload)
  api.py             # HTTP JSON API (stdlib)
  cli.py             # Agent HTTP client helpers (urllib)
  __main__.py        # slipstream / python -m slipstream (serve|lease|heartbeat|alert|release|status|doctor)
  doctor.py          # Preflight: Chrome, CDP, healthz, spaces, skill
skills/
  slipstream/SKILL.md  # Agent skill (alias: slipstream-browser); lifecycle + doctor
scripts/
  oob_smoke.py       # Thin mock OOB smoke (doctor + serve + lease cycle)
  bench_pool.py      # LIVE + MOCK pool speed benchmark
  skylos_local.sh    # Skylos quality gate (also: run_skylos.sh)
  run_vulture.sh     # Vulture dead-code gate
tests/               # pytest (mocked by default; @pytest.mark.live / bench)
benches/             # Sample benchmark outputs (committed samples)
docs/
  ARCHITECTURE.md    # Captain lock
  POOL_API.md        # HTTP endpoints (+ CLI as another client)
  install.md         # OOB install: Chrome, SLIPSTREAM_*, mock vs live, /watch
data/spaces/         # Runtime Space profiles (gitignored)
```

**Space path convention:** `{spaces_root}/{space_id}/` → Chromium `--user-data-dir`.

## Quick start (detail)

Same path as **Out-of-box** above. `pip install -e ".[dev]"` installs the `slipstream` console script (also: `python -m slipstream …`).

Base URL: `http://127.0.0.1:8755` — see [`docs/POOL_API.md`](docs/POOL_API.md).
Agent skill: [`skills/slipstream/SKILL.md`](skills/slipstream/SKILL.md).
Full install (Chrome / `SLIPSTREAM_*` / mock vs live / compose `/watch`): [`docs/install.md`](docs/install.md).

### Agent CLI (against a running pool)

```bash
export SLIPSTREAM_URL=http://127.0.0.1:8755   # optional; this is the default

slipstream lease --agent-id a1 --space-id task-1
# → prints lease JSON (lease_id, slot_id, …; cdp_* only if SLIPSTREAM_EXPOSE_RAW_CDP=1)

slipstream heartbeat --lease-id <lease_id>
slipstream alert need-human --lease-id <lease_id> --reason captcha
slipstream alert done --lease-id <lease_id> --summary "finished"
slipstream release --lease-id <lease_id>
slipstream status
```

### Doctor (preflight)

```bash
# Real path — mock OFF (out-of-box). Checks Chrome, CDP probe, pool healthz,
# Spaces root, skills/slipstream/SKILL.md (alias: slipstream-browser), and
# optional composed /watch (watch_compose WARN if missing — never fails).
slipstream doctor
slipstream doctor --json

# Compose /watch detection only (WARN if missing — never fails the process)
slipstream watch-status
slipstream watch-status --json
```

Exit 0 if no failures (warnings/skips allowed, e.g. pool not yet started or
`/watch` not installed). Client `--url` / `SLIPSTREAM_URL` are loopback-only
by default; set `SLIPSTREAM_ALLOW_REMOTE_URL=1` for remote pools.

curl still works (HTTP is primary):

```bash
curl -s -X POST http://127.0.0.1:8755/v1/leases \
  -H 'Content-Type: application/json' \
  -d '{"agent_id":"a1","space_id":"task-1"}'

# Heartbeat + release (DELETE only — no POST /release alias)
curl -s -X POST http://127.0.0.1:8755/v1/leases/<lease_id>/heartbeat
curl -s -X POST http://127.0.0.1:8755/v1/leases/<lease_id>/alerts \
  -H 'Content-Type: application/json' \
  -d '{"event":"need_human","reason":"captcha","detail":"challenge"}'
curl -s -X DELETE http://127.0.0.1:8755/v1/leases/<lease_id>
```

Spaces are exclusive while leased (second agent gets HTTP 409). Warm slots (from explicit DELETE only) with a matching live `space_id` are reused without relaunch. Idle / hard-TTL always stop Chromium.

Drive a leased browser via **pool HTTP** (`navigate` / `eval` / `cred fill` + act/confirm — ladder server-enforced). Raw CDP tip (urls + ports) is omitted from lease/status JSON by default; set `SLIPSTREAM_EXPOSE_RAW_CDP=1` for Playwright `connectOverCDP` / agent-browser (honor-system for nav/eval; vault fill stays pool-only). Smoke/bench may use `slipstream.cdp_http` when raw CDP is exposed.

## Tests & quality gates

```bash
source .venv/bin/activate
pip install -e ".[dev]"   # pytest + vulture

# Unit tests (mocked launcher — no browsers needed)
SLIPSTREAM_MOCK=1 pytest -q -m "not live and not bench and not live_stress"

# Optional live smoke only (requires Chrome on PATH or SLIPSTREAM_CHROME)
# lease → CDP ready → navigate example.com → title check → heartbeat → release → warm reuse
pytest -q -m live

# Optional bench via pytest (-m bench only; LIVE needs Chrome, skips if SLIPSTREAM_MOCK=1)
SLIPSTREAM_MOCK=1 pytest -q -m bench          # MOCK timings
pytest -q -m bench                          # LIVE timings (uses scripts/bench_pool.py)
# Or run the harness directly:
#   python scripts/bench_pool.py --mode LIVE|MOCK|BOTH

# Skylos (dead-code / SAST / quality) — see docs/SKYLOS.md
./scripts/skylos_local.sh advisory   # or: ./scripts/run_skylos.sh gate

# Vulture dead-code
./scripts/run_vulture.sh
```

## Benchmark harness

```bash
source .venv/bin/activate

# MOCK only (fast, no Chrome)
SLIPSTREAM_MOCK=1 python scripts/bench_pool.py --mode MOCK --out benches/latest.json

# LIVE only (real Chrome; records cold lease, CDP ready, navigate, heartbeat, release, warm reuse)
python scripts/bench_pool.py --mode LIVE --out benches/latest.json

# Both modes
python scripts/bench_pool.py --mode BOTH --out benches/latest.json
```

JSON goes to stdout (and `--out` if set). Sample committed under [`benches/SAMPLE_RESULTS.md`](benches/SAMPLE_RESULTS.md) + [`benches/sample.json`](benches/sample.json). Firstmate run report: `ego-runtime-livebench-001`.

## RSS sampling (K-tuning hook)

`slipstream.rss.sample_tree_rss(pid) -> int | None` walks `/proc` and sums VmRSS (bytes) for the Chromium process tree. Returns `None` on non-Linux or if the pid is gone. Pool status includes `rss_bytes` per slot when available. Use this later to validate the K=5 RAM budget on Firstmate hardware before locking production K.

## Non-goals (MVP)

Electron, CEF/Tauri, Chromium forks, Jev/Open-Jev, Laya-as-browser, packing all agents into one Chromium, cloud overflow (stretch behind same lease API), **MCP** (cancelled — HTTP + skill/CLI is the agent surface).
