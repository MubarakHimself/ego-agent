# Skylos (local Linux)

[Skylos](https://docs.skylos.dev) — dead-code / SAST / quality gate.

**Preferred setup:** run on Linux locally. No GitHub Actions or cloud token required for a local gate.

## One-shot

```bash
./scripts/skylos_local.sh advisory   # default while adopting
./scripts/skylos_local.sh gate       # hard fail on thresholds
./scripts/skylos_local.sh json out.json
```

First run creates `.venv-skylos` and installs `skylos`.

## Equivalent raw CLI

```bash
skylos . --danger --secrets --quality --ai-defects --gate
```

## Vulture (dead code)

Companion gate on every ship:

```bash
pip install -e ".[dev]"
./scripts/run_vulture.sh
```

Whitelist justified false positives only in `scripts/vulture_whitelist.py`.

