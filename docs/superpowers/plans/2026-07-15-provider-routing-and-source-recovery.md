# Provider Routing And Source Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Isolate model channels by provider contract, fail over Gemini policy blocks to Grok, and make Qiniu source downloads resumable and OSS-cacheable.

**Architecture:** Add a small provider registry/router around the existing analyzer instead of replacing the review workflow. Preserve the current Pydantic normalization layer, namespace key/channel health in Redis, and carry Gemini-family exclusions through task metadata. Extend the downloader with HTTP Range and deterministic OSS source-cache helpers.

**Tech Stack:** Python 3.12+, Pydantic 2, google-genai, httpx, Redis, oss2, pytest.

## Global Constraints

- Channel identity is `provider + base_url + contract_id`.
- JZ keys sharing one base URL are one channel, not independent providers.
- Contract errors never open the aggregate circuit.
- Gemini `PROHIBITED_CONTENT` must not be retried on any Gemini channel.
- Secrets must never be persisted or logged.
- No public API response format changes.

---

### Task 1: Provider Channel Identity And Configuration

**Files:**
- Create: `src/video_review/providers.py`
- Modify: `src/video_review/config.py`
- Modify: `src/video_review/api_key_pool.py`
- Test: `tests/test_provider_routing.py`
- Test: `tests/test_api_key_pool.py`

**Interfaces:**
- Produces `ModelChannel`, `ProviderAttempt`, `ProviderRegistry`, `channel_fingerprint()`.
- Extends key leases with `channel_id`, `provider_id`, and `contract_id` while retaining `api_key` and `key_id`.

- [ ] Write failing tests proving three JZ keys share one channel ID while Google direct and xAI produce different channel IDs.
- [ ] Run `uv run pytest tests/test_provider_routing.py tests/test_api_key_pool.py -q` and confirm failures reference missing provider types/channel fields.
- [ ] Implement channel configuration from existing Google variables plus optional Google direct and xAI variables.
- [ ] Namespace Redis key-pool state by channel ID and retain `GoogleApiKeyPool` as a compatibility alias.
- [ ] Re-run the focused tests and confirm they pass.

### Task 2: Error Classification And Channel Failover

**Files:**
- Modify: `src/video_review/model_retry.py`
- Modify: `src/video_review/providers.py`
- Modify: `src/video_review/analyzer.py`
- Modify: `src/video_review/tasks.py`
- Test: `tests/test_provider_routing.py`
- Test: `tests/test_model_retry.py`
- Test: `tests/test_model_qpm_limiter.py`
- Test: `tests/test_staged_tasks.py`

**Interfaces:**
- Produces `ModelProviderBlockedError` and `ModelProviderExhaustedError`.
- `ModelProviderExhaustedError.excluded_families` is persisted as `request.metadata["model_excluded_families"]`.

- [ ] Write failing tests for `PROHIBITED_CONTENT -> provider_block`, channel-only contract cooldown, Grok failover, and metadata preservation on delayed retry.
- [ ] Verify the tests fail because provider-block routing and channel state do not exist.
- [ ] Implement response block inspection before `_response_text()` and classify core-policy blocks before HTTP status classification.
- [ ] Implement ordered provider attempts with per-operation excluded channels/families and channel health reporting.
- [ ] Ensure only the final logical model result reaches aggregate circuit telemetry.
- [ ] Persist excluded families when scheduling model-stage retries.
- [ ] Run focused routing, retry, circuit, and staged-task tests.

### Task 3: Gemini And Grok Adapters

**Files:**
- Create: `src/video_review/provider_adapters.py`
- Modify: `src/video_review/analyzer.py`
- Modify: `src/video_review/config.py`
- Modify: `.env.example`
- Test: `tests/test_provider_adapters.py`
- Test: `tests/test_provider_contract.py`

**Interfaces:**
- `GeminiProviderAdapter.generate(prompt, images, schema)` returns response JSON text.
- `GrokProviderAdapter.generate(prompt, images, schema)` returns response JSON text from OpenAI-compatible chat completions.

- [ ] Write failing tests that inspect outbound Gemini safety categories and Grok multimodal/json-schema payloads.
- [ ] Verify failures show the image safety categories and missing Grok adapter.
- [ ] Restrict Gemini safety settings to the four Developer API categories.
- [ ] Implement Grok text/image requests with base64 data URLs and `response_format=json_schema`.
- [ ] Connect analyzer frame-sheet, frame, subtitle, and narrative operations to the router; keep direct Files API video restricted to Gemini-capable channels.
- [ ] Run adapter and provider-contract tests.

### Task 4: Resumable Source Downloads

**Files:**
- Modify: `src/video_review/downloader.py`
- Modify: `src/video_review/tasks.py`
- Modify: `src/video_review/models.py` if the download error needs typed resume metadata.
- Test: `tests/test_downloader.py`
- Test: `tests/test_staged_tasks.py`

**Interfaces:**
- `SourceDownloadError.partial_path` exposes a safe path under `settings.raw_dir`.
- `download_video(..., resume_path=None)` resumes a task-owned `.part` file.

- [ ] Write failing tests for retaining partial bytes, issuing `Range`, handling `206`, restarting on `200`, and copying the partial path into delayed-retry metadata.
- [ ] Verify the current downloader deletes the partial file and the tests fail for that reason.
- [ ] Implement append-aware streaming, Content-Range validation, atomic final rename, and complete-file hashing.
- [ ] Pass safe resume metadata through `_prepare_review_asset()` and `_handle_source_download_error()`.
- [ ] Run downloader and staged-task tests.

### Task 5: OSS Source Cache

**Files:**
- Modify: `src/video_review/oss.py`
- Modify: `src/video_review/downloader.py`
- Modify: `src/video_review/config.py`
- Modify: `.env.example`
- Test: `tests/test_downloader.py`
- Test: `tests/test_oss_upload.py`

**Interfaces:**
- `source_cache_object_key(url)` returns a deterministic, non-secret OSS key.
- `upload_oss_object(bucket, object_key, local_path)` stores a completed source file.

- [ ] Write failing tests for deterministic keys, cache-hit download bypass, and non-fatal cache-upload failure.
- [ ] Verify tests fail because source-cache helpers do not exist.
- [ ] Add OSS file upload and missing-object detection helpers.
- [ ] Check cache before Qiniu and populate it after a verified complete download.
- [ ] Add configuration defaults and run focused downloader/OSS tests.

### Task 6: Verification And Deployment

**Files:**
- Modify: deployment `.env` only for non-secret channel flags already available.
- Update: `docs/superpowers/specs/2026-07-15-provider-routing-and-source-recovery-design.md` only if implementation reveals a necessary contract correction.

- [ ] Run `uv run pytest -q` locally.
- [ ] Run `uv run python -m compileall -q src` locally.
- [ ] Review `git diff --check` and secret-scan the diff.
- [ ] Commit and push the implementation.
- [ ] Pull the commit on `work@172.16.33.91:/home/work/sn2s-video-review`.
- [ ] Run focused tests on the server.
- [ ] Restart API and worker services using the repository's existing service commands.
- [ ] Verify health, Redis queue connectivity, and provider inventory without printing keys.
- [ ] Confirm Grok is reported disabled until `XAI_API_KEY` is securely injected.

