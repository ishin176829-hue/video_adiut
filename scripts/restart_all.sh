#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

preserved_names=(
  WORKER_COUNT
  WORKER_CONCURRENCY
  WORKER_POLL_COUNT
  WORKER_STAGE
  PREPROCESS_WORKER_COUNT
  MODEL_WORKER_COUNT
  PREPROCESS_WORKER_CONCURRENCY
  MODEL_WORKER_CONCURRENCY
  WEB_CONCURRENCY
  PORT
  VIDEO_REVIEW_WORKER_CONCURRENCY
  VIDEO_REVIEW_WORKER_POLL_COUNT
  VIDEO_REVIEW_GLOBAL_ACTIVE_LIMIT
  VIDEO_REVIEW_REDIS_CACHE_ENABLED
  VIDEO_REVIEW_FRAME_BATCH_CONCURRENCY
  VIDEO_REVIEW_FRAME_BATCH_SIZE
  VIDEO_REVIEW_FRAME_BATCH_MAX_SPLIT_DEPTH
  VIDEO_REVIEW_MODEL_CALL_TIMEOUT_SECONDS
  VIDEO_REVIEW_MODEL_QPM_LIMIT
  VIDEO_REVIEW_MODEL_QPM_WAIT_SECONDS
  VIDEO_REVIEW_MODEL_CONCURRENCY_LIMIT
  VIDEO_REVIEW_MODEL_CONCURRENCY_WAIT_SECONDS
  VIDEO_REVIEW_DOWNLOAD_CONCURRENCY_PER_PROCESS
  VIDEO_REVIEW_DOWNLOAD_TOTAL_TIMEOUT_SECONDS
  VIDEO_REVIEW_DOWNLOAD_RETRY_ATTEMPTS
  VIDEO_REVIEW_DOWNLOAD_RETRY_DELAY_SECONDS
  VIDEO_REVIEW_DOWNLOAD_RETRY_JITTER_SECONDS
  VIDEO_REVIEW_DOWNLOAD_CONNECT_TIMEOUT_SECONDS
  VIDEO_REVIEW_DOWNLOAD_HOST_CONCURRENCY_LIMIT
  VIDEO_REVIEW_DOWNLOAD_HOST_SLOT_TTL_SECONDS
  VIDEO_REVIEW_DOWNLOAD_HOST_WAIT_SECONDS
  VIDEO_REVIEW_DOWNLOAD_HOST_POLL_SECONDS
  VIDEO_REVIEW_DOWNLOAD_TASK_RETRY_ATTEMPTS
  VIDEO_REVIEW_DOWNLOAD_TASK_RETRY_DELAYS_SECONDS
  VIDEO_REVIEW_DOWNLOAD_RETRY_PROMOTE_COUNT
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

worker_count="${WORKER_COUNT:-10}"
worker_concurrency="${WORKER_CONCURRENCY:-${VIDEO_REVIEW_WORKER_CONCURRENCY:-2}}"
worker_poll_count="${WORKER_POLL_COUNT:-${VIDEO_REVIEW_WORKER_POLL_COUNT:-5}}"
pipeline_mode="${VIDEO_REVIEW_PIPELINE_MODE:-single}"
worker_instance_id="${WORKER_INSTANCE_ID:-$(hostname)-$(date +%s%N)}"

./scripts/stop_api.sh || true
for i in $(seq 1 "${worker_count}"); do
  WORKER_ID="${i}" ./scripts/stop_worker.sh || true
done

worker_pids="$(pgrep -u "$(id -u)" -f 'uv run python -m video_review.worker|python -m video_review.worker' || true)"
if [ -n "${worker_pids}" ]; then
  kill ${worker_pids} 2>/dev/null || true
  sleep 2
fi

./scripts/run_api_nohup.sh
for _ in $(seq 1 30); do
  if curl --max-time 2 -fsS "http://127.0.0.1:${PORT:-8767}/health" >/dev/null; then
    break
  fi
  sleep 1
done
curl --max-time 5 -fsS "http://127.0.0.1:${PORT:-8767}/health"

if [ "${pipeline_mode}" = "staged" ]; then
  preprocess_count="${PREPROCESS_WORKER_COUNT:-4}"
  model_count="${MODEL_WORKER_COUNT:-${worker_count}}"
  preprocess_concurrency="${PREPROCESS_WORKER_CONCURRENCY:-2}"
  model_concurrency="${MODEL_WORKER_CONCURRENCY:-${worker_concurrency}}"
  for i in $(seq 1 "${preprocess_count}"); do
    WORKER_ID="preprocess-${i}" \
      WORKER_INSTANCE_ID="${worker_instance_id}" \
      WORKER_STAGE="preprocess" \
      WORKER_CONCURRENCY="${preprocess_concurrency}" \
      WORKER_POLL_COUNT="${worker_poll_count}" \
      ./scripts/run_worker_nohup.sh
  done
  for i in $(seq 1 "${model_count}"); do
    WORKER_ID="model-${i}" \
      WORKER_INSTANCE_ID="${worker_instance_id}" \
      WORKER_STAGE="model" \
      WORKER_CONCURRENCY="${model_concurrency}" \
      WORKER_POLL_COUNT="${worker_poll_count}" \
      ./scripts/run_worker_nohup.sh
  done
else
  for i in $(seq 1 "${worker_count}"); do
    WORKER_ID="${i}" WORKER_CONCURRENCY="${worker_concurrency}" WORKER_POLL_COUNT="${worker_poll_count}" ./scripts/run_worker_nohup.sh
  done
fi

ps -ef | grep -E "uvicorn video_review.main|video_review.worker" | grep -v grep || true
