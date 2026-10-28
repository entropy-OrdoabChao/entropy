#!/bin/bash
HERMES_VENV="$HOME/.hermes/hermes-agent/venv/bin/python"
SCRIPT="$HOME/.hermes/skills/legal/precedent-cache/scripts/cache.py"
exec "$HERMES_VENV" "$SCRIPT" "$@"
