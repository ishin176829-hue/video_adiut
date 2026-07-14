#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types

from video_review.config import settings
from video_review.model_retry import call_model_with_retry


ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "docs" / "load-test-runs"


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    rank = (len(ordered) - 1) * p
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[int(rank)]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


def sliding_peak(timestamps: list[float], window_seconds: float) -> int:
    ordered = sorted(timestamps)
    left = 0
    peak = 0
    for right, ts in enumerate(ordered):
        while ordered[left] < ts - window_seconds:
            left += 1
        peak = max(peak, right - left + 1)
    return peak


def classify_error(exc: BaseException) -> str:
    text = str(exc).lower()
    if "429" in text or "resource_exhausted" in text or "rate limit" in text:
        return "429"
    if "504" in text or "gateway time-out" in text or "gateway timeout" in text:
        return "504"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    return exc.__class__.__name__


async def call_once(
    client: genai.Client,
    model: str,
    index: int,
    timeout_seconds: float,
    *,
    retry_attempts: int = 1,
    retry_backoff_seconds: float = 0.5,
) -> dict[str, Any]:
    started_at = time.monotonic()
    retry_counts: Counter[str] = Counter()
    try:
        async def operation() -> None:
            response = await client.aio.models.generate_content(
                model=model,
                contents=[
                    (
                        "Return compact JSON only, no markdown. "
                        f"Schema: {{\"ok\": true, \"index\": {index}, \"label\": \"pass\"}}"
                    )
                ],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0,
                ),
            )
            text = response.text or ""
            json.loads(text)

        def on_retry(event: dict[str, Any]) -> None:
            retry_counts[str(event["error_kind"])] += 1

        await call_model_with_retry(
            operation,
            label=f"load-test-{index}",
            timeout_seconds=timeout_seconds,
            parse_attempts=retry_attempts,
            transient_attempts=retry_attempts,
            rate_limit_attempts=retry_attempts,
            base_backoff_seconds=retry_backoff_seconds,
            rate_limit_backoff_seconds=retry_backoff_seconds,
            jitter_seconds=0,
            on_retry=on_retry,
        )
        elapsed = time.monotonic() - started_at
        return {
            "status": "ok",
            "attempts": sum(retry_counts.values()) + 1,
            "retry_counts": dict(retry_counts),
            "started_at": started_at,
            "finished_at": time.monotonic(),
            "latency_seconds": elapsed,
        }
    except Exception as exc:
        return {
            "status": "error",
            "error": classify_error(exc),
            "error_text": str(exc)[:500],
            "attempts": sum(retry_counts.values()) + 1,
            "retry_counts": dict(retry_counts),
            "started_at": started_at,
            "finished_at": time.monotonic(),
            "latency_seconds": time.monotonic() - started_at,
        }


async def run_load(args: argparse.Namespace) -> dict[str, Any]:
    if not args.yes_real_cost:
        raise SystemExit("Refusing real model API load test without --yes-real-cost.")
    http_options: dict[str, Any] | None = None
    if settings.google_api_base_url:
        http_options = {"base_url": settings.google_api_base_url}
    client = genai.Client(api_key=settings.google_api_key, http_options=http_options)

    total = int(args.qps * args.duration_seconds)
    interval = 1 / args.qps
    started_wall = time.monotonic()
    tasks = []
    for index in range(total):
        target = started_wall + index * interval
        sleep_seconds = target - time.monotonic()
        if sleep_seconds > 0:
            await asyncio.sleep(sleep_seconds)
        tasks.append(
            asyncio.create_task(
                call_once(
                    client,
                    args.model,
                    index + 1,
                    args.request_timeout_seconds,
                    retry_attempts=args.retry_attempts,
                    retry_backoff_seconds=args.retry_backoff_seconds,
                )
            )
        )

    results = await asyncio.gather(*tasks)
    finished_wall = time.monotonic()
    statuses = Counter(result["status"] for result in results)
    errors = Counter(result.get("error") for result in results if result["status"] == "error")
    retry_counts = Counter()
    for result in results:
        retry_counts.update(result.get("retry_counts") or {})
    latencies = [float(result["latency_seconds"]) for result in results]
    ok_latencies = [float(result["latency_seconds"]) for result in results if result["status"] == "ok"]
    started_timestamps = [float(result["started_at"]) for result in results]
    finished_timestamps = [float(result["finished_at"]) for result in results]
    elapsed = finished_wall - started_wall

    payload = {
        "run_id": args.run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": settings.google_api_base_url,
        "model": args.model,
        "target_qps": args.qps,
        "duration_seconds": args.duration_seconds,
        "request_timeout_seconds": args.request_timeout_seconds,
        "retry_attempts": args.retry_attempts,
        "retry_backoff_seconds": args.retry_backoff_seconds,
        "total": total,
        "elapsed_seconds": elapsed,
        "actual_started_qps": total / max(args.duration_seconds, 1),
        "actual_finished_qps_over_wall": total / elapsed if elapsed else None,
        "sliding_1s_started_peak": sliding_peak(started_timestamps, 1),
        "sliding_1s_finished_peak": sliding_peak(finished_timestamps, 1),
        "sliding_60s_started_peak": sliding_peak(started_timestamps, 60),
        "sliding_60s_finished_peak": sliding_peak(finished_timestamps, 60),
        "status_counts": dict(statuses),
        "error_counts": {str(key): value for key, value in errors.items() if key},
        "retry_counts": dict(retry_counts),
        "total_model_attempts": sum(int(result.get("attempts") or 1) for result in results),
        "success_ratio": statuses.get("ok", 0) / total if total else 0,
        "ok_latency_seconds": {
            "avg": statistics.mean(ok_latencies) if ok_latencies else None,
            "p50": percentile(ok_latencies, 0.5),
            "p90": percentile(ok_latencies, 0.9),
            "p95": percentile(ok_latencies, 0.95),
            "p99": percentile(ok_latencies, 0.99),
        },
        "all_latency_seconds": {
            "avg": statistics.mean(latencies) if latencies else None,
            "p50": percentile(latencies, 0.5),
            "p90": percentile(latencies, 0.9),
            "p95": percentile(latencies, 0.95),
            "p99": percentile(latencies, 0.99),
        },
        "sample_errors": [
            {
                "error": result.get("error"),
                "error_text": result.get("error_text"),
                "latency_seconds": result.get("latency_seconds"),
            }
            for result in results
            if result["status"] == "error"
        ][:10],
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = REPORT_DIR / f"{args.run_id}.json"
    md_path = REPORT_DIR / f"{args.run_id}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text("# " + args.run_id + "\n\n```json\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\n```\n", encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Direct model API QPS load test.")
    parser.add_argument("--qps", type=float, required=True)
    parser.add_argument("--duration-seconds", type=float, required=True)
    parser.add_argument("--request-timeout-seconds", type=float, default=60)
    parser.add_argument("--retry-attempts", type=int, default=1)
    parser.add_argument("--retry-backoff-seconds", type=float, default=0.5)
    parser.add_argument("--model", default=settings.video_review_model)
    parser.add_argument("--run-id", default=f"model-api-qps-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
    parser.add_argument("--yes-real-cost", action="store_true")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run_load(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
