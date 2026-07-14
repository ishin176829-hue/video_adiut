# Staged OSS Model Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a staged backend pipeline so video download/preprocessing is separated from model inference, with Redis-based global model QPM limiting.

**Architecture:** Reuse the existing Redis Stream worker, but add stage-aware streams and dispatch. Preprocess stage prepares local artifacts and enqueues the model stage. Model stage reuses existing audit logic and gates each model call through a Redis token bucket.

**Tech Stack:** FastAPI, Redis Streams, PostgreSQL, Python/uv, Google GenAI-compatible API, FFmpeg.

## Global Constraints

- Default mode remains `single`; staged mode is enabled by `VIDEO_REVIEW_PIPELINE_MODE=staged`.
- No database migration is required for this first version.
- Existing OSS STS upload endpoints remain compatible.
- Use TDD for queue, worker, and rate-limiter behavior.

---

### Task 1: Stage-Aware Redis Queue

**Files:**
- Modify: `src/video_review/config.py`
- Modify: `src/video_review/queue.py`
- Test: `tests/test_stage_queue.py`

**Interfaces:**
- Produces: `ReviewQueueStage`, `enqueue_review_stage(review_id, request, stage)`, `dequeue_reviews(..., stage=...)`.
- Produces: `ReviewQueueMessage.stage`, `ReviewQueueMessage.stream`, `ReviewQueueMessage.group`.

- [ ] **Step 1: Write failing tests for stage stream routing.**
- [ ] **Step 2: Implement config and queue helpers.**
- [ ] **Step 3: Run `uv run pytest tests/test_stage_queue.py -q`.**

### Task 2: Worker Stage Dispatch

**Files:**
- Modify: `src/video_review/worker.py`
- Modify: `scripts/run_worker_nohup.sh`
- Modify: `scripts/restart_all.sh`
- Test: `tests/test_worker_stage_dispatch.py`

**Interfaces:**
- Consumes: `ReviewQueueMessage.stage`.
- Produces: `run_worker(stage=...)`.

- [ ] **Step 1: Write failing tests for preprocess/model dispatch.**
- [ ] **Step 2: Add `--stage preprocess|model|single` CLI option.**
- [ ] **Step 3: Make model stage acquire the existing global review slot; preprocess stage does not.**
- [ ] **Step 4: Run worker tests.**

### Task 3: Split Review Task Phases

**Files:**
- Modify: `src/video_review/tasks.py`
- Test: `tests/test_staged_tasks.py`

**Interfaces:**
- Produces: `run_preprocess_stage(review_id, request)`.
- Produces: `run_model_stage(review_id, request)`.
- Keeps: `run_review(review_id, request)` for single mode.

- [ ] **Step 1: Write failing tests that preprocess enqueues model stage after creating frames.**
- [ ] **Step 2: Extract asset preparation/preprocess logic from `run_review`.**
- [ ] **Step 3: Extract model/report logic from `run_review`.**
- [ ] **Step 4: Keep `run_review` as prepare + preprocess + model for single mode.**
- [ ] **Step 5: Run task tests.**

### Task 4: Model QPM Token Bucket

**Files:**
- Modify: `src/video_review/config.py`
- Modify: `src/video_review/queue.py`
- Modify: `src/video_review/tasks.py`
- Test: `tests/test_model_qpm_limiter.py`

**Interfaces:**
- Produces: `model_qpm_slot()`.
- Consumes: Redis key `settings.redis_model_qpm_key`.

- [ ] **Step 1: Write failing tests for Redis token acquisition.**
- [ ] **Step 2: Implement sliding-window token bucket in Redis Lua.**
- [ ] **Step 3: Wrap `_call_model_with_timeout()` with the limiter.**
- [ ] **Step 4: Run limiter tests.**

### Task 5: Deploy and Verify

**Files:**
- Modify: `.env`
- Sync: changed source and tests to `/home/work/sn2s-video-review`.

**Interfaces:**
- Produces: staged workers using existing scripts.

- [ ] **Step 1: Run local `uv run pytest -q`.**
- [ ] **Step 2: Copy files to remote.**
- [ ] **Step 3: Run remote `uv run pytest -q`.**
- [ ] **Step 4: Restart service and verify health, worker count, disk, and active jobs.**
