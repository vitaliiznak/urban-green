#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")" || exit 1

if [ ! -f .env ]; then
  cp .env.example .env
fi

if [ ! -x .venv/bin/python ]; then
  if command -v uv >/dev/null 2>&1; then
    uv venv .venv
  else
    python3 -m venv .venv
  fi
fi

if [ ! -x .venv/bin/uvicorn ]; then
  if command -v uv >/dev/null 2>&1; then
    uv pip install -p .venv/bin/python -e ".[dev]"
  else
    .venv/bin/python -m pip install -e ".[dev]"
  fi
fi

port="${PORT:-8000}"
while lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; do
  echo "Port ${port} is busy; trying $((port + 1))"
  port=$((port + 1))
done

echo "Urban Green → http://localhost:${port}"
exec .venv/bin/uvicorn server.app:app --reload --host 127.0.0.1 --port "${port}"
