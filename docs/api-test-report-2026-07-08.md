# 视频审核 API 接口测试报告

测试时间：2026-07-08 13:38 Asia/Shanghai  
测试对象：`https://video-audit.duanju.com`  
测试范围：稳定平台接口 `/api/v1`  
测试方式：公网黑盒 HTTP 请求 + 本地/远程自动化回归测试

## 1. 结论

本轮接口测试共覆盖 15 个黑盒用例，全部通过。

| 类型 | 通过 | 失败 | 结论 |
| --- | ---: | ---: | --- |
| 公网 API 黑盒测试 | 15 | 0 | 通过 |
| 本地 pytest 回归 | 52 | 0 | 通过 |
| 远程 pytest 回归 | 52 | 0 | 通过 |

健康检查显示 PostgreSQL、Redis、Redis 队列均已配置并可用。

## 2. 测试边界

本轮没有提交真实视频进入 Gemini 推理链路，避免产生模型费用和长耗时任务。因此未覆盖：

- 真实视频从提交、排队、抽帧、模型审核到报告生成的端到端耗时。
- OSS 文件实际上传后的 `/api/v1/uploads/oss/complete`。
- 真实 callback 回调签名与平台接收。
- HMAC 签名开启后的强制鉴权链路。

本轮重点覆盖平台对接前必须稳定的接口行为：权限、历史记录、管理员查询、数据库只读、OSS STS 初始化、错误码、安全拦截。

## 3. 黑盒接口测试结果

| 编号 | 方法 | 接口 | 预期 | 实际 | 耗时 | 结果 | 备注 |
| --- | --- | --- | --- | --- | ---: | --- | --- |
| API-001 | GET | `/api/v1/health` | `200 healthy` | 200 | 257.4 ms | 通过 | `postgres=True`，`redis=True`，`queue=True` |
| API-002 | GET | `/api/v1/policies/current` | 返回审核规则 | 200 | 96.8 ms | 通过 | 规则版本 `sn2s-video-review-v0.3-taxonomy`，分类数 11 |
| API-003 | GET | `/api/v1/reviews/history` | 未登录返回 401 | 401 | 66.2 ms | 通过 | 错误码 `FEISHU_LOGIN_REQUIRED` |
| API-004 | GET | `/api/v1/reviews/history?limit=1` | 返回本人历史 | 200 | 83.7 ms | 通过 | 测试用户当前返回 0 条 |
| API-005 | GET | `/api/v1/admin/reviews/history?limit=1` | 普通用户禁止访问 | 403 | 81.0 ms | 通过 | 错误码 `ADMIN_REQUIRED` |
| API-006 | GET | `/api/v1/admin/reviews/history?limit=1` | 管理员可查询全部历史 | 200 | 226.8 ms | 通过 | 总数 1185，返回 1 条 |
| API-007 | GET | `/api/v1/admin/database` | 普通用户禁止访问 | 403 | 101.0 ms | 通过 | 错误码 `ADMIN_REQUIRED` |
| API-008 | GET | `/api/v1/admin/database` | 管理员返回表白名单 | 200 | 125.0 ms | 通过 | 返回 7 张业务表 |
| API-009 | GET | `/api/v1/admin/database/review_jobs?limit=1` | 管理员分页查询业务表 | 200 | 201.7 ms | 通过 | 总数 1185，返回 1 行，未发现明文敏感字段 |
| API-010 | GET | `/api/v1/admin/database/pg_user?limit=1` | 非白名单表禁止查询 | 400 | 102.7 ms | 通过 | 错误码 `DATABASE_TABLE_NOT_ALLOWED` |
| API-011 | GET | `/api/v1/reviews/review_not_exists` | 不存在任务返回 404 | 404 | 136.5 ms | 通过 | 错误码 `REVIEW_NOT_FOUND` |
| API-012 | GET | `/api/v1/reviews/review_not_exists/result` | 不存在结果返回 404 | 404 | 135.5 ms | 通过 | 错误码 `REVIEW_NOT_FOUND` |
| API-013 | POST | `/api/v1/reviews` | 内网/元数据 URL 被拦截 | 400 | 188.6 ms | 通过 | 错误码 `VIDEO_URL_NOT_ALLOWED` |
| API-014 | POST | `/api/v1/reviews/batch` | 批量部分失败结构正确 | 200 | 108.7 ms | 通过 | `accepted=0`，`failed=2`，错误码为 `INVALID_SOURCE` / `VIDEO_URL_NOT_ALLOWED` |
| API-015 | POST | `/api/v1/uploads/oss/init` | `.mov` 获取 OSS STS 上传凭证 | 200 | 237.9 ms | 通过 | Bucket `hd-audit-oss`，Object Key 保留 `.mov` 后缀 |

## 4. 管理员数据库接口验证

`GET /api/v1/admin/database` 当前返回的可查询表：

```text
frame_batch_cache_index
review_events
review_findings
review_jobs
review_reports
review_segments
video_assets
```

验证点：

- 普通用户访问管理员数据库接口返回 `403 ADMIN_REQUIRED`。
- 管理员只能查询白名单业务表。
- 尝试查询 `pg_user` 返回 `400 DATABASE_TABLE_NOT_ALLOWED`。
- `review_jobs` 分页查询返回结构包含 `success/table/total/limit/offset/columns/rows`。
- 抽样结果未发现 `password/api_key/access_key/security_token` 等明显敏感字段明文透出。

## 5. OSS / `.mov` 验证

请求：

```http
POST /api/v1/uploads/oss/init
```

测试文件名：`api-smoke-20260708133811.mov`  
Content-Type：`video/quicktime`  
返回结果：

- `bucket=hd-audit-oss`
- `object_key=sn2s-video-audit/prod/uploads/video_a697971cf3e247b9/original/api-smoke-20260708133811.mov`
- 返回临时凭证字段：`access_key_id/access_key_secret/security_token/expiration`

报告中未记录临时凭证明文。

## 6. 回归测试

本地执行：

```bash
cd /Users/buding/projects/sn2s-main/.codex_artifacts/sn2s-video-review-migrate
uv run pytest -q
```

结果：

```text
52 passed, 6 warnings in 2.26s
```

远程执行：

```bash
ssh work@10.0.0.10 'cd /home/work/sn2s-video-review && .venv/bin/python -m pytest -q'
```

结果：

```text
52 passed, 5 warnings in 2.28s
```

warnings 为 FastAPI `on_event` 与依赖库 deprecation warning，不影响本轮接口验证。

## 7. 风险和建议

1. 当前公网测试请求未携带 HMAC 签名也可以访问部分 `/api/v1` 接口，说明生产强鉴权尚未强制开启，正式对接前建议开启 `VIDEO_REVIEW_API_AUTH_ENABLED` 并配置平台密钥。
2. 管理员权限当前可以通过 `X-Feishu-Is-Admin: true` header 识别。该 header 必须只由可信飞书登录网关或内网网关注入，不能允许公网客户端自行伪造。
3. 本轮没有覆盖真实视频审核链路，下一轮应使用一条小体积测试视频验证 `OSS 上传 -> complete -> 入队 -> worker 审核 -> result -> callback` 全链路。
4. 管理员数据库接口已经做白名单限制和敏感字段脱敏，但仍建议在网关层额外限制管理员接口访问来源。

## 8. 后续建议测试项

- 使用 1 个小视频做真实端到端审核，记录各阶段耗时。
- 使用 5-10 个小视频做批量提交，验证 Redis 队列、worker 并发和任务状态一致性。
- 开启 HMAC 鉴权后补测签名正确、签名错误、时间戳过期、nonce 重放。
- 补测 callback 签名和审核平台回调接收。
- 补测不同飞书用户之间无法互查任务详情和结果。
