from __future__ import annotations

import argparse
import asyncio
import json

from .cleanup import cleanup_storage
from .models import CreateReviewRequest
from .store import store
from .tasks import create_job, persist_created_job, run_review


async def review_url(args) -> None:
    request = CreateReviewRequest(video_url=args.url, video_title=args.title)
    job = create_job(request)
    await persist_created_job(job, request)
    await run_review(job.review_id, request)
    report = store.get_report(job.review_id)
    print(json.dumps(report.model_dump() if report else store.get_job(job.review_id).model_dump(), ensure_ascii=False, indent=2))


def cleanup_storage_command(args) -> None:
    result = cleanup_storage(
        raw_ttl_hours=args.raw_ttl_hours,
        derived_ttl_hours=args.derived_ttl_hours,
        upload_session_ttl_hours=args.upload_session_ttl_hours,
        dry_run=not args.execute,
    )
    print(
        json.dumps(
            {
                "dry_run": result.dry_run,
                "disk_used_percent": round(result.disk_used_percent, 2),
                "watermark_action": result.watermark_action,
                "candidate_count": result.candidate_count,
                "deleted_count": result.deleted_count,
                "deleted_bytes": result.deleted_bytes,
                "items": [item.__dict__ for item in result.items],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("review-url")
    p.add_argument("url")
    p.add_argument("--title")
    cleanup = sub.add_parser("cleanup-storage")
    cleanup.add_argument("--execute", action="store_true", help="真正删除文件；不加则只输出 dry-run 候选项")
    cleanup.add_argument("--raw-ttl-hours", type=float)
    cleanup.add_argument("--derived-ttl-hours", type=float)
    cleanup.add_argument("--upload-session-ttl-hours", type=float)
    args = parser.parse_args()
    if args.cmd == "review-url":
        asyncio.run(review_url(args))
    elif args.cmd == "cleanup-storage":
        cleanup_storage_command(args)


if __name__ == "__main__":
    main()
