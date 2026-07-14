#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p logs
preserved_names=(
  HOST
  PORT
  WEB_CONCURRENCY
  PYTHON_BIN
  VIDEO_REVIEW_GLOBAL_ACTIVE_LIMIT
  VIDEO_REVIEW_REDIS_CACHE_ENABLED
  VIDEO_REVIEW_FRAME_BATCH_CONCURRENCY
  VIDEO_REVIEW_FRAME_BATCH_SIZE
  VIDEO_REVIEW_FRAME_BATCH_MAX_SPLIT_DEPTH
  VIDEO_REVIEW_MODEL_CALL_TIMEOUT_SECONDS
  VIDEO_REVIEW_DOWNLOAD_CONCURRENCY_PER_PROCESS
  VIDEO_REVIEW_DOWNLOAD_TOTAL_TIMEOUT_SECONDS
  VIDEO_REVIEW_FFMPEG_THREADS
  VIDEO_REVIEW_FFMPEG_FILTER_THREADS
)
preserved_values=()
for name in "${preserved_names[@]}"; do
  if [ "${!name+x}" = "x" ]; then
    preserved_values+=("${name}=${!name}")
  fi
done
if [ -f .env ]; then
  set -a
  source .env
  set +a
fi
for value in "${preserved_values[@]}"; do
  export "${value}"
done
if [ -d /home/work/miniforge3/bin ]; then
  export PATH="/home/work/miniforge3/bin:$PATH"
fi
python_bin="${PYTHON_BIN:-.venv/bin/python}"
nohup "${python_bin}" -m uvicorn video_review.main:app \
  --host "${HOST:-0.0.0.0}" \
  --port "${PORT:-8767}" \
  --workers "${WEB_CONCURRENCY:-1}" \
  > logs/api.log 2>&1 &
echo $! > logs/api.pid
echo "started pid=$(cat logs/api.pid) log=$(pwd)/logs/api.log"
