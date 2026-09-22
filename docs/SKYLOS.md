# Skylos CI

[Skylos](https://docs.skylos.dev) — dead code / SAST / quality gate (what QMX-style projects often use in CI).

## Modes

- **Local gate (no cloud):** `skylos . --danger --secrets --quality --ai-defects --gate`
- **GitHub Actions:** `.github/workflows/skylos.yml` (scaffolded via `skylos cicd init`)
- **Cloud upload / dashboard:** needs `SKYLOS_TOKEN` or OIDC project link — not enabled by default here

## Local run (this machine)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install skylos
skylos . --danger --secrets --quality --ai-defects --gate
```

When the captain's laptop is back, we can align thresholds/suppressions with the QMX Skylos project if one exists.
