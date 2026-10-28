#!/bin/bash
/home/{{USER_FIRST}}/.hermes/hermes-agent/venv/bin/python3 /home/{{USER_FIRST}}/.hermes/skills/legal/db-ingest/scripts/ingest_text.py "$@"
