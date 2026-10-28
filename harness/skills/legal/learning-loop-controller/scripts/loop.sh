#!/bin/bash
HERMES_VENV="$HOME/.hermes/hermes-agent/venv/bin/python"
SCRIPT="$HOME/.hermes/skills/legal/learning-loop-controller/scripts/loop.py"
exec "$HERMES_VENV" "$SCRIPT" "$@"
