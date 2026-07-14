from __future__ import annotations

import argparse
import asyncio
import contextlib
import time

from .config import settings
from .db import fetch_review_job_states
from .infra import init_infra
from .queue import (
    ReviewQueueStage,
    ack_review,
    claim_stale_reviews,
    dead_letter_review,
    default_consumer_name,
    dequeue_reviews,
    global_review_slot,
    promote_due_download_retries,
    renew_review_claim,
)
from .store import store
from .tasks import reconcile_stale_reviews, run_model_stage, run_preprocess_stage, run_review


async def process_message(message) -> None:
    if message.stage == ReviewQueueStage.PREPROCESS:
        store.add_event(message.review_id, "status", {"text": "preprocess worker 已领取任务"})
        await run_preprocess_stage(message.review_id, message.request)
        return
    if message.stage == ReviewQueueStage.MODEL:
        async with global_review_slot(message.review_id):
            store.add_event(message.review_id, "status", {"text": "model worker 已领取任务"})
            await run_model_stage(message.review_id, message.request)
        return
    async with global_review_slot(message.review_id):
        store.add_event(message.review_id, "status", {"text": "worker 已领取任务"})
        await run_review(message.review_id, message.request)


async def _renew_pending_claim(message, consumer_name: str, stop: asyncio.Event) -> None:
    interval = max(1, settings.redis_pending_heartbeat_seconds)
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
        except TimeoutError:
            try:
                await renew_review_claim(message, consumer_name)
            except Exception as exc:
                store.add_event(message.review_id, "status", {"text": f"pending 心跳续期失败：{exc}"})


async def _is_terminal_job(review_id: str) -> bool:
    try:
        states = await fetch_review_job_states([review_id])
    except Exception:
        return False
    state = states.get(review_id) or {}
    return state.get("status") in {"completed", "failed", "cancelled", "source_unavailable"}


async def handle_message(message, worker_semaphore: asyncio.Semaphore, consumer_name: str) -> None:
    async with worker_semaphore:
        if await _is_terminal_job(message.review_id):
            store.add_event(message.review_id, "status", {"text": "任务已是终态，确认并跳过遗留队列消息"})
            await ack_review(message)
            return
        heartbeat_stop = asyncio.Event()
        heartbeat = asyncio.create_task(_renew_pending_claim(message, consumer_name, heartbeat_stop))
        try:
            await process_message(message)
            await ack_review(message)
        except Exception as exc:
            await dead_letter_review(message, str(exc))
            store.add_event(message.review_id, "error", {"error": f"worker 执行异常：{exc}"})
        finally:
            heartbeat_stop.set()
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat


async def run_worker(
    *,
    consumer_name: str | None = None,
    once: bool = False,
    count: int | None = None,
    concurrency: int | None = None,
    stage: ReviewQueueStage | str | None = None,
) -> None:
    consumer = consumer_name or default_consumer_name()
    queue_stage = ReviewQueueStage(stage or ReviewQueueStage.SINGLE)
    read_count = max(1, count or settings.worker_poll_count)
    max_concurrency = max(1, concurrency or settings.worker_concurrency)
    worker_semaphore = asyncio.Semaphore(max_concurrency)
    running: set[asyncio.Task] = set()
    store.add_event("worker", "status", {"text": f"worker started: {consumer}"})
    last_stale_reconcile_at = 0.0
    while True:
        running = {task for task in running if not task.done()}
        infra = await init_infra(strict=False)
        if not infra["redis"]["ok"]:
            error = infra["redis"].get("error") or "Redis 未配置或不可达"
            store.add_event("worker", "status", {"text": f"等待 Redis 队列可用：{error}"})
            if once:
                raise RuntimeError(error)
            await asyncio.sleep(10)
            continue
        if queue_stage == ReviewQueueStage.PREPROCESS:
            try:
                promoted = await promote_due_download_retries()
                if promoted:
                    store.add_event("worker", "status", {"text": f"已投递 {promoted} 个延迟下载重试任务"})
            except Exception as exc:
                store.add_event("worker", "status", {"text": f"下载延迟队列投递失败：{exc}"})
        reconcile_interval = max(5, settings.stale_processing_reconcile_interval_seconds)
        if settings.stale_processing_reconcile_on_worker_start and (
            not last_stale_reconcile_at or time.monotonic() - last_stale_reconcile_at >= reconcile_interval
        ):
            last_stale_reconcile_at = time.monotonic()
            try:
                stale_count = await reconcile_stale_reviews()
            except Exception as exc:
                stale_count = 0
                store.add_event("worker", "status", {"text": f"超时任务巡检失败，稍后重试：{exc}"})
            if stale_count:
                store.add_event("worker", "status", {"text": f"已回收 {stale_count} 个长时间无更新的 processing 任务"})
        if len(running) >= max_concurrency:
            done, pending = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            running = pending
            if once and not running:
                return
            continue
        available_slots = max_concurrency - len(running)
        try:
            messages = await claim_stale_reviews(consumer, count=min(read_count, available_slots), stage=queue_stage)
        except Exception as exc:
            store.add_event("worker", "status", {"text": f"回收 Redis pending 队列失败，准备重试：{exc}"})
            if once:
                raise
            await asyncio.sleep(10)
            continue
        if not messages:
            try:
                messages = await dequeue_reviews(consumer, count=min(read_count, available_slots), stage=queue_stage)
            except Exception as exc:
                store.add_event("worker", "status", {"text": f"读取 Redis 队列失败，准备重试：{exc}"})
                messages = []
        if not messages:
            if once and not running:
                return
            if running:
                done, pending = await asyncio.wait(running, timeout=0, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
                running = pending
            continue
        for message in messages:
            running.add(asyncio.create_task(handle_message(message, worker_semaphore, consumer)))
        if once:
            if running:
                await asyncio.gather(*running)
            return


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Redis-backed video review worker.")
    parser.add_argument("--consumer", help="Redis consumer name.")
    parser.add_argument("--once", action="store_true", help="Consume one batch and exit.")
    parser.add_argument("--count", type=int, help="Max messages per Redis read.")
    parser.add_argument("--concurrency", type=int, help="Max concurrent review jobs inside this worker process.")
    parser.add_argument(
        "--stage",
        choices=[stage.value for stage in ReviewQueueStage],
        default=ReviewQueueStage.SINGLE.value,
        help="Redis queue stage to consume.",
    )
    args = parser.parse_args()
    asyncio.run(
        run_worker(
            consumer_name=args.consumer,
            once=args.once,
            count=args.count,
            concurrency=args.concurrency,
            stage=args.stage,
        )
    )


if __name__ == "__main__":
    main()
