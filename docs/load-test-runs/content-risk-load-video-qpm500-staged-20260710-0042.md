# content-risk-load-video-qpm500-staged-20260710-0042

## Run

```json
{
  "total": 158,
  "elapsed_seconds": 1582.56297235901,
  "status_counts": {
    "cancelled": 39,
    "completed": 91,
    "failed": 28
  },
  "submit_errors": 0,
  "latency_seconds": {
    "p50": 706.9726948738098,
    "p90": 1567.9871537923814,
    "p95": 1570.3654200673104,
    "avg": 806.2981259822845
  },
  "model_error_result_counts": {
    "timeout": 24
  },
  "model_error_result_ratio": {
    "timeout": 0.1518987341772152
  },
  "aborted": false
}
```

## DB Report

```json
{
  "matched_jobs": 158,
  "status_counts": {
    "cancelled": 39,
    "completed": 91,
    "failed": 28
  },
  "review_seconds": {
    "p50": 263.91889349999997,
    "p90": 1252.3833249000004,
    "p95": 1403.5961295,
    "avg": 481.0962833860759
  },
  "total_seconds": {
    "p50": 701.0069445,
    "p90": 1563.7428824,
    "p95": 1566.12124275,
    "avg": 801.6466032531646
  },
  "model_error_job_counts": {
    "timeout": 30,
    "429": 2,
    "504": 2
  },
  "model_error_job_ratio": {
    "429": 0.012658227848101266,
    "504": 0.012658227848101266,
    "timeout": 0.189873417721519
  }
}
```
