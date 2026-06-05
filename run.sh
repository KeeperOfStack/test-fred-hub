#!/usr/bin/env bash
# Test Fred Hub launcher
set -e
cd "$(dirname "$0")"
exec .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8765 "$@"
