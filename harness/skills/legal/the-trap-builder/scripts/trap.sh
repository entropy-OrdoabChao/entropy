#!/bin/bash
HERMES_VENV="$HOME/.hermes/hermes-agent/venv/bin/python"
SCRIPT="$HOME/.hermes/skills/legal/the-trap-builder/scripts/trap.py"
exec "$HERMES_VENV" "$SCRIPT" "$@"
