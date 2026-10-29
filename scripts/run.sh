#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$PROJECT_ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
    printf '%s\n' 'Missing project virtual environment. Run bash scripts/setup-ubuntu.sh first.' >&2
    exit 1
fi
cd -- "$PROJECT_ROOT"
export PYTHONUTF8=1
if [[ $# -eq 0 ]]; then
    set -- run --dry-run
fi
exec "$PYTHON" -B "$PROJECT_ROOT/main.py" "$@"
