# 分阶段 OSS 视频审核链路设计

## 目标

把完整视频审核链路从“一条 worker 从下载跑到模型”改成“预处理队列 + 模型队列”的第一版，使源视频下载、FFmpeg 抽帧、OCR 不再占用模型调用并发槽，并为 500 QPM 模型调度提供全局 Redis 限流能力。

## 范围

本次只改后端链路：

- 复用现有 OSS STS 上传接口和 OSS 下载能力。
- 新增 Redis 阶段队列：`preprocess`、`model`。
- 新增模型调用全局 QPM token bucket。
- 保留旧单队列模式作为默认兼容路径，方便灰度。

不在本次做：

- 不重写前端上传 UI。
- 不新建数据库表。
- 不把 callback 独立为第三个 worker 进程，先保留在模型阶段末尾。

## 数据流

1. 用户通过 OSS STS 上传本地视频。
2. 上传完成接口创建 review job。
3. 当 `VIDEO_REVIEW_PIPELINE_MODE=staged` 时，任务进入 `preprocess` stream。
4. preprocess worker 下载 OSS 或 URL、登记本地文件、ffprobe、抽帧，写入 `derived/<video_id>/asset.json`，然后把同一任务投递到 `model` stream。
5. model worker 从 `asset.json` 恢复资产，读取已抽帧结果，执行字幕审核、视觉审核、剧情主线和最终裁决。
6. 每次模型 API 调用前先获取 Redis 全局 QPM token。

## 配置

- `VIDEO_REVIEW_PIPELINE_MODE=single|staged`，默认 `single`。
- `REDIS_PREPROCESS_STREAM=sn2s:video_review:preprocess`
- `REDIS_MODEL_STREAM=sn2s:video_review:model`
- `REDIS_PREPROCESS_GROUP=video-review-preprocess-workers`
- `REDIS_MODEL_GROUP=video-review-model-workers`
- `VIDEO_REVIEW_MODEL_QPM_LIMIT=500`
- `REDIS_MODEL_QPM_KEY=sn2s:video_review:model_qpm`

## 失败处理

- preprocess 失败：任务直接 failed，并回调失败。
- model 失败：任务 failed，并回调失败。
- Redis 队列不可用：沿用旧逻辑降级为本进程执行。
- 模型 QPM token 等待超时：按模型调用异常处理，由现有审核兜底逻辑降级为人工复核或失败。

## 验收

- 单元测试覆盖队列 stage 投递、worker stage 分发、模型 QPM token bucket。
- 远端 `uv run pytest -q` 通过。
- 生产重启后 health 正常，worker 数正常，active_jobs 为 0。
