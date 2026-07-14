# 模型 API QPS 阶梯压测报告

测试对象：`gemini-3.1-pro-preview` via `https://jzapi.duanju.com`

口径：只压测模型 API，不经过视频下载、抽帧、OCR、数据库、回调。

## 10 秒探针

| QPS | 请求数 | OK | Error | 成功率 | 错误 | P50 | P95 | P99 |
| ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: |
| 10 | 100 | 100 | 0 | 100.00% | - | 3.60s | 4.79s | 5.13s |
| 20 | 200 | 196 | 4 | 98.00% | JSONDecodeError:3, TimeoutError:1 | 3.89s | 5.22s | 5.76s |
| 40 | 400 | 392 | 8 | 98.00% | TimeoutError:2, JSONDecodeError:6 | 4.25s | 19.54s | 21.84s |
| 60 | 600 | 598 | 2 | 99.67% | TimeoutError:1, JSONDecodeError:1 | 3.93s | 5.13s | 5.49s |
| 80 | 800 | 752 | 48 | 94.00% | JSONDecodeError:43, TimeoutError:5 | 4.80s | 17.36s | 19.20s |
| 100 | 1000 | 986 | 14 | 98.60% | JSONDecodeError:12, TimeoutError:2 | 4.40s | 13.44s | 16.80s |

## 60 秒稳态

只对 10 秒探针成功率 >= 99% 的档位补跑。

| QPS | 请求数 | OK | Error | 成功率 | 错误 | P50 | P95 | P99 |
| ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: |
| 10 | 600 | 568 | 32 | 94.67% | JSONDecodeError:29, TimeoutError:3 | 4.54s | 10.73s | 28.68s |
| 60 | 3600 | 3569 | 31 | 99.14% | JSONDecodeError:24, TimeoutError:7 | 4.08s | 6.97s | 9.51s |

## 结论

- 10 秒探针里，`60 QPS` 成功率最高且 >= 99%，`80/100 QPS` 没有 429/504，但 JSONDecodeError/Timeout 增多。
- 60 秒稳态里，`60 QPS` 成功率为 `99.14%`，但仍有 `24` 个 JSONDecodeError 和 `7` 个 Timeout。
- `10 QPS` 的 60 秒稳态反而只有 `94.67%`，说明结构化输出错误具有波动性，不能只看短探针。
- 当前可以认为网关可承压到至少 `100 QPS` 短探针不出 429/504，但业务可用稳定档不能只按网关成功率定义；如果要求结构化 JSON 成功率 >= 99%，本轮最好是 `60 QPS` 稳态。

## 原始报告

- `model-api-qps-ladder-20260710T010554Z-probe-10qps-10s.json`
- `model-api-qps-ladder-20260710T010554Z-probe-20qps-10s.json`
- `model-api-qps-ladder-20260710T010554Z-probe-40qps-10s.json`
- `model-api-qps-ladder-20260710T010554Z-probe-60qps-10s.json`
- `model-api-qps-ladder-20260710T010554Z-probe-80qps-10s.json`
- `model-api-qps-ladder-20260710T010554Z-probe-100qps-10s.json`
- `model-api-qps-ladder-20260710T010554Z-steady-10qps-60s.json`
- `model-api-qps-ladder-20260710T010554Z-steady-60qps-60s.json`
