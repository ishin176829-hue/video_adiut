#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
if [ -f .env ]; then
  set -a
  source .env
  set +a
fi
if [ -d /home/work/miniforge3/bin ]; then
  export PATH="/home/work/miniforge3/bin:$PATH"
fi
exec uv run python -m video_review.worker \
  --count "${WORKER_POLL_COUNT:-5}" \
  --concurrency "${WORKER_CONCURRENCY:-5}" \
  "$@"
