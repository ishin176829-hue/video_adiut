#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

from video_review.config import settings
from video_review.models import VideoAsset
from video_review.preprocessor import enrich_asset, extract_frames, list_segment_frames, make_segment_plan
from video_review.semantic import (
    BenchmarkResult,
    FullVideoScreenplaySemantic,
    SegmentScreenplaySemantic,
    build_screenplay_semantic_prompt,
    json_payload,
    segment_frame_batches,
)


DEFAULT_DURATIONS = [60, 90, 120, 180, 240]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_stem(path: Path) -> str:
    return path.stem.replace("/", "_")


def make_asset(video_path: Path, *, video_id: str) -> VideoAsset:
    return VideoAsset(
        video_id=video_id,
        source_url=None,
        local_path=str(video_path),
        sha256=sha256_file(video_path),
        content_length=video_path.stat().st_size,
    )


def try_enrich_asset(asset: VideoAsset) -> VideoAsset:
    try:
        return enrich_asset(asset)
    except Exception as exc:
        print(f"metadata_probe_warning={type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return asset


def extracted_frame_count(asset: VideoAsset) -> int:
    frame_dir = settings.derived_dir / asset.video_id / "frames"
    return len(sorted(frame_dir.glob("*.jpg")))


def build_client():
    http_options = {"base_url": settings.google_api_base_url} if settings.google_api_base_url else None
    return genai.Client(api_key=settings.google_api_key, http_options=http_options)


def coerce_segment_payload(
    raw_text: str,
    *,
    episode_id: str,
    title: str,
    segment_index: int,
    start_time: str,
    end_time: str,
) -> SegmentScreenplaySemantic:
    payload = json_payload(raw_text, unwrap_key="SegmentScreenplaySemantic")
    payload.setdefault("episode_id", episode_id)
    payload.setdefault("title", title)
    payload.setdefault("segment_index", segment_index)
    payload.setdefault("time_range", f"{start_time}-{end_time}")
    payload.setdefault("script_scenes", [])
    payload.setdefault("character_candidates", [])
    payload.setdefault("asset_observations", [])
    payload.setdefault("audit_signals", [])
    payload.setdefault("confidence", 0.5)
    return SegmentScreenplaySemantic.model_validate(payload)


async def analyze_frames(
    client,
    *,
    model: str,
    episode_id: str,
    title: str,
    segment_index: int,
    start_time: str,
    end_time: str,
    frame_interval_seconds: int,
    frames: list[dict],
) -> tuple[SegmentScreenplaySemantic, str]:
    prompt = build_screenplay_semantic_prompt(
        episode_id=episode_id,
        title=title,
        segment_index=segment_index,
        start_time=start_time,
        end_time=end_time,
        frame_interval_seconds=frame_interval_seconds,
    )
    parts = [types.Part.from_text(text=prompt)]
    for frame in frames:
        frame_path = Path(frame["path"])
        parts.append(types.Part.from_text(text=f"FRAME timestamp={frame['timestamp']} index={frame['frame_index']}"))
        parts.append(types.Part.from_bytes(data=frame_path.read_bytes(), mime_type="image/jpeg"))

    response = await client.aio.models.generate_content(
        model=model,
        contents=parts,
        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0),
    )
    raw_text = response.text or "{}"
    return (
        coerce_segment_payload(
            raw_text,
            episode_id=episode_id,
            title=title,
            segment_index=segment_index,
            start_time=start_time,
            end_time=end_time,
        ),
        raw_text,
    )


def parse_durations(value: str) -> list[int]:
    durations = [int(item.strip()) for item in value.split(",") if item.strip()]
    return sorted(duration for duration in durations if duration > 0)


async def run_benchmark(args: argparse.Namespace) -> None:
    video_path = Path(args.video).expanduser().resolve()
    episode_id = args.episode_id or safe_stem(video_path)
    title = args.title or safe_stem(video_path)
    durations = parse_durations(args.durations)
    out_dir = Path(args.out_dir).expanduser().resolve() / safe_stem(video_path)
    out_dir.mkdir(parents=True, exist_ok=True)

    asset = try_enrich_asset(make_asset(video_path, video_id=f"semantic_benchmark_{episode_id}"))
    max_duration = min(max(durations), int(asset.duration_seconds or max(durations)))
    extract_frames(asset, fps=args.fps, start_seconds=0, end_seconds=max_duration)
    probe_segment = make_segment_plan(max_duration, max_duration)[0]
    frames = list_segment_frames(asset, probe_segment, fps=args.fps)
    client = None if args.dry_run else build_client()
    results: list[BenchmarkResult] = []

    for index, duration in enumerate(durations, start=1):
        selected = [
            frame
            for frame in frames
            if float(frame.get("timestamp_seconds") or 0) < min(duration, max_duration)
        ]
        started = time.perf_counter()
        raw_path = out_dir / f"benchmark-{duration}s.raw.txt"
        json_path = out_dir / f"benchmark-{duration}s.json"
        if args.dry_run:
            result = BenchmarkResult(
                duration_seconds=duration,
                frames_sent=len(selected),
                success=True,
                latency_seconds=0,
                output_valid_json=False,
                output_path=None,
                raw_output_path=None,
                error="dry-run",
            )
            print(result.model_dump_json(), flush=True)
            results.append(result)
            continue

        try:
            semantic, raw_text = await analyze_frames(
                client,
                model=args.model,
                episode_id=episode_id,
                title=title,
                segment_index=index,
                start_time="00:00",
                end_time=probe_segment.end_time if duration >= max_duration else make_segment_plan(duration, duration)[0].end_time,
                frame_interval_seconds=args.fps,
                frames=selected,
            )
            raw_path.write_text(raw_text, encoding="utf-8")
            json_path.write_text(semantic.model_dump_json(indent=2), encoding="utf-8")
            result = BenchmarkResult(
                duration_seconds=duration,
                frames_sent=len(selected),
                success=True,
                latency_seconds=round(time.perf_counter() - started, 3),
                output_valid_json=True,
                output_path=str(json_path),
                raw_output_path=str(raw_path),
            )
        except Exception as exc:
            result = BenchmarkResult(
                duration_seconds=duration,
                frames_sent=len(selected),
                success=False,
                latency_seconds=round(time.perf_counter() - started, 3),
                output_valid_json=False,
                error=f"{type(exc).__name__}: {exc}",
            )
        print(result.model_dump_json(), flush=True)
        results.append(result)

    (out_dir / "benchmark-results.json").write_text(
        json.dumps([item.model_dump(mode="json") for item in results], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


async def run_extract(args: argparse.Namespace) -> None:
    video_path = Path(args.video).expanduser().resolve()
    episode_id = args.episode_id or safe_stem(video_path)
    title = args.title or safe_stem(video_path)
    out_dir = Path(args.out_dir).expanduser().resolve() / safe_stem(video_path)
    segment_dir = out_dir / "segments"
    segment_dir.mkdir(parents=True, exist_ok=True)

    asset = try_enrich_asset(make_asset(video_path, video_id=f"screenplay_semantic_{episode_id}"))
    duration = int(asset.duration_seconds or 0)
    extract_frames(asset, fps=args.fps, start_seconds=0, end_seconds=duration or None)
    if duration <= 0:
        duration = max(1, extracted_frame_count(asset))
    full_segment = make_segment_plan(duration, duration)[0]
    frames = list_segment_frames(asset, full_segment, fps=args.fps)
    batches = segment_frame_batches(frames, segment_seconds=args.segment_seconds)
    client = None if args.dry_run else build_client()
    segments: list[SegmentScreenplaySemantic] = []
    total_frames_sent = 0

    for batch in batches:
        total_frames_sent += len(batch.frames)
        if args.dry_run:
            print(
                json.dumps(
                    {
                        "segment_index": batch.segment_index,
                        "start_time": batch.start_time,
                        "end_time": batch.end_time,
                        "frames": len(batch.frames),
                        "dry_run": True,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            continue

        started = time.perf_counter()
        semantic, raw_text = await analyze_frames(
            client,
            model=args.model,
            episode_id=episode_id,
            title=title,
            segment_index=batch.segment_index,
            start_time=batch.start_time,
            end_time=batch.end_time,
            frame_interval_seconds=args.fps,
            frames=batch.frames,
        )
        raw_path = segment_dir / f"segment-{batch.segment_index:03d}.raw.txt"
        json_path = segment_dir / f"segment-{batch.segment_index:03d}.json"
        raw_path.write_text(raw_text, encoding="utf-8")
        json_path.write_text(semantic.model_dump_json(indent=2), encoding="utf-8")
        segments.append(semantic)
        print(
            json.dumps(
                {
                    "segment_index": batch.segment_index,
                    "frames": len(batch.frames),
                    "latency_seconds": round(time.perf_counter() - started, 3),
                    "scenes": len(semantic.script_scenes),
                    "characters": len(semantic.character_candidates),
                    "assets": len(semantic.asset_observations),
                    "signals": len(semantic.audit_signals),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    if args.dry_run:
        return

    full = FullVideoScreenplaySemantic(
        episode_id=episode_id,
        title=title,
        model=args.model,
        frame_interval_seconds=args.fps,
        segment_seconds=args.segment_seconds,
        total_frames_sent=total_frames_sent,
        segments=segments,
    )
    output_path = out_dir / f"{safe_stem(video_path)}.screenplay-semantic.json"
    output_path.write_text(full.model_dump_json(indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output_path), "segments": len(segments)}, ensure_ascii=False), flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Benchmark and extract screenplay-style video semantics.")
    parser.add_argument("--model", default="gemini-3.1-flash-lite")
    parser.add_argument("--fps", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)

    benchmark = subparsers.add_parser("benchmark", help="Probe single-request frame count limits.")
    benchmark.add_argument("video")
    benchmark.add_argument("--title")
    benchmark.add_argument("--episode-id")
    benchmark.add_argument("--durations", default=",".join(str(item) for item in DEFAULT_DURATIONS))
    benchmark.add_argument("--out-dir", default="data/semantic_benchmarks")

    extract = subparsers.add_parser("extract", help="Extract full screenplay-style semantics by segments.")
    extract.add_argument("video")
    extract.add_argument("--title")
    extract.add_argument("--episode-id")
    extract.add_argument("--segment-seconds", type=int, default=60)
    extract.add_argument("--out-dir", default="data/semantic_extractions")
    return parser


async def async_main() -> None:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args()
    if args.fps != 1:
        raise SystemExit("Only --fps 1 is supported for this workflow.")
    if args.command == "benchmark":
        await run_benchmark(args)
    elif args.command == "extract":
        await run_extract(args)


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
