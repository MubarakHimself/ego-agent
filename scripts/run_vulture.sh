#!/usr/bin/env bash
# Vulture dead-code gate for Slipstream.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if ! command -v vulture >/dev/null 2>&1; then
  echo "vulture not on PATH — install with: pip install -e '.[dev]'" >&2
  exit 2
fi

WHITELIST="$ROOT/scripts/vulture_whitelist.py"
# Paths first, then options (vulture 2.16 parses cleanly this way).
ARGS=(slipstream)
if [[ -f "$WHITELIST" ]]; then
  ARGS+=("$WHITELIST")
fi
ARGS+=(--min-confidence 80 --exclude "*/tests/*,*/.venv*,*/.venv-skylos*")

echo "+ vulture ${ARGS[*]}"
vulture "${ARGS[@]}"
