# 内容风控视频机审兼容接口文档

版本：v1  
服务域名：`https://video-audit.duanju.com`
接口前缀：`/api/compat/content-risk/video`

这组接口用于让交片系统从原机审服务切换到 SN2S 视频审核链路，同时尽量保留“提交异步任务 -> 等完成通知 -> 拉取结果”的接入风格。

## 1. 接口列表

| 能力 | 方法 | 路径 |
| --- | --- | --- |
| 提交视频机审任务 | `POST` | `/api/compat/content-risk/video/tasks` |
| 查询视频机审结果 | `GET` | `/api/compat/content-risk/video/results?data_id=xxx` |
| 批量查询视频机审结果 | `POST` | `/api/compat/content-risk/video/results/batch` |

内部仍复用现有 `/api/v1/reviews`、Redis 队列、Worker、OSS、PostgreSQL 和审核报告生成链路。

## 2. 鉴权

兼容接口复用现有平台鉴权：

| Header | 说明 |
| --- | --- |
| `X-App-Id` | 调用方应用 ID；参与 `data_id -> review_id` 幂等映射 |
| `X-Timestamp` | 开启 HMAC 鉴权时必填 |
| `X-Nonce` | 开启 HMAC 鉴权时必填 |
| `X-Signature` | 开启 HMAC 鉴权时必填 |
| `X-Feishu-User-Id` | 可选；用于用户数据隔离 |
| `X-Feishu-User-Name` | 可选；用于上传人展示 |

生产环境建议开启：

```bash
VIDEO_REVIEW_API_AUTH_ENABLED=1
VIDEO_REVIEW_API_SECRETS=delivery-system:your-secret
```

## 3. 提交任务

```http
POST /api/compat/content-risk/video/tasks
Content-Type: application/json
```

### 3.1 推荐请求

```json
{
  "data_id": "episode_001",
  "parameters": {
    "video_url": "https://qiniu.duanju.com/ORIGIN/origin_1780972559338_334.mp4",
    "title": "短剧-第1集",
    "interval": 1,
    "callback_url": "https://delivery.example.com/audit/callback",
    "callback_secret": "callback-secret"
  }
}
```

### 3.2 兼容别名

为了降低切换成本，服务端同时兼容以下命名：

| 规范字段 | 兼容别名 | 内部字段 |
| --- | --- | --- |
| `data_id` | `DataId`、`dataID`、`DataID` | `platform_task_id` |
| `parameters` | `Parameters` | 参数对象 |
| `parameters.video_url` | `VideoUrl`、`videoUrl`、`source_url`、`SourceUrl` | `video_url` |
| `parameters.title` | `Title`、`video_title`、`VideoTitle` | `video_title` |
| `parameters.interval` | `Interval`、`fps`、`FPS` | `fps` |
| `parameters.callback_url` | `CallbackUrl`、`callbackUrl` | `callback_url` |
| `parameters.callback_secret` | `CallbackSecret`、`callbackSecret` | `callback_secret` |
| `parameters.oss_bucket` | `OssBucket`、`ossBucket` | `oss_bucket` |
| `parameters.oss_key` | `OssKey`、`ossKey` | `oss_key` |

`parameters` 可以是 JSON object，也可以是 JSON object 字符串：

```json
{
  "DataId": "episode_001",
  "Parameters": "{\"VideoUrl\":\"https://qiniu.duanju.com/a.mp4\",\"Title\":\"第1集\"}"
}
```

### 3.3 响应

```json
{
  "code": 0,
  "message": "success",
  "data_id": "episode_001",
  "task_id": "episode_001",
  "review_id": "review_xxx",
  "status": "submitted",
  "idempotent": false
}
```

说明：

- 对外交片系统继续使用 `data_id` / `task_id`。
- `review_id` 是 SN2S 内部任务 ID，排障时可用；业务侧不需要强依赖。
- 同一个 `X-App-Id + data_id` 重复提交会返回同一个 `review_id`，`idempotent=true`。

## 4. 查询结果

```http
GET /api/compat/content-risk/video/results?data_id=episode_001
```

也兼容：

```http
GET /api/compat/content-risk/video/results?DataId=episode_001
```

### 4.1 处理中响应

未完成时返回 `200`，而不是 `409`，避免打断原轮询逻辑。

```json
{
  "code": 0,
  "message": "success",
  "data_id": "episode_001",
  "task_id": "episode_001",
  "status": "processing",
  "final_label": "",
  "decision_label": "",
  "video_results": {
    "decision": "",
    "frames": []
  },
  "audio_results": {
    "decision": "",
    "details": []
  },
  "annotations": []
}
```

### 4.2 完成响应

```json
{
  "code": 0,
  "message": "success",
  "data_id": "episode_001",
  "task_id": "episode_001",
  "status": "completed",
  "final_label": "BLOCK",
  "decision_label": "BLOCK",
  "summary": "命中风险",
  "risk_score": 92,
  "video_results": {
    "decision": "BLOCK",
    "frames": [
      {
        "time": 12.0,
        "end_time": 13.0,
        "label": "violence_harm",
        "sub_label": "暴力威胁",
        "decision": "BLOCK",
        "decision_detail": "字幕出现威胁性台词；存在威胁表达",
        "risk_level": "高",
        "confidence": 0.93
      }
    ]
  },
  "audio_results": {
    "decision": "BLOCK",
    "details": [
      {
        "start_time": 12.0,
        "end_time": 13.0,
        "label": "violence_harm",
        "sub_label": "暴力威胁",
        "decision": "BLOCK",
        "decision_detail": "字幕出现威胁性台词；存在威胁表达",
        "text": "再看把你眼睛挖了",
        "confidence": 0.93
      }
    ]
  },
  "annotations": [
    "视频12秒 命中 violence_harm-暴力威胁：字幕出现威胁性台词；存在威胁表达"
  ]
}
```

### 4.3 状态映射

| SN2S 状态 | 兼容接口状态 |
| --- | --- |
| `pending` | `submitted` |
| `processing` | `processing` |
| `completed` | `completed` |
| `failed` | `failed` |
| `cancelled` | `cancelled` |

### 4.4 判定映射

| SN2S 判定 | 兼容接口判定 |
| --- | --- |
| `pass` | `PASS` |
| `warn` | `REVIEW` |
| `manual_review` | `REVIEW` |
| `reject` | `BLOCK` |

单条风险明细按严重度映射：

| SN2S severity | 明细 decision |
| --- | --- |
| `critical` | `BLOCK` |
| `high` | `BLOCK` |
| `medium` | `REVIEW` |
| `low` | `REVIEW` |

## 5. 批量查询结果

```http
POST /api/compat/content-risk/video/results/batch
Content-Type: application/json
```

请求：

```json
{
  "data_ids": ["episode_001", "episode_002"]
}
```

也兼容 `DataIds`、`dataIDs`、`DataIDs`。单次最多 500 个 `data_id`；生产轮询建议按 100 到 200 条一批，避免高峰期频繁单查放大 API 和数据库压力。

响应：

```json
{
  "code": 0,
  "message": "success",
  "count": 2,
  "items": [
    {
      "code": 0,
      "message": "success",
      "data_id": "episode_001",
      "task_id": "episode_001",
      "status": "processing",
      "final_label": "",
      "decision_label": "",
      "video_results": {"decision": "", "frames": []},
      "audio_results": {"decision": "", "details": []},
      "annotations": []
    },
    {
      "code": 40404,
      "message": "任务不存在",
      "data_id": "episode_002",
      "task_id": "episode_002"
    }
  ]
}
```

## 6. 完成通知 Callback

提交任务时传 `parameters.callback_url` 后，兼容模式下 callback 只做“任务状态通知”，不携带完整审核报告。交片系统收到通知后继续调用结果接口拉取终态结果。

发送方式：`POST callback_url?data_id=xxx&status=completed&sig=xxx`

`sig` 规则：

```text
hex(HMAC-SHA256(key=callback_secret, message=data_id))
```

请求 body：

```json
{
  "data_id": "episode_001",
  "task_id": "episode_001",
  "status": "completed"
}
```

失败和取消时：

```json
{
  "data_id": "episode_001",
  "task_id": "episode_001",
  "status": "failed"
}
```

```json
{
  "data_id": "episode_001",
  "task_id": "episode_001",
  "status": "cancelled"
}
```

## 6. Gap 对比

| 能力 | 当前 `/api/v1` | 原机审接口风格 | 融合兼容接口 | Gap |
| --- | --- | --- | --- | --- |
| 提交审核 | `POST /api/v1/reviews` | SDK 异步提交，核心是 `DataId + Parameters` | `POST /api/compat/content-risk/video/tasks` | 新增外壳，内部复用 `/api/v1/reviews` |
| 任务 ID | `platform_task_id` + `review_id` | `DataId` | 对外 `data_id` / `task_id`，内部映射 `platform_task_id` | 业务侧不用感知 `review_id` |
| 视频地址 | `video_url` 或 `oss_bucket + oss_key` | `Parameters.VideoUrl` | `parameters.video_url`，兼容 `VideoUrl` | 字段别名适配 |
| 标题 | `video_title` | `Title/title` | `parameters.title`，兼容 `Title` | 字段别名适配 |
| 抽帧频率 | `fps` | `Interval/interval` | `parameters.interval -> fps` | 需要固定 `interval=1` 表示 1fps |
| 回调语义 | 回调 payload 带 `report` | 只通知完成，结果另拉 | 只通知 `data_id + status` | 已由兼容 callback 适配 |
| 回调签名 | Header `X-Signature` | URL 参数 `sig=HMAC(data_id)` | URL 参数 `sig=HMAC(data_id)` | 已新增签名模式 |
| 查询结果 | `/api/v1/reviews/{review_id}/result` | 按任务 ID 查询 | `/results?data_id=xxx` | 内部计算 `review_id` |
| 未完成结果 | `409 RESULT_NOT_READY` | 轮询，关键判定字段为空 | `200 + status=processing + 空判定字段` | 已适配原轮询风格 |
| 最终判定 | `pass/warn/manual_review/reject` | `PASS/REVIEW/BLOCK` | `PASS/REVIEW/BLOCK` | 已做转换 |
| 风险明细 | `report.findings[]` | `VideoResults.Frames[]` / `AudioResults.Details[]` | `findings -> frames/details` | 已做转换 |
| 错误结构 | `detail.error_code/message` | SDK 异常或业务错误 | HTTP 状态 + `code/message` | 首期保留 FastAPI 422 格式用于参数校验 |
| 批量 | `/api/v1/reviews/batch` | 原链路偏单条异步 | 首期不暴露 batch | 后续如需要可新增 `/tasks/batch` |

## 7. 切换建议

1. 交片系统新增一个中性 client，替换原机审 SDK client 封装。
2. 业务层仍保留 `data_id`、提交参数、callback、轮询拉结果的处理模式。
3. 首批用 5-10 条视频灰度，检查 `data_id`、callback、`annotations` 和审核日志落库。
4. 灰度通过后再把主链路切到 `https://video-audit.duanju.com/api/compat/content-risk/video/tasks`。
