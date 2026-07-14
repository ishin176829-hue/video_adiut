#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
port="${PORT:-8767}"
if [ -f logs/api.pid ]; then
  kill "$(cat logs/api.pid)" 2>/dev/null || true
  rm -f logs/api.pid
fi
pkill -f "uvicorn video_review.main:app" 2>/dev/null || true
for _ in $(seq 1 20); do
  if ! ss -ltn "sport = :${port}" | grep -q ":${port}"; then
    exit 0
  fi
  sleep 0.5
done

listener_pids="$(
  ss -ltnp "sport = :${port}" 2>/dev/null \
    | grep -oE 'pid=[0-9]+' \
    | cut -d= -f2 \
    | sort -u \
    || true
)"
if [ -n "${listener_pids}" ]; then
  kill ${listener_pids} 2>/dev/null || true
  sleep 1
fi

listener_pids="$(
  ss -ltnp "sport = :${port}" 2>/dev/null \
    | grep -oE 'pid=[0-9]+' \
    | cut -d= -f2 \
    | sort -u \
    || true
)"
if [ -n "${listener_pids}" ]; then
  kill -9 ${listener_pids} 2>/dev/null || true
fi
