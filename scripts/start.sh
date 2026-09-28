#!/usr/bin/env bash
# Linux counterpart of start.ps1:
#   ./scripts/start.sh --profile profiles/qwen3.8-27b.json --port 8000
# Uses the repo's .venv when present (see setup_linux.sh), else python3 on PATH.
# All arguments are passed through to server_manager.py.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
py="$root/.venv/bin/python"
[ -x "$py" ] || py="$(command -v python3)"

exec "$py" "$root/server_manager.py" "$@"
