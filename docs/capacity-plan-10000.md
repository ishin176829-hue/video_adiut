# 视频审核 10000 条/8 小时容量与退避策略

目标：4-6 小时内完成 10000 个分集视频审核。8 小时只能作为宽松兜底，不能作为排班后的真实 SLA。

换算：

| 完成窗口 | 条/小时 | 条/分钟 | 平均完成间隔 |
| ---: | ---: | ---: | ---: |
| 4 小时 | 2500 | 41.7 | 1.44 秒/条 |
| 5 小时 | 2000 | 33.3 | 1.80 秒/条 |
| 6 小时 | 1667 | 27.8 | 2.16 秒/条 |

## 1. 当前耗时口径

以 2026-07-09 查询到的后台数据为参考：

| 时间范围 | 完成数 | 视频时长 p50 | 审核耗时 p50 | 审核耗时 p90 | 总耗时 p50 | 总耗时 p90 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 全量 | 1083 | 59.88 秒 | 129.42 秒 | 254.38 秒 | 227.62 秒 | 644.23 秒 |
| 近 7 天 | 354 | 72.96 秒 | 112.92 秒 | 278.55 秒 | 173.74 秒 | 506.48 秒 |

如果按修复前实际启动状态 10 个 worker、每个并发 2，即约 20 active 并发估算：

- p50 审核耗时可承载约 4.4k 到 5.1k 条/8 小时
- p90 审核耗时可承载约 2.1k 到 2.3k 条/8 小时



## 2. 推荐并发参数

目标 active 并发估算：

```text
required_concurrency = target_qps * review_seconds / target_utilization
target_qps = 10000 / (8 * 3600) = 0.347
target_utilization = 0.7
```

| 完成窗口 | p50 112.92 秒 | p90 278.55 秒 | p95 357.35 秒 |
| ---: | ---: | ---: | ---: |
| 4 小时 | 113 | 277 | 355 |
| 5 小时 | 90 | 222 | 284 |
| 6 小时 | 75 | 185 | 237 |

推荐：

- 先测曲线：`VIDEO_REVIEW_GLOBAL_ACTIVE_LIMIT=80/120/140/180`
- 6 小时 p90 目标：至少 `VIDEO_REVIEW_GLOBAL_ACTIVE_LIMIT=185`
- 5 小时 p90 目标：至少 `VIDEO_REVIEW_GLOBAL_ACTIVE_LIMIT=222`
- 4 小时 p90 目标：至少 `VIDEO_REVIEW_GLOBAL_ACTIVE_LIMIT=277`
- 4 小时 p95 兜底：约 `VIDEO_REVIEW_GLOBAL_ACTIVE_LIMIT=355`

结论：80/120/140/180 只能用于找模型 429/504 拐点。若要按 4-6 小时完成 10000 条，180 只接近 6 小时 p90，无法覆盖 4-5 小时 p90，更无法覆盖 4 小时 p95。

前提：模型 API 额度、网关超时时间、Redis、PostgreSQL 连接池、机器 CPU 都要同步压测确认。不要只改全局阀门；当前 10 worker × 5 并发的实际 worker active 上限是 50，`VIDEO_REVIEW_GLOBAL_ACTIVE_LIMIT` 设置到 80/120/140/180 时，如果不提高 worker 总并发，压测不会真正打到这些档位。

## 3. 已落地的保护

### 3.1 Redis 全局 active 槽位

所有 Redis worker 在真正执行审核前先抢占同一个 Redis ZSET 槽位：

```bash
VIDEO_REVIEW_GLOBAL_ACTIVE_LIMIT=50
REDIS_GLOBAL_ACTIVE_KEY=sn2s:video_review:active_reviews
VIDEO_REVIEW_GLOBAL_ACTIVE_TTL_SECONDS=1800
VIDEO_REVIEW_GLOBAL_ACTIVE_WAIT_SECONDS=3600
VIDEO_REVIEW_GLOBAL_ACTIVE_POLL_SECONDS=2
```

作用：

- 防止多 worker、多机器叠加后把模型 QPS 打爆
- worker 崩溃后槽位会通过 TTL 自动释放
- 执行中的任务会定期续租，避免长视频审核时槽位提前过期

### 3.2 stale processing 回收

worker 启动时会自动标记长时间没有更新的 `processing` 任务为失败：

```bash
VIDEO_REVIEW_STALE_PROCESSING_MINUTES=60
VIDEO_REVIEW_STALE_PROCESSING_RECONCILE_ON_WORKER_START=1
```

管理员也可以手工触发：

```http
POST /api/v1/admin/reviews/reconcile-stale?older_than_minutes=60&limit=500
```

返回：

```json
{
  "success": true,
  "reconciled_count": 51,
  "older_than_minutes": 60
}
```

当前策略是标记失败，不自动重跑。自动重跑可能造成重复回调、重复扣费或重复审核，后续如果要做需要先加任务幂等锁和 callback outbox。

### 3.3 批量结果查询

交片系统应优先使用：

```http
POST /api/compat/content-risk/video/results/batch
```

单次 100 到 200 个 `data_id` 比逐条轮询更稳，可明显降低 API 请求数和数据库压力。

## 4. 退避策略

### 4.1 提交侧

- 提交任务接口只入队，不同步等待模型结果。
- 对 `429`、`503`、`504` 使用指数退避：1s、2s、4s、8s、16s，最大 30s。
- 同一个 `X-App-Id + data_id` 重复提交是幂等的，可安全重试。

### 4.2 Worker 侧

- Redis stream 负责排队。
- `WORKER_COUNT * WORKER_CONCURRENCY` 是本机理论并发。
- `VIDEO_REVIEW_GLOBAL_ACTIVE_LIMIT` 是全局硬上限，必须小于模型和机器可承载上限。
- 模型临时错误会拆小帧批次重试；仍失败时标记该时间段为人工复核，不让整条任务卡死。

### 4.3 查询侧

- 未完成结果返回 `200 + status=submitted/processing`。
- 调用方轮询间隔建议：
  - 任务提交后 0 到 2 分钟：10 秒一次
  - 2 到 10 分钟：30 秒一次
  - 10 分钟后：60 秒一次
- 批量查询优先；单查只用于人工排障。

## 5. 下一步

要真正稳定跑满 10000 条/8 小时，还需要：

- 压测 `VIDEO_REVIEW_GLOBAL_ACTIVE_LIMIT=80/120/140/180` 下的模型 429/504 比例；如果 180 仍稳定，再继续测 220/280/360
- 按模型供应商 QPS 建立自适应限流，429/504 时动态降并发
- 增加 callback outbox 表和独立 callback worker，避免业务回调失败影响审核 worker 周转
- 根据视频长度分队列：短视频队列和长视频队列分开，避免长视频阻塞短视频
- PostgreSQL 连接池按 API、worker、admin 分开配置，避免后台查询影响审核写入

## 6. 压测样本与基线

历史 PostgreSQL 已抽样：

```bash
cd /home/work/sn2s-video-review
.venv/bin/python scripts/load_test_content_risk.py sample \
  --limit 10000 \
  --min-duration-seconds 10 \
  --max-duration-seconds 600 \
  --output docs/load-test-runs/postgres-samples-full.txt
```

结果：10-600 秒范围内可用去重 HTTP 样本为 158 条。

这意味着如果要提交超过 158 条真实模型压测任务，必须允许样本重复。重复样本可以测试模型 QPS 和系统并发，但会弱化内容分布代表性；如果 Redis 帧批次缓存开启，重复样本还会让模型调用被缓存命中，导致 429/504 比例失真。正式模型压测建议临时设置：

```bash
VIDEO_REVIEW_REDIS_CACHE_ENABLED=0
VIDEO_REVIEW_FRAME_BATCH_CONCURRENCY=1
```

历史错误基线：

| 范围 | 任务数 | 429 | 504 | timeout |
| --- | ---: | ---: | ---: | ---: |
| 近 24 小时 | 29 | 1 | 0 | 0 |
| 全量 | 1214 | 29 | 21 | 71 |

已跑一次真实 smoke：

```bash
.venv/bin/python scripts/load_test_content_risk.py run \
  --sample-from-db \
  --sample-limit 3 \
  --total 3 \
  --app-id capacity-test \
  --prefix capacity-smoke-20260709152952 \
  --submit-rps 1 \
  --poll-interval 15 \
  --timeout-seconds 900 \
  --batch-size 50 \
  --with-db-report \
  --yes-real-cost
```

结果：

| 指标 | 值 |
| --- | ---: |
| 总数 | 3 |
| completed | 2 |
| failed | 1 |
| submit_errors | 0 |
| DB review_seconds p50 | 354.17 秒 |
| DB review_seconds p90 | 379.12 秒 |
| 429/504 | 0 |

失败原因：`Expecting value: line 1 column 1 (char 0)`，属于模型响应/JSON 解析异常，不是 429/504。

## 7. 矩阵压测执行方式

单机当前 32 vCPU / 30 GiB 内存。若要测 80/120/140/180，worker 总并发必须不低于对应档位。建议每档先跑 `2 * limit` 条任务看 429/504 和 CPU，再决定是否扩大到 5-10 倍。

示例：

```bash
cd /home/work/sn2s-video-review

# 80 active
VIDEO_REVIEW_GLOBAL_ACTIVE_LIMIT=80 WORKER_COUNT=16 WORKER_CONCURRENCY=5 ./scripts/restart_all.sh
.venv/bin/python scripts/load_test_content_risk.py run \
  --sample-from-db --sample-limit 160 --total 160 --allow-repeat \
  --app-id capacity-test --prefix capacity-limit-80-$(date +%Y%m%d%H%M%S) \
  --submit-rps 20 --poll-interval 20 --timeout-seconds 3600 \
  --batch-size 200 --with-db-report --yes-real-cost

# 120 active
VIDEO_REVIEW_GLOBAL_ACTIVE_LIMIT=120 WORKER_COUNT=24 WORKER_CONCURRENCY=5 ./scripts/restart_all.sh
.venv/bin/python scripts/load_test_content_risk.py run \
  --sample-from-db --sample-limit 240 --total 240 --allow-repeat \
  --app-id capacity-test --prefix capacity-limit-120-$(date +%Y%m%d%H%M%S) \
  --submit-rps 30 --poll-interval 20 --timeout-seconds 5400 \
  --batch-size 200 --with-db-report --yes-real-cost

# 140 active
VIDEO_REVIEW_GLOBAL_ACTIVE_LIMIT=140 WORKER_COUNT=28 WORKER_CONCURRENCY=5 ./scripts/restart_all.sh
.venv/bin/python scripts/load_test_content_risk.py run \
  --sample-from-db --sample-limit 280 --total 280 --allow-repeat \
  --app-id capacity-test --prefix capacity-limit-140-$(date +%Y%m%d%H%M%S) \
  --submit-rps 35 --poll-interval 20 --timeout-seconds 5400 \
  --batch-size 200 --with-db-report --yes-real-cost

# 180 active
VIDEO_REVIEW_GLOBAL_ACTIVE_LIMIT=180 WORKER_COUNT=36 WORKER_CONCURRENCY=5 ./scripts/restart_all.sh
.venv/bin/python scripts/load_test_content_risk.py run \
  --sample-from-db --sample-limit 360 --total 360 --allow-repeat \
  --app-id capacity-test --prefix capacity-limit-180-$(date +%Y%m%d%H%M%S) \
  --submit-rps 45 --poll-interval 20 --timeout-seconds 7200 \
  --batch-size 200 --with-db-report --yes-real-cost
```

判定口径：

- `429` 比例 > 1%：说明模型额度不足或并发过高，需要降全局并发或申请 QPS。
- `504/timeout` 比例 > 3%：说明批次过大、网关超时或模型排队过长，需要降低 `FRAME_BATCH_CONCURRENCY`、缩小拼图/批次或增加超时重试。
- CPU 长时间 > 85%：说明抽帧/下载/拼图已经成为瓶颈，应减少单机 worker，改多机器横向扩容。
- `failed` 中非 429/504 的 JSON 解析异常需要单独归类，不应混入模型限流指标。
