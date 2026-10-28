#!/usr/bin/env bash
set -euo pipefail
PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$PROJECT_ROOT"
exec "$PROJECT_ROOT/.venv/bin/python" -B "$PROJECT_ROOT/scripts/install-user-timers.py" "$@"
