#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
worker_id="${WORKER_ID:-default}"
pid_file="logs/worker-${worker_id}.pid"
if [ -f "${pid_file}" ]; then
  kill "$(cat "${pid_file}")" 2>/dev/null || true
  rm -f "${pid_file}"
fi
