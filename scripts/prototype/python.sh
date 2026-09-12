#!/usr/bin/env bash
set -euo pipefail
TASK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export PYTHONPATH="$TASK_ROOT/data/prototype/dependencies/site${PYTHONPATH:+:$PYTHONPATH}"
exec bash "$TASK_ROOT/scripts/data/python.sh" "$@"
