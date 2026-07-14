# 测试环境回调接口真实签名压测报告

Run ID：`callback-real-signed-20260710T081502Z`

目标：`http://testaimediainter.weilianmenggz.cn/api/callback/volc_compliance/video`

口径：使用双方约定的 callback_secret 生成真实 `sig`，POST 到测试环境真实回调地址。由于压测 data_id 不是交片系统已存在上下文，业务返回 `上下文不存在或已过期`；本报告验证签名校验、上下文查询和网关承压，不代表成功回调写库链路。

## 单条探测

- 随机 data_id：HTTP 500，`上下文不存在或已过期`。
- 最近业务 platform_task_id：HTTP 500，`上下文不存在或已过期`。
- 已不是 `签名校验失败`，说明测试环境已经配置回调密钥。

## 阶梯结果

| QPS | 请求数 | 2xx | 非2xx/异常 | 业务分类 | P50 | P95 | P99 | Max |
| ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: |
| 1 | 10 | 0 | 10 | context_missing_or_expired:10 | 24.04ms | 36.42ms | 44.18ms | 46.12ms |
| 5 | 50 | 0 | 50 | context_missing_or_expired:50 | 23.82ms | 24.93ms | 29.77ms | 31.58ms |
| 10 | 100 | 0 | 100 | context_missing_or_expired:100 | 23.71ms | 24.83ms | 27.12ms | 35.22ms |
| 20 | 200 | 0 | 200 | context_missing_or_expired:200 | 23.37ms | 24.58ms | 25.58ms | 30.8ms |
| 50 | 500 | 0 | 500 | context_missing_or_expired:500 | 23.05ms | 24.08ms | 26.11ms | 38.03ms |

## 结论

- 本轮没有出现签名失败，说明对方已经配置了我们给出的回调密钥。
- 1 到 50 QPS 下没有网络异常或超时，P95 约 24-36ms。
- 所有请求都进入业务校验并返回 `上下文不存在或已过期`，说明随机压测 `data_id` 无法验证成功回调写库链路。
- 需要对方提供一批测试环境已存在、可重复回调的 `data_id`，才能做真正成功路径压测。
