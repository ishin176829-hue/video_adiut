# SN2S 视频审核平台对接接口文档

版本：v1.1  
更新日期：2026-07-09  
服务域名：https://video-audit.duanju.com  
稳定接口前缀：`/api/v1`  
交片系统兼容前缀：`/api/compat/content-risk`  
内部 Web 接口前缀：`/video`，仅供当前页面和后台使用，不建议审核平台直接依赖。

## 1. 对接边界

审核平台只需要接入 `/api/v1`：

| 能力 | 接口 |
| --- | --- |
| 健康检查 | `GET /api/v1/health` |
| 查询当前审核规则 | `GET /api/v1/policies/current` |
| URL/OSS 对象提交审核 | `POST /api/v1/reviews` |
| 批量提交审核 | `POST /api/v1/reviews/batch` |
| 查询本人审核历史 | `GET /api/v1/reviews/history` |
| 管理员查询审核历史 | `GET /api/v1/admin/reviews/history` |
| 管理员列出可查询数据库表 | `GET /api/v1/admin/database` |
| 管理员分页查询数据库表内容 | `GET /api/v1/admin/database/{table}` |
| 管理员回收超时 processing 任务 | `POST /api/v1/admin/reviews/reconcile-stale` |
| 查询任务状态 | `GET /api/v1/reviews/{review_id}` |
| 查询审核结果 | `GET /api/v1/reviews/{review_id}/result` |
| 取消审核任务 | `POST /api/v1/reviews/{review_id}/cancel` |
| 获取 OSS 直传临时凭证 | `POST /api/v1/uploads/oss/init` |
| OSS 上传完成并提交审核 | `POST /api/v1/uploads/oss/complete` |
| 兼容方式提交审核 | `POST /api/compat/content-risk/video/tasks` |
| 兼容方式查询单条结果 | `GET /api/compat/content-risk/video/results` |
| 兼容方式批量查询结果 | `POST /api/compat/content-risk/video/results/batch` |

推荐接入方式：

1. 本地文件：审核平台先调用 `POST /api/v1/uploads/oss/init` 获取 STS 临时凭证。
2. 浏览器或客户端直传 OSS，不经过视频审核后端中转大文件。
3. 上传完成后调用 `POST /api/v1/uploads/oss/complete`，后端校验 OSS 对象并把任务写入 Redis 审核队列。
4. 审核平台轮询 `GET /api/v1/reviews/{review_id}` 或接收 `callback_url` 回调。
5. 完成后调用 `GET /api/v1/reviews/{review_id}/result` 拉取完整结果。

## 2. 鉴权

生产环境必须打开 HMAC 鉴权：

```bash
VIDEO_REVIEW_API_AUTH_ENABLED=1
VIDEO_REVIEW_API_SECRET=默认共享密钥
# 或多平台：
VIDEO_REVIEW_API_SECRETS=platform-a:secret-a,platform-b:secret-b
```

请求头：

| Header | 必填 | 说明 |
| --- | --- | --- |
| `X-App-Id` | 是 | 调用方应用 ID |
| `X-Timestamp` | 是 | Unix 秒级时间戳，默认允许 300 秒偏移 |
| `X-Nonce` | 是 | 每次请求唯一随机串，Redis 可用时用于全局防重放 |
| `X-Signature` | 是 | HMAC-SHA256 签名，支持裸 hex 或 `sha256=<hex>` |

飞书登录信息由可信登录网关或审核平台后端注入：

| Header | 必填 | 说明 |
| --- | --- | --- |
| `X-Feishu-User-Id` | 个人历史/隔离查询必填 | 飞书用户 ID，用于数据隔离 |
| `X-Feishu-Open-Id` | 否 | 飞书 open_id |
| `X-Feishu-Union-Id` | 否 | 飞书 union_id |
| `X-Feishu-User-Name` | 否 | 上传人展示名；如包含中文，建议 URL 编码或在 body 传 `uploader_info` |
| `X-Feishu-Tenant-Key` | 否 | 飞书租户 |
| `X-Feishu-Is-Admin` | 管理员接口需要 | 可信网关注入 `true`，或使用服务端管理员白名单 |

生产管理员白名单：

```bash
VIDEO_REVIEW_API_ADMIN_FEISHU_USER_IDS=ou_xxx,ou_yyy
VIDEO_REVIEW_API_ADMIN_APP_IDS=admin-platform
```

签名原文：

```text
{timestamp}
{nonce}
{HTTP_METHOD}
{PATH}
{sha256(request_body_bytes)}
```

示例：

```python
import hashlib, hmac, json, time

body = {"platform_task_id": "task-001", "video_url": "https://qiniu.duanju.com/a.mp4"}
raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
timestamp = str(int(time.time()))
nonce = "uuid-or-random"
base = "\n".join([timestamp, nonce, "POST", "/api/v1/reviews", hashlib.sha256(raw).hexdigest()])
signature = hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest()
```

开发环境如果未开启 `VIDEO_REVIEW_API_AUTH_ENABLED`，接口仍会读取 `X-App-Id`，用于生成幂等 `review_id`。

## 3. 通用错误

`/api/v1` 业务错误统一放在 `detail` 中：

```json
{
  "detail": {
    "success": false,
    "error_code": "INVALID_SOURCE",
    "message": "video_url 和 oss_bucket/oss_key 必须二选一"
  }
}
```

常见错误码：

| HTTP | error_code | 说明 |
| --- | --- | --- |
| 400 | `INVALID_SOURCE` | 视频来源不合法或同时传了 URL 和 OSS |
| 400 | `VIDEO_URL_NOT_ALLOWED` | URL 指向内网、localhost、云元数据地址，或不在白名单 |
| 400 | `OSS_OBJECT_VERIFY_FAILED` | OSS 对象不存在或校验失败 |
| 400 | `OSS_OBJECT_SIZE_MISMATCH` | OSS 对象大小与提交值不一致 |
| 400 | `DATABASE_TABLE_NOT_ALLOWED` | 管理员数据库接口只允许查询审核业务白名单表 |
| 401 | `UNAUTHORIZED` | 缺少签名头 |
| 401 | `INVALID_SIGNATURE` | 签名不正确 |
| 401 | `TIMESTAMP_EXPIRED` | 时间戳超出允许窗口 |
| 401 | `REPLAYED_NONCE` | nonce 已使用 |
| 401 | `FEISHU_LOGIN_REQUIRED` | 缺少飞书登录用户信息 |
| 403 | `FORBIDDEN_REVIEW` | 当前飞书用户不能查看其他人的审核数据 |
| 403 | `ADMIN_REQUIRED` | 管理员接口需要管理员权限 |
| 404 | `REVIEW_NOT_FOUND` | 审核任务不存在 |
| 404 | `REPORT_NOT_FOUND` | 审核报告不存在 |
| 409 | `RESULT_NOT_READY` | 任务未完成，暂不能取结果 |
| 409 | `REVIEW_CREATE_IN_PROGRESS` | 同一 `X-App-Id + platform_task_id` 正在创建中，稍后幂等重试 |
| 503 | `API_AUTH_NOT_CONFIGURED` | 开启鉴权但未配置密钥 |

## 4. 幂等规则

平台侧必须传 `platform_task_id`。服务端使用：

```text
review_id = "review_" + sha256("{app_id}:{platform_task_id}")[:16]
```

同一个 `X-App-Id + platform_task_id` 重复提交，会返回同一个 `review_id`，并且 `idempotent=true`，不会重复入队。

不同平台如果 `platform_task_id` 相同，因为 `X-App-Id` 不同，会生成不同 `review_id`。

## 5. 提交审核

### 5.1 URL 或 OSS 对象提交

```http
POST /api/v1/reviews
Content-Type: application/json
```

请求字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `platform_task_id` | string | 是 | 平台侧任务 ID，幂等键 |
| `drama_title` | string | 否 | 剧名；状态和历史接口会返回 |
| `uploader_info` | string | 否 | 上传人展示信息；优先使用该字段，否则由飞书 header 生成 |
| `video_url` | string | URL 模式必填 | 外部可访问视频地址 |
| `oss_bucket` | string | OSS 模式必填 | OSS Bucket |
| `oss_key` | string | OSS 模式必填 | OSS Object Key |
| `oss_endpoint` | string | 否 | OSS Endpoint，默认使用服务配置 |
| `oss_etag` | string | 否 | OSS ETag |
| `oss_size` | integer | 否 | 文件大小 |
| `video_title` | string | 否 | 视频标题 |
| `fps` | integer | 否 | 抽帧 FPS，默认 1，范围 1-10 |
| `segment_seconds` | integer | 否 | 分段秒数，默认服务配置 |
| `start_seconds` | integer | 否 | 只审核某一段的开始秒 |
| `end_seconds` | integer | 否 | 只审核某一段的结束秒 |
| `callback_url` | string | 否 | 完成、失败、取消时回调 |
| `callback_secret` | string | 否 | 回调签名密钥 |
| `metadata` | object | 否 | 平台自定义元数据 |

`video_url` 与 `oss_bucket/oss_key` 必须二选一。平台接口会阻断内网 IP、localhost、链路本地地址和云元数据地址。生产可增加域名白名单：

```bash
VIDEO_REVIEW_API_VIDEO_URL_ALLOWED_HOSTS=qiniu.duanju.com,*.duanju.com
```

请求示例：

```json
{
  "platform_task_id": "audit-20260707-0001",
  "video_url": "https://qiniu.duanju.com/ORIGIN/origin_1780972559338_334.mp4",
  "video_title": "短剧-第1集",
  "drama_title": "掌心风暴",
  "uploader_info": "龚小龙",
  "fps": 1,
  "segment_seconds": 180,
  "callback_url": "https://audit-platform.example.com/callback",
  "callback_secret": "callback-secret"
}
```

响应示例：

```json
{
  "success": true,
  "review_id": "review_f03189c4db2a8d9f",
  "platform_task_id": "audit-20260707-0001",
  "status": "pending",
  "idempotent": false,
  "status_url": "/api/v1/reviews/review_f03189c4db2a8d9f",
  "result_url": "/api/v1/reviews/review_f03189c4db2a8d9f/result",
  "cancel_url": "/api/v1/reviews/review_f03189c4db2a8d9f/cancel"
}
```

### 5.2 批量提交

```http
POST /api/v1/reviews/batch
```

请求：

```json
{
  "items": [
    {
      "platform_task_id": "task-001",
      "video_url": "https://qiniu.duanju.com/a.mp4"
    },
    {
      "platform_task_id": "task-002",
      "oss_bucket": "hd-audit-oss",
      "oss_key": "sn2s-video-audit/prod/uploads/video_x/original/b.mp4"
    }
  ]
}
```

响应会部分成功，不会因为单个任务失败导致整批失败：

```json
{
  "success": false,
  "accepted_count": 1,
  "failed_count": 1,
  "items": [
    {
      "success": true,
      "platform_task_id": "task-001",
      "review_id": "review_xxx",
      "status": "pending",
      "idempotent": false,
      "status_url": "/api/v1/reviews/review_xxx",
      "result_url": "/api/v1/reviews/review_xxx/result",
      "cancel_url": "/api/v1/reviews/review_xxx/cancel"
    },
    {
      "success": false,
      "platform_task_id": "task-002",
      "error_code": "INVALID_SOURCE",
      "message": "video_url 和 oss_bucket/oss_key 必须二选一"
    }
  ]
}
```

### 5.3 查询本人审核历史

```http
GET /api/v1/reviews/history
```

必须带 `X-Feishu-User-Id`。接口只返回当前飞书用户提交或上传的视频审核记录。

查询参数：

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `created_from` | datetime | 否 | 创建时间起点，ISO8601 |
| `created_to` | datetime | 否 | 创建时间终点，ISO8601 |
| `start_time` | datetime | 否 | `created_from` 的兼容别名 |
| `end_time` | datetime | 否 | `created_to` 的兼容别名 |
| `status` | string | 否 | `pending/processing/completed/failed/cancelled` |
| `limit` | integer | 否 | 默认 50，最大 500 |
| `offset` | integer | 否 | 默认 0 |

响应：

```json
{
  "success": true,
  "total": 1,
  "limit": 50,
  "offset": 0,
  "items": [
    {
      "review_id": "review_xxx",
      "platform_task_id": "audit-20260707-0001",
      "uploader_info": "龚小龙",
      "drama_title": "掌心风暴",
      "feishu_user_id": "ou_xxx",
      "feishu_user_name": "龚小龙",
      "status": "completed",
      "phase": "done",
      "message": "审核完成",
      "video_title": "第1集",
      "decision": "reject",
      "risk_score": 92,
      "duration_seconds": 180.0,
      "duration_minutes": 3.0,
      "created_at": "2026-07-07T10:00:00+00:00",
      "completed_at": "2026-07-07T10:05:30+00:00",
      "total_seconds": 330.0,
      "status_url": "/api/v1/reviews/review_xxx",
      "result_url": "/api/v1/reviews/review_xxx/result"
    }
  ]
}
```

### 5.4 管理员查询审核历史

```http
GET /api/v1/admin/reviews/history
```

需要管理员权限。管理员来源：

- `X-Feishu-Is-Admin: true`，由可信飞书登录网关注入。
- 或 `X-Feishu-User-Id` 命中 `VIDEO_REVIEW_API_ADMIN_FEISHU_USER_IDS`。
- 或 `X-App-Id` 命中 `VIDEO_REVIEW_API_ADMIN_APP_IDS`。

查询参数同个人历史，并额外支持：

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `feishu_user_id` | string | 否 | 只看某个飞书用户；不传则查全部 |

### 5.5 管理员查询数据库内容

列出可查询的业务表：

```http
GET /api/v1/admin/database
```

响应：

```json
{
  "success": true,
  "tables": [
    "frame_batch_cache_index",
    "review_events",
    "review_findings",
    "review_jobs",
    "review_reports",
    "review_segments",
    "video_assets"
  ]
}
```

### 5.6 管理员回收超时 processing 任务

```http
POST /api/v1/admin/reviews/reconcile-stale?older_than_minutes=60&limit=500
```

用于把长时间没有更新的 `processing` 任务标记为失败，避免后台统计和任务队列长期显示“处理中”。

参数：

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `older_than_minutes` | integer | 否 | 超过多少分钟未更新才回收，默认 `VIDEO_REVIEW_STALE_PROCESSING_MINUTES`，范围 5-1440 |
| `limit` | integer | 否 | 单次最多回收数量，默认 500，范围 1-5000 |

响应：

```json
{
  "success": true,
  "reconciled_count": 51,
  "older_than_minutes": 60
}
```

当前策略只标记失败，不自动重跑。需要重审时由平台按原 `platform_task_id` 或新的任务 ID 重新提交。

分页查询某张业务表：

```http
GET /api/v1/admin/database/review_jobs?limit=100&offset=0
```

约束：

- 需要管理员权限。
- 只允许查询视频审核业务白名单表，不支持任意 SQL。
- `limit` 最大 500。
- 响应会递归脱敏 `secret/token/password/api_key/access_key/security_token` 等字段。

响应：

```json
{
  "success": true,
  "table": "review_jobs",
  "total": 1184,
  "limit": 100,
  "offset": 0,
  "columns": ["review_id", "status", "phase", "request", "created_at"],
  "rows": [
    {
      "review_id": "review_xxx",
      "status": "completed",
      "phase": "done",
      "request": {
        "video_title": "第1集",
        "callback_secret": "***REDACTED***"
      },
      "created_at": "2026-07-07T10:00:00+00:00"
    }
  ]
}
```

## 6. OSS 直传链路

### 6.1 初始化上传

```http
POST /api/v1/uploads/oss/init
```

请求：

```json
{
  "filename": "episode-01.mov",
  "size": 104857600,
  "content_type": "video/quicktime"
}
```

响应：

```json
{
  "success": true,
  "upload_id": "upload_session_xxx",
  "video_id": "video_xxx",
  "bucket": "hd-audit-oss",
  "region": "oss-cn-hangzhou",
  "endpoint": "https://oss-cn-hangzhou.aliyuncs.com",
  "object_key": "sn2s-video-audit/prod/uploads/video_xxx/original/episode-01.mov",
  "upload_started_at": "2026-07-07T10:00:00+00:00",
  "credentials": {
    "access_key_id": "STS.xxx",
    "access_key_secret": "xxx",
    "security_token": "xxx",
    "expiration": "2026-07-07T11:00:00Z"
  }
}
```

前端只拿 STS 临时凭证上传指定 `object_key`，不能接触长期 AccessKey。

### 6.2 上传完成并提交审核

```http
POST /api/v1/uploads/oss/complete
```

请求：

```json
{
  "upload_id": "upload_session_xxx",
  "platform_task_id": "audit-20260707-0002",
  "filename": "episode-01.mp4",
  "etag": "oss-etag",
  "size": 104857600,
  "video_title": "短剧-第1集",
  "drama_title": "掌心风暴",
  "uploader_info": "龚小龙",
  "fps": 1,
  "segment_seconds": 180,
  "callback_url": "https://audit-platform.example.com/callback",
  "callback_secret": "callback-secret"
}
```

服务端会：

1. 校验 `upload_id`。
2. 对 OSS 对象执行 HEAD 校验。
3. 校验大小一致。
4. 用 `X-App-Id + platform_task_id` 生成稳定 `review_id`。
5. 创建审核任务并入 Redis 队列。

响应同 `POST /api/v1/reviews`。

## 7. 状态与结果

### 7.1 查询状态

```http
GET /api/v1/reviews/{review_id}
```

响应：

```json
{
  "review_id": "review_xxx",
  "platform_task_id": "audit-20260707-0001",
  "上传人信息": "龚小龙",
  "剧名": "掌心风暴",
  "feishu_user_id": "ou_xxx",
  "feishu_user_name": "龚小龙",
  "status": "processing",
  "phase": "scan",
  "message": "正在审核第 2/6 段",
  "progress": {
    "current_segment": 2,
    "total_segments": 6,
    "percentage": 24
  },
  "error_code": null,
  "error_message": null,
  "created_at": "2026-07-07T10:00:00",
  "updated_at": "2026-07-07T10:03:00"
}
```

状态机：

| status | 说明 |
| --- | --- |
| `pending` | 已创建，等待入队或等待 Worker 消费 |
| `processing` | 审核中 |
| `completed` | 审核完成，可取结果 |
| `failed` | 审核失败 |
| `cancelled` | 已取消 |

`phase` 是更细的阶段：`pending`、`ingest`、`preprocess`、`scan`、`judge`、`done`、`error`、`cancelled`。

### 7.2 查询结果

```http
GET /api/v1/reviews/{review_id}/result
```

如果任务未完成，返回 `409 RESULT_NOT_READY`。完成后返回：

```json
{
  "success": true,
  "review_id": "review_xxx",
  "platform_task_id": "audit-20260707-0001",
  "status": "completed",
  "report": {
    "review_id": "review_xxx",
    "video_id": "video_xxx",
    "policy_version": "2026-07-xx",
    "decision": "reject",
    "risk_score": 92,
    "summary": "整片存在民族历史敏感表达和暴力威胁台词。",
    "main_plot": "开头...中间...结尾...",
    "plot_structure": [
      {
        "phase": "opening",
        "phase_name": "开头",
        "time_range": "00:00-01:30",
        "plot_summary": "交代人物冲突。",
        "value_judgement": "冲突表达偏激。",
        "risk_points": ["民族历史敏感称谓"],
        "correction_advice": "弱化羞辱性台词。"
      }
    ],
    "value_correction_advice": {
      "opening": "开头去掉歧视性称谓。",
      "main": "中段冲突改为非羞辱性表达。",
      "ending": "结尾补充正向价值观落点。",
      "overall": "整体回到合法、理性解决冲突。"
    },
    "final_verdict": {
      "passed": false,
      "conclusion": "不予过审",
      "reason": "命中高风险规则。",
      "high_risk_categories": ["national_history_ethnic"],
      "medium_risk_categories": ["violence_harm"]
    },
    "findings": [
      {
        "category": "national_history_ethnic",
        "sub_category": "民族历史敏感",
        "risk_level": "不予过审",
        "rule_tag": "民族历史",
        "severity": "critical",
        "start_time": "00:14",
        "end_time": "00:15",
        "evidence": "字幕出现敏感称谓。",
        "reason": "涉及民族历史敏感表达。",
        "suggested_action": "删除或改写对应片段。",
        "confidence": 0.92
      }
    ],
    "segments": [
      {
        "segment_index": 1,
        "start_time": "00:00",
        "end_time": "00:58",
        "summary": "该段命中敏感表达。",
        "risk_score": 92,
        "findings": []
      }
    ],
    "generated_at": "2026-07-07T10:12:00"
  }
}
```

审核结果可追溯到时间戳：

- `segments[].start_time/end_time` 表示视频片段范围。
- `findings[].start_time/end_time` 表示具体违规证据时间。
- `findings[].evidence/reason/suggested_action` 表示证据、原因和整改建议。

## 8. 回调

提交审核时传 `callback_url` 后，任务完成、失败或取消时会发送一次 HTTP POST。

生产建议配置回调域名白名单：

```bash
VIDEO_REVIEW_API_CALLBACK_ALLOWED_HOSTS=audit-platform.example.com
```

回调 payload：

```json
{
  "event": "review.completed",
  "review_id": "review_xxx",
  "platform_task_id": "audit-20260707-0001",
  "status": "completed",
  "result_url": "/api/v1/reviews/review_xxx/result",
  "error": null,
  "report": {},
  "sent_at": "2026-07-07T10:12:00+00:00"
}
```

回调请求头：

| Header | 说明 |
| --- | --- |
| `X-App-Id` | 原始提交方 app_id |
| `X-Timestamp` | Unix 秒级时间戳 |
| `X-Signature` | 如果传了 `callback_secret`，则为 `sha256=<hmac>` |

回调签名原文：

```text
{timestamp}
{sha256(callback_body_bytes)}
```

当前实现为单次回调，不自动重试。审核平台必须以 `review_id` 做幂等处理；如果回调失败，仍可通过状态/结果接口补偿查询。

## 9. 并发与限流建议

当前服务支持：

- 单批最多 50 个任务。
- Redis 队列承接并发提交。
- Worker 进程内通过 `VIDEO_REVIEW_WORKER_CONCURRENCY` 控制并发。
- 多 Worker 通过 `VIDEO_REVIEW_GLOBAL_ACTIVE_LIMIT` 和 Redis 活跃槽控制全局并发。
- 模型调用通过 `VIDEO_REVIEW_FRAME_BATCH_CONCURRENCY` 控制每个视频的帧批次并发。
- 模型 429 使用同批次指数退避，不拆分帧批次，避免限流期间放大请求数。

审核平台建议：

- 本地文件优先 OSS 直传，不走后端文件中转。
- 批量提交时保留 `platform_task_id`，失败任务按 item 级错误重试。
- 遇到 `409 RESULT_NOT_READY` 不要高频轮询，建议 3-10 秒退避。
- 遇到 `502/503/504` 或 `REVIEW_FAILED`，可以按 `platform_task_id` 幂等重试一次。
- 任务终态以状态接口或 PostgreSQL 持久化结果为准，不应只依赖单次 callback。

## 10. 交片系统兼容接口

兼容接口用于把原内容风控视频机审调用平滑切换到本服务，字段中不使用供应商名称。

### 10.1 提交任务

```http
POST /api/compat/content-risk/video/tasks
Content-Type: application/json
```

```json
{
  "data_id": "delivery-episode-0001",
  "parameters": {
    "video_url": "https://qiniu.duanju.com/ORIGIN/example.mp4",
    "title": "第1集",
    "drama_title": "掌心风暴",
    "uploader_info": "龚小龙",
    "interval": 1,
    "callback_url": "https://delivery.example.com/audit/callback",
    "callback_secret": "callback-secret"
  },
  "metadata": {}
}
```

`data_id` 是幂等键。`parameters` 可传 JSON 对象或 JSON 字符串，并兼容常见大小写字段别名。

响应：

```json
{
  "code": 0,
  "message": "success",
  "data_id": "delivery-episode-0001",
  "task_id": "delivery-episode-0001",
  "review_id": "review_xxx",
  "status": "submitted",
  "idempotent": false
}
```

### 10.2 查询结果

```http
GET /api/compat/content-risk/video/results?data_id=delivery-episode-0001
```

处理中也返回 HTTP 200，`status=processing`，结果数组为空。完成后主要字段如下：

```json
{
  "code": 0,
  "message": "success",
  "data_id": "delivery-episode-0001",
  "task_id": "delivery-episode-0001",
  "status": "completed",
  "final_label": "BLOCK",
  "decision_label": "BLOCK",
  "summary": "命中高风险审核规则",
  "risk_score": 92,
  "video_results": {
    "decision": "BLOCK",
    "frames": [
      {
        "time": 14,
        "end_time": 15,
        "label": "national_history_ethnic",
        "sub_label": "民族历史敏感",
        "decision": "BLOCK",
        "risk_level": "不予过审",
        "confidence": 0.92
      }
    ]
  },
  "audio_results": {
    "decision": "REVIEW",
    "details": []
  },
  "annotations": []
}
```

判定映射：

| SN2S decision | 兼容标签 |
| --- | --- |
| `pass` | `PASS` |
| `warn` | `REVIEW` |
| `manual_review` | `REVIEW` |
| `reject` | `BLOCK` |

### 10.3 批量查询

```http
POST /api/compat/content-risk/video/results/batch
```

```json
{
  "data_ids": ["delivery-episode-0001", "delivery-episode-0002"]
}
```

单批最多 500 个 `data_id`，响应的 `items` 与请求顺序一致，单条不存在或无权访问不会使整批失败。

更详细的兼容字段、通知回调和切换 Gap 见 [content-risk-compat-api.md](content-risk-compat-api.md)。

## 11. 部署与 Nginx

新增 `/api/v1` 不需要新增后端服务，仍由当前 FastAPI 应用提供。Nginx 如果是全路径反代到 `8767`，无需修改；如果只配置了 `/video` 或静态路径，需要补：

```nginx
location /api/v1/ {
    proxy_pass http://127.0.0.1:8767;
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;
}

location /api/compat/ {
    proxy_pass http://127.0.0.1:8767;
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;
}
```

生产必须确认：

```bash
VIDEO_REVIEW_API_AUTH_ENABLED=1
VIDEO_REVIEW_API_SECRET=...
VIDEO_REVIEW_API_IDEMPOTENCY_WAIT_SECONDS=3
VIDEO_REVIEW_API_VIDEO_URL_ALLOWED_HOSTS=qiniu.duanju.com,*.duanju.com
VIDEO_REVIEW_API_CALLBACK_ALLOWED_HOSTS=审核平台回调域名
VIDEO_REVIEW_USE_REDIS_QUEUE=1
```

不要把长期 AccessKey、平台 HMAC secret 或 callback secret 写入接口文档、前端代码或日志。

## 12. 内部接口附录

以下接口仍保留给当前 Web 页面、后台和排障使用，但不作为审核平台稳定契约：

- `GET /`
- `GET /admin`
- `GET /video/admin/stats`
- `GET /video/admin/system-metrics`
- `POST /video/reviews`
- `POST /video/reviews/bulk`
- `POST /video/reviews/upload`
- `POST /video/reviews/uploads`
- `GET /video/reviews/{review_id}`
- `GET /video/reviews/{review_id}/stream`
- `GET /video/reviews/{review_id}/report`
- `POST /video/reviews/{review_id}/cancel`
- `POST /video/oss/uploads/init`
- `POST /video/oss/uploads/complete`
- `POST /video/uploads/init`
- `POST /video/uploads/{upload_id}/chunk`
- `POST /video/uploads/{upload_id}/complete`
- `POST /video/reviews/dataset/import`
