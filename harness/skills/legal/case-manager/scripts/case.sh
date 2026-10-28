#!/bin/bash
HERMES_VENV="$HOME/.hermes/hermes-agent/venv/bin/python"
SCRIPT="$HOME/.hermes/skills/legal/case-manager/scripts/case.py"
exec "$HERMES_VENV" "$SCRIPT" "$@"
