#!/usr/bin/env sh
# Local dev server with auto-reload. Copy .env.example to .env and add a key to enable the agent.
cd "$(dirname "$0")" || exit 1
exec .venv/bin/uvicorn server.app:app --reload --port "${PORT:-8000}"
