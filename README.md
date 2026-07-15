# SN2S Video Review

独立视频审核服务，覆盖“历史拒审数据导入 -> 规则库 -> MP4 下载缓存 -> ffprobe/抽帧 -> 多模态审核 -> 结构化裁决 -> 报告查询”的完整链路。

默认使用 `frames` 模式：服务先抽帧，再把抽出的图片按批次作为 inline image 传给多模态 API；不依赖 Gemini Files API。

## 快速启动

```bash
cd /data/home/huangyimin/sn2s-video-review
uv sync
cp .env.example .env  # 如果 .env 已存在，不要覆盖
uv run uvicorn video_review.main:app --host 0.0.0.0 --port 8767
```

## PostgreSQL + Redis 队列

生产模式建议打开 Redis 队列：

```bash
VIDEO_REVIEW_USE_REDIS_QUEUE=1
DATABASE_URL=postgresql://story_audit_user:change-me@postgres.example.com:5432/story_audit
REDIS_URL=redis://:change-me@redis.example.com:6379/0
```

初始化表和 Redis Stream consumer group：

```bash
cd /data/home/huangyimin/sn2s-video-review
scripts/init_infra.sh
```

如果当前服务器暂时不能连通 RDS，也可以在能访问数据库的机器上直接执行：

```bash
psql "$DATABASE_URL" -f migrations/001_video_review_infra.sql
```

启动 API：

```bash
WEB_CONCURRENCY=4 PORT=8767 scripts/run_api_nohup.sh
```

启动 worker。多个 worker 可以用不同 `WORKER_ID` 横向扩展：

```bash
WORKER_ID=1 WORKER_CONCURRENCY=5 WORKER_POLL_COUNT=5 scripts/run_worker_nohup.sh
WORKER_ID=2 WORKER_CONCURRENCY=5 WORKER_POLL_COUNT=5 scripts/run_worker_nohup.sh
```

50 人同时上传 50 个视频的建议配置：

- API：`WEB_CONCURRENCY=4`，负责接收上传并快速创建任务。
- 上传上限：`VIDEO_REVIEW_UPLOAD_MAX_FILES=50`。
- 队列：Redis Stream 承接任务，上传成功后立即返回 `review_id`。
- Worker：启动 10 个 worker，每个 `WORKER_CONCURRENCY=5`，总审核槽位 50。
- 模型限额：如果 Gemini/代理侧 QPS 不足，把 worker 数或 `WORKER_CONCURRENCY` 降低，避免触发限流。

Redis Stream：

- 主队列：`sn2s:video_review:jobs`
- 预处理队列：`sn2s:video_review:preprocess`
- 模型队列：`sn2s:video_review:model`
- 模型延迟重试队列：`sn2s:video_review:model_retry`
- 失败队列：`sn2s:video_review:jobs:dead`
- Consumer group：`video-review-workers`

模型工作流默认以提交时间为起点设置 30 分钟截止时间。429、502/503/504、连接超时、模型熔断和结构化输出错误在调用级重试耗尽后进入 Redis 延迟重试队列；只有超过截止时间或明确不可恢复的错误才进入最终失败。技术错误不会生成“人工复核”占位审核结果。

```bash
VIDEO_REVIEW_PIPELINE_MODE=staged
VIDEO_REVIEW_WORKFLOW_DEADLINE_SECONDS=1800
VIDEO_REVIEW_MODEL_TASK_RETRY_DELAYS_SECONDS=5,15,30,60,120,180,300
VIDEO_REVIEW_MODEL_RETRY_PROMOTE_COUNT=100
```

PostgreSQL 表：

- `review_jobs`：任务主状态、请求参数、进度和错误
- `review_events`：SSE 事件流落库
- `review_segments`：分段审核结果，带时间戳
- `review_findings`：风险命中明细，带时间戳和风险等级
- `review_reports`：最终报告 JSON
- `video_assets`：视频本地路径、sha256 和 ffprobe 元信息
- `frame_batch_cache_index`：Redis 帧批次缓存索引

## OSS + STS 直传

本地文件上传默认优先走 OSS 直传：前端向后端申请 STS 临时凭证，浏览器直接 multipart 上传到 OSS，上传完成后后端用 HeadObject 校验对象，再把 `oss_bucket/oss_key` 写入审核任务并投递 Redis。

必需配置：

```bash
ALIYUN_STS_ROLE_ARN=acs:ram::xxx:role/xxx
ALIYUN_OSS_BUCKET=hd-audit-oss
ALIYUN_OSS_REGION=oss-cn-hangzhou
ALIYUN_OSS_ENDPOINT=https://oss-cn-hangzhou.aliyuncs.com
ALIYUN_OSS_PUBLIC_HOST=hd-audit-oss.oss-cn-hangzhou.aliyuncs.com
ALIYUN_OSS_PREFIX=sn2s-video-audit/prod
```

后端签发 STS 时优先使用阿里云默认凭证链，例如服务器绑定的 ECS/RAM Role；没有云上角色时，才通过安全密钥系统注入 `ALIYUN_ACCESS_KEY_ID` / `ALIYUN_ACCESS_KEY_SECRET`。长期密钥不要写进前端、代码仓库或镜像。

OSS Bucket CORS 至少允许：

```text
Allowed Origin: https://video-audit.duanju.com
Allowed Method: PUT, POST, HEAD, OPTIONS
Allowed Header: *
Expose Header: ETag, x-oss-request-id, x-oss-hash-crc64ecma
```

RAM Role 的 STS 策略由后端按单个上传对象动态下发，只允许当前对象前缀的 `PutObject` 和 multipart 上传相关操作。不要给前端长期 AccessKey/Secret，不要把 bucket 设置为公开读写。worker 建议和 OSS 杭州 bucket 同地域部署，并配置 `ALIYUN_OSS_INTERNAL_ENDPOINT` 走内网。

新增 OSS 字段迁移：

```bash
psql "$DATABASE_URL" -f migrations/004_oss_uploads.sql
```

重启：

```bash
scripts/run_api_nohup.sh
scripts/run_worker_nohup.sh
```

## API

- `GET /health`
- `GET /video/reviews/policies/current`
- `POST /video/oss/uploads/init`
- `POST /video/oss/uploads/complete`
- `POST /video/reviews`
- `GET /video/reviews/{review_id}`
- `GET /video/reviews/{review_id}/stream`
- `GET /video/reviews/{review_id}/report`
- `POST /video/reviews/{review_id}/cancel`
- `POST /video/reviews/dataset/import`

## 示例

```bash
curl -X POST http://127.0.0.1:8767/video/reviews \
  -H 'Content-Type: application/json' \
  -d '{"video_url":"https://qiniu.duanju.com/ORIGIN/origin_1780972559338_334.mp4","video_title":"审核测试"}'
```

## 目录

```text
data/
  raw/        # 下载后的 MP4
  derived/    # ffprobe、抽帧、分段计划
  reports/    # VideoReviewReport
  jobs/       # ReviewJob 状态
  events/     # SSE 事件日志
  datasets/   # 历史拒审表导入结果
```
