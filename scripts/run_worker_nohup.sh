#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p logs
preserved_names=(
  WORKER_ID
  CONSUMER
  WORKER_CONCURRENCY
  WORKER_POLL_COUNT
  WORKER_STAGE
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
worker_id="${WORKER_ID:-default}"
worker_instance_id="${WORKER_INSTANCE_ID:-$(hostname)-$(date +%s%N)-$$}"
python_bin="${PYTHON_BIN:-.venv/bin/python}"
nohup "${python_bin}" -m video_review.worker \
  --consumer "${CONSUMER:-video-review-${worker_id}-${worker_instance_id}}" \
  --stage "${WORKER_STAGE:-single}" \
  --count "${WORKER_POLL_COUNT:-5}" \
  --concurrency "${WORKER_CONCURRENCY:-5}" \
  > "logs/worker-${worker_id}.log" 2>&1 &
echo $! > "logs/worker-${worker_id}.pid"
echo "started worker=${worker_id} pid=$(cat "logs/worker-${worker_id}.pid") log=$(pwd)/logs/worker-${worker_id}.log"
