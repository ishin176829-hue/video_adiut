#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json

from video_review.db import get_pool
from video_review.queue import ReviewQueueStage, get_redis, review_stream_group


async def cancel_prefix(prefix: str) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT review_id FROM review_jobs WHERE request ->> $1 LIKE $2",
            "platform_task_id",
            f"{prefix}%",
        )
        review_ids = {row["review_id"] for row in rows}
        db_update = await conn.execute(
            """
            UPDATE review_jobs
            SET status = CASE WHEN status IN ('pending','processing') THEN 'cancelled' ELSE status END,
                phase = CASE WHEN status IN ('pending','processing') THEN 'cancelled' ELSE phase END,
                message = CASE WHEN status IN ('pending','processing') THEN '压测无效，已取消' ELSE message END,
                error = CASE WHEN status IN ('pending','processing') THEN 'LOAD_TEST_INVALIDATED' ELSE error END,
                completed_at = CASE WHEN status IN ('pending','processing') THEN COALESCE(completed_at, now()) ELSE completed_at END,
                updated_at = now()
            WHERE request ->> $1 LIKE $2
              AND status IN ('pending','processing')
            """,
            "platform_task_id",
            f"{prefix}%",
        )

    client = await get_redis(strict=True)
    redis_report = []
    for stage in [ReviewQueueStage.PREPROCESS, ReviewQueueStage.MODEL]:
        stream, group = review_stream_group(stage)
        last = "-"
        scanned = matched = xack = xdel = 0
        while True:
            entries = await client.xrange(stream, min=last, max="+", count=500)
            if not entries:
                break
            for stream_id, fields in entries:
                if stream_id == last:
                    continue
                scanned += 1
                try:
                    payload = json.loads(fields.get("payload") or "{}")
                except json.JSONDecodeError:
                    payload = {}
                review_id = fields.get("review_id") or payload.get("review_id")
                platform_task_id = payload.get("platform_task_id") or payload.get("task_id")
                if review_id in review_ids or (platform_task_id and str(platform_task_id).startswith(prefix)):
                    matched += 1
                    try:
                        xack += int(await client.xack(stream, group, stream_id) or 0)
                    except Exception:
                        pass
                    xdel += int(await client.xdel(stream, stream_id) or 0)
                last = stream_id
            if len(entries) < 500:
                break
        redis_report.append(
            {
                "stage": stage.value,
                "stream": stream,
                "scanned": scanned,
                "matched": matched,
                "xack": xack,
                "xdel": xdel,
            }
        )
    return {
        "prefix": prefix,
        "matched_jobs": len(review_ids),
        "db_update": db_update,
        "redis": redis_report,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Cancel and remove Redis staged messages for a load-test prefix.")
    parser.add_argument("prefix")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(cancel_prefix(args.prefix)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
