#!/usr/bin/env bash
# Local Skylos quality gate (Linux) — no GitHub / no cloud required.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

VENV="${SKYLOS_VENV:-$ROOT/.venv-skylos}"
if [[ ! -x "$VENV/bin/skylos" ]]; then
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -U pip skylos
fi
SKYLOS="$VENV/bin/skylos"

MODE="${1:-advisory}"  # advisory | gate | json
OUT="${2:-skylos-results.json}"

case "$MODE" in
  json)
    "$SKYLOS" . --danger --secrets --quality --ai-defects --json -o "$OUT"
    echo "Wrote $OUT"
    ;;
  advisory)
    "$SKYLOS" . --danger --secrets --quality --ai-defects --json -o "$OUT"
    "$SKYLOS" cicd gate --input "$OUT" --summary --advisory
    ;;
  gate)
    "$SKYLOS" . --danger --secrets --quality --ai-defects --gate
    ;;
  *)
    echo "Usage: $0 [advisory|gate|json] [out.json]" >&2
    exit 2
    ;;
esac
