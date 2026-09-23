#!/usr/bin/env bash
# Thin wrapper — same as: SLIPSTREAM_MOCK=1 python scripts/oob_smoke.py
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export SLIPSTREAM_MOCK=1
exec python3 "$ROOT/scripts/oob_smoke.py" "$@"
