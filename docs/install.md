# Slipstream — Installation Guide

## For Humans

Paste this to your AI agent:

```
Install Slipstream for me using https://raw.githubusercontent.com/MubarakHimself/slipstream/main/docs/install.md
```

After Slipstream is up, for **video** URL/path intents (YouTube, Loom, local `.mp4`, …) also install the composed `/watch` skill — Slipstream routes those intents; it does **not** vendor yt-dlp / ffmpeg / Whisper:

```
Also install bradautomates/claude-video /watch using the Compose /watch section of that install.md
```

---

## Out-of-box path (clean venv)

```bash
git clone https://github.com/MubarakHimself/slipstream.git
cd slipstream
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # console script: slipstream

# Live preflight — needs Chrome (or SLIPSTREAM_CHROME).
# Bare `slipstream doctor` (mock unset): missing Chrome → FAIL exit 1.
# WARN/SKIP (e.g. pool not up) still allow exit 0 when Chrome is present.
slipstream doctor

# Chrome-less path (do not use bare doctor):
make demo                        # or: SLIPSTREAM_MOCK=1 python scripts/oob_smoke.py
# or: SLIPSTREAM_MOCK=1 slipstream doctor   # chrome missing → WARN, exit 0
#     SLIPSTREAM_MOCK=1 slipstream serve --port 8755

# Live Chrome serve (after doctor PASS):
# unset SLIPSTREAM_MOCK; slipstream serve --port 8755
```

**No MCP server** — HTTP JSON API + `slipstream` CLI + skill doc only.

---

## For AI Agents

### Goal

Install the Slipstream browser pool (skill + CLI over localhost HTTP) so the user can lease Chromium CDP Spaces. Optionally install upstream **Claude `/watch`** (`bradautomates/claude-video`) so video intents compose correctly. Do **not** copy or vendor `claude-video` scripts into the Slipstream repo.

### Boundaries

- **DO NOT** `sudo` unless the user explicitly approved
- **DO NOT** vendor / fork `skills/watch/scripts/*` into Slipstream
- **DO NOT** require a paid Whisper key for Slipstream itself (captions-first `/watch` is free; Whisper is optional upstream)
- **DO NOT** install Monid or paid marketplaces
- **DO NOT** look for or add an MCP server — cancelled; use HTTP + CLI
- Config and Spaces stay outside the agent project tree when possible (`SLIPSTREAM_SPACES_ROOT`, vault root)

### Step 1 — Install Slipstream (Python)

```bash
git clone https://github.com/MubarakHimself/slipstream.git
cd slipstream
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Editable install is the supported path. Console entry point: `slipstream` (same as `python -m slipstream`).

### Step 2 — Doctor (safe preflight)

```bash
# Real path — mock OFF
unset SLIPSTREAM_MOCK
slipstream doctor
slipstream doctor --json
```

Expect: Chrome + CDP probe OK (or clear install hints), `skill_path` OK, `watch_compose` **WARN** until `/watch` is installed (WARN never fails doctor). Pool not running yet → `pool_healthz` **WARN** (still exit 0).

Mock path (no Chrome required):

```bash
SLIPSTREAM_MOCK=1 slipstream doctor
# chrome missing → WARN (not FAIL); cdp_probe → SKIP; exit 0 if no other failures
```

Start the pool when ready:

```bash
# Mock (CI / laptop without Chrome)
SLIPSTREAM_MOCK=1 slipstream serve --port 8755

# Live
unset SLIPSTREAM_MOCK
slipstream serve --port 8755

# other shell:
export SLIPSTREAM_URL=http://127.0.0.1:8755
slipstream doctor   # pool_healthz should OK
```

### Step 3 — Chrome binary (live path)

Doctor and the pool resolve Chrome in this order:

1. `SLIPSTREAM_CHROME` — absolute path **or** command name (must exist / be executable; no PATH fallthrough if set but unusable)
2. PATH candidates: `google-chrome-stable`, `google-chrome`, `chromium`, `chromium-browser`, …
3. Common Linux paths under `/usr/bin` and `/opt/google/chrome`

Install examples (pick one; no sudo here unless the user approved):

| Distro family | Package hint |
|---------------|--------------|
| Debian/Ubuntu | `google-chrome-stable` or `chromium` |
| Fedora | `google-chrome-stable` or `chromium` |
| Arch | `google-chrome` (AUR) or `chromium` |

Override:

```bash
export SLIPSTREAM_CHROME=/usr/bin/google-chrome-stable
slipstream doctor
```

Headful (not headless): `SLIPSTREAM_HEADLESS=0`.

### Step 4 — `SLIPSTREAM_*` basics

| Variable | Default / notes |
|----------|-----------------|
| `SLIPSTREAM_MOCK` | unset/`0` = real Chrome; `1` = mock launcher (no browsers) |
| `SLIPSTREAM_CHROME` | Chrome binary path/name |
| `SLIPSTREAM_URL` | CLI client base URL (default `http://127.0.0.1:8755`) |
| `SLIPSTREAM_PORT` | Pool listen port when serving |
| `SLIPSTREAM_SPACES_ROOT` | Chromium `user-data-dir` root (default `./data/spaces`) |
| `SLIPSTREAM_VAULT_ROOT` | Vault outside spaces (default `./data/vault`) |
| `SLIPSTREAM_ARTIFACTS_ROOT` | Lease downloads/uploads root |
| `SLIPSTREAM_K` / `SLIPSTREAM_W` | Hard cap / warm slots (defaults 5 / 1) |
| `SLIPSTREAM_HEADLESS` | `1` (default) or `0` |
| `SLIPSTREAM_SKILL_PATH` | Override skill markdown path |
| `SLIPSTREAM_EXPOSE_RAW_CDP` | `1` to put CDP tip on lease/status JSON (default off) |
| `SLIPSTREAM_ALLOW_REMOTE_URL` | `1` to allow non-loopback `SLIPSTREAM_URL` |

Prefer the `SLIPSTREAM_*` prefix. Full API/env list: [`POOL_API.md`](POOL_API.md).

### Step 5 — Mock vs live

| Mode | When | Doctor | Serve |
|------|------|--------|-------|
| **Mock** | CI, first demo, no Chrome | `SLIPSTREAM_MOCK=1`; chrome missing → WARN | `SLIPSTREAM_MOCK=1 slipstream serve` |
| **Live** | Real CDP Spaces | mock OFF; chrome + CDP probe must OK | `slipstream serve` (system Chrome or `SLIPSTREAM_CHROME`) |

Mock still exercises lease / heartbeat / release / alerts over the same HTTP API. Live adds real Chromium + CDP. Agents never spawn Chrome themselves — only the pool does.

Thin one-shot mock smoke:

```bash
make demo
# or: SLIPSTREAM_MOCK=1 python scripts/oob_smoke.py
```

### Step 6 — Compose `/watch` (optional, recommended for video)

Slipstream is the **browser pool**. Video “what’s in this clip / summarize this URL” intents belong to upstream `/watch`.

**Install (pick one host):**

| Host | Command |
|------|---------|
| Agent Skills CLI (Codex, Cursor, Gemini, …) | `npx skills add bradautomates/claude-video -g` |
| Claude Code marketplace | `/plugin marketplace add bradautomates/claude-video` then `/plugin install watch@claude-video` |
| Manual / Firstmate box | clone repo and symlink `skills/watch` → `~/.agents/skills/watch` (or set `SLIPSTREAM_WATCH_SKILL`) |

**Known paths `slipstream doctor` probes** (PASS if a valid `SKILL.md` is found; never FAIL if missing):

- `$SLIPSTREAM_WATCH_SKILL` (file path override)
- `./skills/watch/SKILL.md`
- `~/.claude/skills/watch/SKILL.md`
- `~/.codex/skills/watch/SKILL.md`
- `~/.cursor/skills/watch/SKILL.md`
- `~/.agents/skills/watch/SKILL.md`
- `~/.openclaw/skills/watch/SKILL.md`
- `~/.gemini/skills/watch/SKILL.md`
- `~/.slipstream/skills/watch/SKILL.md`
- Claude Code plugin cache under `~/.claude/plugins/**/skills/watch/SKILL.md`

Re-check:

```bash
slipstream doctor          # watch_compose → OK when found, WARN when missing
slipstream watch-status    # thin JSON status (optional CLI)
slipstream watch-status --json
```

Captions cover most public videos for free. Whisper (Groq/OpenAI) is **optional** upstream when captions are absent — do not block Slipstream install on a Whisper key.

### Step 7 — Agent skill

Point the harness at `skills/slipstream/SKILL.md` (package data / repo). Alias intent: `slipstream-browser` (do not name a skill `browser`).

Lifecycle: `lease → drive CDP → heartbeat → release` (or `alert need-human` / `alert done`). Full contract: [skills/slipstream/SKILL.md](../skills/slipstream/SKILL.md).

### Division of labor

| Intent | Route |
|--------|-------|
| Interactive web / forms / QA in Chromium | Slipstream lease + peer CDP driver |
| Video URL or local media path (“watch / summarize / what’s on screen”) | Installed `/watch` skill (`bradautomates/claude-video`) |
| Human takeover of a live leased page | Slipstream `need_human` → tokenized Watch; Confirm → exclusive pair-browse; Cede returns drive |

### Quick reference

| Command | Purpose |
|---------|---------|
| `slipstream doctor [--json]` | Preflight (Chrome, CDP, pool, spaces, skill, **watch_compose**) |
| `slipstream watch-status [--json]` | Compose `/watch` detection only |
| `slipstream serve` | Start pool |
| `make demo` / `scripts/oob_smoke.py` | Mock OOB smoke (doctor + lease cycle) |
| `slipstream lease\|heartbeat\|alert\|release\|status\|cred …` | Agent CLI |

Upstream `/watch`: https://github.com/bradautomates/claude-video
