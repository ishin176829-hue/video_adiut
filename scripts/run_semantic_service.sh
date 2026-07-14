#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

exec uv run uvicorn video_review.semantic_service:app \
  --host "${HOST:-0.0.0.0}" \
  --port "${PORT:-8768}"
