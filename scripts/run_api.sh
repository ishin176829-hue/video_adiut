#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p logs
if [ -f .env ]; then
  set -a
  source .env
  set +a
fi
if [ -d /home/work/miniforge3/bin ]; then
  export PATH="/home/work/miniforge3/bin:$PATH"
fi
exec uv run uvicorn video_review.main:app \
  --host "${HOST:-0.0.0.0}" \
  --port "${PORT:-8767}" \
  --workers "${WEB_CONCURRENCY:-1}"
