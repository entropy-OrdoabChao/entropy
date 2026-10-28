#!/bin/bash
HERMES_VENV="$HOME/.hermes/hermes-agent/venv/bin/python"
SCRIPT="$HOME/.hermes/skills/legal/court-practice-auditor/scripts/audit.py"
exec "$HERMES_VENV" "$SCRIPT" "$@"
