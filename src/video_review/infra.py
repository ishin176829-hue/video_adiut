from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from .config import settings
from .db import close_pool, db_last_error, init_schema
from .queue import close_redis, init_review_queue, redis_last_error


async def init_infra(*, strict: bool = False) -> dict[str, Any]:
    results: dict[str, Any] = {
        "postgres": {"configured": bool(settings.database_url), "ok": False, "error": None},
        "redis": {"configured": bool(settings.redis_url), "ok": False, "error": None},
    }
    if settings.database_url:
        try:
            results["postgres"]["ok"] = await init_schema(strict=strict)
            results["postgres"]["error"] = db_last_error()
        except Exception as exc:
            results["postgres"]["error"] = str(exc) or exc.__class__.__name__
            if strict:
                results["postgres"]["ok"] = False
    if settings.redis_url:
        try:
            results["redis"]["ok"] = await init_review_queue(strict=strict)
            results["redis"]["error"] = redis_last_error()
        except Exception as exc:
            results["redis"]["error"] = str(exc) or exc.__class__.__name__
            if strict:
                results["redis"]["ok"] = False
    return results


def _has_failed_required_backend(results: dict[str, Any]) -> bool:
    for item in results.values():
        if item["configured"] and not item["ok"]:
            return True
    return False


async def _run(args) -> int:
    try:
        strict = not args.best_effort
        results = await init_infra(strict=strict)
        print(json.dumps(results, ensure_ascii=False, indent=2))
        if _has_failed_required_backend(results) and strict:
            return 1
        return 0
    finally:
        await close_pool()
        await close_redis()


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize or check video review infra.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    init_parser = sub.add_parser("init", help="Create PostgreSQL tables and Redis stream group.")
    init_parser.add_argument("--best-effort", action="store_true", help="Do not fail if an infra backend is unreachable.")
    check_parser = sub.add_parser("check", help="Check PostgreSQL and Redis connectivity.")
    check_parser.add_argument("--best-effort", action="store_true", help="Do not fail if an infra backend is unreachable.")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
