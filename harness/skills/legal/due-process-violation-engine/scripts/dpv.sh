#!/bin/bash
HERMES_VENV="$HOME/.hermes/hermes-agent/venv/bin/python"
SCRIPT="$HOME/.hermes/skills/legal/due-process-violation-engine/scripts/dpv.py"
exec "$HERMES_VENV" "$SCRIPT" "$@"
