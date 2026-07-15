# Model Workflow Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make model-stage failures recover through a durable Redis workflow until the 30-minute deadline without producing manual-review placeholder results.

**Architecture:** Reuse the existing staged Redis Streams pipeline. Add one model retry sorted set, pure retry-plan helpers, strict model-channel behavior, and a real circuit half-open transition. PostgreSQL jobs and events remain the durable audit log; successful frame caches make model-stage replay idempotent.

**Tech Stack:** Python 3.12+, asyncio, Redis Streams/ZSET/Lua, PostgreSQL, Pydantic, pytest, uv.

## Global Constraints

- Final technical failure means no complete machine result within 30 minutes.
- At most one final technical failure is allowed in every rolling 10,000 tasks.
- Technical errors must never be converted to `manual_review` findings.
- No new runtime dependency or external workflow framework.
- Existing unrelated worktree changes and load-test artifacts must remain untouched.

---

### Task 1: Correct circuit health semantics

**Files:**
- Modify: `src/video_review/queue.py`
- Test: `tests/test_model_qpm_limiter.py`

**Interfaces:**
- Consumes: `record_model_call_result(success, error_kind, api_key_id)`
- Produces: circuit health that ignores contract errors and supports one half-open probe.

- [ ] **Step 1: Write failing tests**

Add tests proving `parse` and `validation` errors do not enter the global health window, an open deadline is not extended, and an expired open circuit permits a half-open probe.

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest -q tests/test_model_qpm_limiter.py`

Expected: new assertions fail because all errors are currently counted and expired open state retains multiplier zero.

- [ ] **Step 3: Implement minimal state-machine changes**

Use an allowlist for global availability errors:

```python
GLOBAL_CIRCUIT_ERROR_KINDS = {"rate_limit", "transient"}
```

Preserve an existing unexpired `open_until_ms`, transition expired `open` to `half_open` with effective concurrency one, close and clear health on probe success, and reopen on probe failure.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `uv run pytest -q tests/test_model_qpm_limiter.py tests/test_model_retry.py`

- [ ] **Step 5: Commit**

```bash
git add src/video_review/queue.py tests/test_model_qpm_limiter.py
git commit -m "fix: isolate model circuit from contract errors"
```

### Task 2: Add durable model-stage retry queue

**Files:**
- Create: `src/video_review/workflow.py`
- Modify: `src/video_review/config.py`
- Modify: `src/video_review/queue.py`
- Modify: `src/video_review/worker.py`
- Test: `tests/test_workflow_retry.py`
- Test: `tests/test_stage_queue.py`

**Interfaces:**
- Produces: `plan_stage_retry(request, stage, reason, error_kind, started_at, now) -> WorkflowRetryPlan | None`
- Produces: `schedule_stage_retry(review_id, request, stage, delay_seconds, attempt) -> str | None`
- Produces: `promote_due_stage_retries(stage) -> int`

- [ ] **Step 1: Write failing pure retry-plan tests**

Cover incrementing attempts, configured delay selection, stable deadline propagation, and refusal to schedule past 30 minutes.

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest -q tests/test_workflow_retry.py tests/test_stage_queue.py`

Expected: import or missing-function failures.

- [ ] **Step 3: Implement retry planning and generic Redis promotion**

Store stage retry state in request metadata and use a generic Lua promotion script that atomically moves due entries from a stage ZSET to the target Stream.

- [ ] **Step 4: Promote model retries from model workers**

At the top of the model worker loop call:

```python
promoted = await promote_due_stage_retries(ReviewQueueStage.MODEL)
```

- [ ] **Step 5: Run tests and verify GREEN**

Run: `uv run pytest -q tests/test_workflow_retry.py tests/test_stage_queue.py tests/test_worker_stage_dispatch.py`

- [ ] **Step 6: Commit**

```bash
git add src/video_review/workflow.py src/video_review/config.py src/video_review/queue.py src/video_review/worker.py tests/test_workflow_retry.py tests/test_stage_queue.py
git commit -m "feat: add durable model stage retries"
```

### Task 3: Require complete model results and schedule stage recovery

**Files:**
- Modify: `src/video_review/tasks.py`
- Test: `tests/test_task_resilience.py`
- Test: `tests/test_staged_tasks.py`

**Interfaces:**
- Consumes: `plan_stage_retry` and `schedule_stage_retry`
- Produces: model-stage retry state instead of manual-review placeholders or terminal failure.

- [ ] **Step 1: Write failing behavior tests**

Add tests proving exhausted 429, circuit, parse and validation errors escape the frame batch; subtitle and narrative transient failures escape; `run_model_stage` schedules a model retry and leaves the job pending; deadline exhaustion marks the job failed.

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest -q tests/test_task_resilience.py tests/test_staged_tasks.py`

Expected: current code returns manual-review findings, skips subtitle/narrative, or marks the model job failed immediately.

- [ ] **Step 3: Remove technical manual-review fallback**

Keep batch splitting for transient frame failures, but re-raise after the final split/call. Propagate retryable subtitle and narrative failures.

- [ ] **Step 4: Add model-stage retry scheduling**

On retryable model errors, calculate a retry plan, enqueue it, update job phase to `model_retry_wait`, emit an event, and return without callback or cleanup. Only deadline exhaustion or permanent errors become terminal.

- [ ] **Step 5: Run tests and verify GREEN**

Run: `uv run pytest -q tests/test_task_resilience.py tests/test_staged_tasks.py`

- [ ] **Step 6: Commit**

```bash
git add src/video_review/tasks.py tests/test_task_resilience.py tests/test_staged_tasks.py
git commit -m "fix: recover model stages without manual placeholders"
```

### Task 4: Stop worker exceptions from going directly to dead letter

**Files:**
- Modify: `src/video_review/worker.py`
- Test: `tests/test_worker_stage_dispatch.py`

**Interfaces:**
- Consumes: Redis pending claims and stage retry scheduler.
- Produces: ack only after successful processing or durable rescheduling; leaves the original pending if Redis rescheduling fails.

- [ ] **Step 1: Write failing tests**

Verify a model worker exception is durably rescheduled and acknowledged, while retry-scheduler failure leaves the source message unacknowledged and never dead-letters it immediately.

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest -q tests/test_worker_stage_dispatch.py`

- [ ] **Step 3: Implement minimal recovery path**

Replace unconditional `dead_letter_review` with stage-aware retry. Preserve pending delivery when Redis cannot create the retry entry.

- [ ] **Step 4: Run tests and verify GREEN**

Run: `uv run pytest -q tests/test_worker_stage_dispatch.py tests/test_worker_concurrency.py`

- [ ] **Step 5: Commit**

```bash
git add src/video_review/worker.py tests/test_worker_stage_dispatch.py
git commit -m "fix: requeue worker failures before dead letter"
```

### Task 5: Configuration, regression and deployment

**Files:**
- Modify: `.env.example`
- Modify: `.env`
- Modify: `README.md`
- Test: focused and full local suites

**Interfaces:**
- Configures: 30-minute deadline, model retry ZSET, delays, and promotion batch size.

- [ ] **Step 1: Add explicit production settings**

```dotenv
REDIS_MODEL_RETRY_KEY=sn2s:video_review:model_retry
VIDEO_REVIEW_WORKFLOW_DEADLINE_SECONDS=1800
VIDEO_REVIEW_MODEL_TASK_RETRY_DELAYS_SECONDS=5,15,30,60,120,180,300
VIDEO_REVIEW_MODEL_RETRY_PROMOTE_COUNT=100
```

- [ ] **Step 2: Run focused regression**

Run: `uv run pytest -q tests/test_model_retry.py tests/test_model_qpm_limiter.py tests/test_api_key_pool.py tests/test_stage_queue.py tests/test_workflow_retry.py tests/test_task_resilience.py tests/test_staged_tasks.py tests/test_worker_stage_dispatch.py`

- [ ] **Step 3: Run full suite and separate pre-existing environmental failures**

Run: `uv run pytest -q`

- [ ] **Step 4: Push, deploy and restart services**

Push the reviewed branch, fast-forward production, run `uv sync`, restart API/preprocess/model workers, and verify health and Redis retry queue keys without printing secrets.

- [ ] **Step 5: Run fault injection and smoke tests**

Inject 429, parse, circuit and Redis scheduling failures in tests; submit a small real video and verify callback/result completion. Do not claim the rolling-10,000 SLO until a 10,000-task validation run is complete.

## Self-Review

- Spec coverage: all five reported gaps map to Tasks 1-4; deployment and verification map to Task 5.
- Placeholder scan: no TBD/TODO steps.
- Type consistency: retry planning uses `CreateReviewRequest`, queue stages use existing `ReviewQueueStage`, and all queue helpers return the same nullable Redis entry convention as current download retries.
