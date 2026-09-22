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

## For AI Agents

### Goal

Install the Slipstream browser pool (skill + CLI over localhost HTTP) so the user can lease Chromium CDP Spaces. Optionally install upstream **Claude `/watch`** (`bradautomates/claude-video`) so video intents compose correctly. Do **not** copy or vendor `claude-video` scripts into the Slipstream repo.

### Boundaries

- **DO NOT** `sudo` unless the user explicitly approved
- **DO NOT** vendor / fork `skills/watch/scripts/*` into Slipstream
- **DO NOT** require a paid Whisper key for Slipstream itself (captions-first `/watch` is free; Whisper is optional upstream)
- **DO NOT** install Monid or paid marketplaces
- Config and Spaces stay outside the agent project tree when possible (`SLIPSTREAM_SPACES_ROOT`, vault root)

### Step 1 — Install Slipstream (Python)

```bash
git clone https://github.com/MubarakHimself/slipstream.git
cd slipstream
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

### Step 2 — Doctor (safe preflight)

```bash
# Real path — mock OFF
unset SLIPSTREAM_MOCK
slipstream doctor
slipstream doctor --json
```

Expect: Chrome + CDP probe OK (or clear install hints), `skill_path` OK, `watch_compose` **WARN** until `/watch` is installed (WARN never fails doctor). Start the pool when ready:

```bash
slipstream serve --port 8755
# other shell:
export SLIPSTREAM_URL=http://127.0.0.1:8755
slipstream doctor   # pool_healthz should OK
```

### Step 3 — Compose `/watch` (optional, recommended for video)

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

### Step 4 — Agent skill

Point the harness at `skills/slipstream/SKILL.md` (package data / repo). Alias intent: `slipstream-browser` (do not name a skill `browser`).

Lifecycle: `lease → drive CDP → heartbeat → release` (or `alert need-human` / `alert done`). Full contract: [skills/slipstream/SKILL.md](../skills/slipstream/SKILL.md).

### Division of labor

| Intent | Route |
|--------|-------|
| Interactive web / forms / QA in Chromium | Slipstream lease + peer CDP driver |
| Video URL or local media path (“watch / summarize / what’s on screen”) | Installed `/watch` skill (`bradautomates/claude-video`) |
| Human takeover of a live leased page | Slipstream `need_human` → tokenized Watch JPEG/HTML (observe-only) |

### Quick reference

| Command | Purpose |
|---------|---------|
| `slipstream doctor [--json]` | Preflight (Chrome, CDP, pool, spaces, skill, **watch_compose**) |
| `slipstream watch-status [--json]` | Compose `/watch` detection only |
| `slipstream serve` | Start pool |
| `slipstream lease\|heartbeat\|alert\|release\|status\|cred …` | Agent CLI |

Upstream `/watch`: https://github.com/bradautomates/claude-video
