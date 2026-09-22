#!/usr/bin/env bash
# Wrapper — same as scripts/skylos_local.sh (docs / CI convenience).
exec "$(cd "$(dirname "$0")" && pwd)/skylos_local.sh" "$@"
