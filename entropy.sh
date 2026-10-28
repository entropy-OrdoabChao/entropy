#!/usr/bin/env bash
# Entropy startup (macOS/Linux/WSL). Creates the data layout, runs the intake chain once, then serves the desk.
#   ./entropy.sh [HOME_DIR]        HOME_DIR defaults to $ENTROPY_HOME or ~/Entropy
set -euo pipefail
PKG="$(cd "$(dirname "$0")" && pwd)"; export PYTHONPATH="$PKG"
[ -n "${1:-}" ] && export ENTROPY_HOME="$1"
export ENTROPY_HOME="${ENTROPY_HOME:-$HOME/Entropy}"
PY="${PYTHON:-python3}"
"$PY" -c "import fastapi, uvicorn" 2>/dev/null || { echo "Installing requirements (first run)…"; "$PY" -m pip install -q -r "$PKG/requirements.txt"; }
"$PY" -m pipeline.bootstrap
"$PY" -m pipeline.intake_flow --apply || true
PORT="${ENTROPY_PORT:-8646}"
echo
echo "Entropy desk: http://127.0.0.1:$PORT   (home: $ENTROPY_HOME)   Ctrl+C stops it."
exec "$PY" -c "import uvicorn; from core.server import create_app; uvicorn.run(create_app(), host='127.0.0.1', port=int('$PORT'), log_level='warning')"
