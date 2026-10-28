#!/bin/bash
# binding-precedent-enforcer wrapper — uses Hermes venv
HERMES_VENV="$HOME/.hermes/hermes-agent/venv/bin/python"
SCRIPT="$HOME/.hermes/skills/legal/binding-precedent-enforcer/scripts/enforce.py"
exec "$HERMES_VENV" "$SCRIPT" "$@"
