#!/bin/bash
HERMES_VENV="$HOME/.hermes/hermes-agent/venv/bin/python"
SCRIPT="$HOME/.hermes/skills/legal/appellate-pipeline/scripts/appeal.py"
exec "$HERMES_VENV" "$SCRIPT" "$@"
