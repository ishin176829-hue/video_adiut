from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .config import settings
from .models import SegmentPlan, VideoAsset
from .utils import format_time


def ffmpeg_executable() -> str:
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def ffprobe(path: str) -> dict:
    cmd = [
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffprobe failed")
    return json.loads(result.stdout)


def enrich_asset(asset: VideoAsset) -> VideoAsset:
    info = ffprobe(asset.local_path)
    fmt = info.get("format", {})
    duration = float(fmt.get("duration") or 0)
    bit_rate = int(fmt.get("bit_rate") or 0)
    width = None
    height = None
    for stream in info.get("streams", []):
        if stream.get("codec_type") == "video":
            width = stream.get("width")
            height = stream.get("height")
            break
    asset.duration_seconds = duration
    asset.bit_rate = bit_rate
    asset.width = width
    asset.height = height
    out_dir = settings.derived_dir / asset.video_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "ffprobe.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "asset.json").write_text(asset.model_dump_json(indent=2), encoding="utf-8")
    return asset


def make_segment_plan(
    duration_seconds: float,
    segment_seconds: int,
    start_seconds: int | None = None,
    end_seconds: int | None = None,
) -> list[SegmentPlan]:
    total = max(1, int(duration_seconds or 0))
    start_bound = max(0, int(start_seconds or 0))
    end_bound = total if end_seconds is None else min(total, int(end_seconds))
    if end_bound <= start_bound:
        raise ValueError(f"无效审核时间段：start_seconds={start_bound}, end_seconds={end_bound}")
    if end_bound - start_bound <= segment_seconds:
        return [
            SegmentPlan(
                segment_index=1,
                start_seconds=start_bound,
                end_seconds=end_bound,
                start_time=format_time(start_bound),
                end_time=format_time(end_bound),
            )
        ]
    segments: list[SegmentPlan] = []
    start = start_bound
    index = 1
    while start < end_bound:
        end = min(start + segment_seconds, end_bound)
        segments.append(
            SegmentPlan(
                segment_index=index,
                start_seconds=start,
                end_seconds=end,
                start_time=format_time(start),
                end_time=format_time(end),
            )
        )
        start = end
        index += 1
    return segments


def extract_frames(
    asset: VideoAsset,
    fps: int = 1,
    start_seconds: int | None = None,
    end_seconds: int | None = None,
) -> Path:
    out_dir = settings.derived_dir / asset.video_id / "frames"
    out_dir.mkdir(parents=True, exist_ok=True)
    for old_file in out_dir.glob("*"):
        if old_file.is_file():
            old_file.unlink()
    pattern = str(out_dir / "%06d.jpg")
    start = max(0, int(start_seconds or 0))
    duration = None
    if end_seconds is not None and int(end_seconds) > start:
        duration = int(end_seconds) - start
    cmd = [
        ffmpeg_executable(),
        "-nostdin",
        "-y",
        "-threads",
        str(max(1, settings.ffmpeg_threads)),
        "-filter_threads",
        str(max(1, settings.ffmpeg_filter_threads)),
    ]
    if start:
        cmd.extend(["-ss", str(start)])
    cmd.extend([
        "-i",
        asset.local_path,
    ])
    if duration is not None:
        cmd.extend(["-t", str(duration)])
    cmd.extend([
        "-vf",
        f"fps={fps},scale='if(gt(iw,ih),640,-2)':'if(gt(ih,iw),640,-2)'",
        "-q:v",
        "6",
        pattern,
    ])
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-2000:])
    (out_dir / "frames_meta.json").write_text(
        json.dumps({"start_seconds": start, "fps": fps}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return out_dir


def list_segment_frames(asset: VideoAsset, segment: SegmentPlan, fps: int = 1) -> list[dict]:
    frame_dir = settings.derived_dir / asset.video_id / "frames"
    frames = sorted(frame_dir.glob("*.jpg"))
    meta_path = frame_dir / "frames_meta.json"
    extract_start_seconds = 0.0
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            extract_start_seconds = float(meta.get("start_seconds") or 0)
        except Exception:
            extract_start_seconds = 0.0
    output = []
    for index, frame_path in enumerate(frames, start=1):
        timestamp = extract_start_seconds + ((index - 1) / max(1, fps))
        if segment.start_seconds <= timestamp <= segment.end_seconds:
            output.append(
                {
                    "path": frame_path,
                    "timestamp_seconds": timestamp,
                    "timestamp": format_time(math.floor(timestamp)),
                    "frame_index": index,
                }
            )
    return output


def _clean_subtitle_text(text: str) -> str:
    lines = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.isdigit():
            continue
        if "-->" in line:
            lines.append(line)
            continue
        line = re.sub(r"<[^>]+>", "", line)
        line = re.sub(r"\{\\[^}]+\}", "", line)
        if line:
            lines.append(line)
    return "\n".join(lines)


def extract_embedded_subtitles(asset: VideoAsset) -> str:
    info = ffprobe(asset.local_path)
    subtitle_streams = [
        stream
        for stream in info.get("streams", [])
        if stream.get("codec_type") == "subtitle"
    ]
    if not subtitle_streams:
        return ""
    out_dir = settings.derived_dir / asset.video_id / "subtitles"
    out_dir.mkdir(parents=True, exist_ok=True)
    extracted: list[str] = []
    for index, _stream in enumerate(subtitle_streams):
        target = out_dir / f"subtitle_{index}.srt"
        cmd = [
            ffmpeg_executable(),
            "-nostdin",
            "-y",
            "-threads",
            str(max(1, settings.ffmpeg_threads)),
            "-i",
            asset.local_path,
            "-map",
            f"0:s:{index}",
            str(target),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode == 0 and target.exists():
            extracted.append(_clean_subtitle_text(target.read_text(encoding="utf-8", errors="ignore")))
    return "\n".join(item for item in extracted if item)


def _ocr_frame_subtitle(frame: dict) -> str:
    if not shutil.which("tesseract"):
        return ""
    frame_path = Path(frame["path"])
    with Image.open(frame_path) as image:
        image = image.convert("L")
        width, height = image.size
        crop_top = max(0, int(height * (1 - settings.subtitle_ocr_crop_ratio)))
        cropped = image.crop((0, crop_top, width, height))
        temp_path = frame_path.with_suffix(".subtitle_crop.png")
        cropped.save(temp_path)
    try:
        cmd = [
            "tesseract",
            str(temp_path),
            "stdout",
            "-l",
            settings.subtitle_ocr_lang,
            "--psm",
            "6",
        ]
        env = os.environ.copy()
        env["OMP_THREAD_LIMIT"] = str(max(1, settings.tesseract_thread_limit))
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
        if result.returncode != 0:
            return ""
        return " ".join(result.stdout.split())
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def extract_ocr_subtitles(frames: list[dict]) -> str:
    if not settings.subtitle_ocr_enabled or not shutil.which("tesseract"):
        return ""
    lines: list[str] = []
    last_timestamp = -settings.subtitle_ocr_min_interval_seconds
    last_text = ""
    scanned_frames = 0
    for frame in frames:
        if settings.subtitle_ocr_max_frames > 0 and scanned_frames >= settings.subtitle_ocr_max_frames:
            break
        timestamp_seconds = float(frame.get("timestamp_seconds") or 0)
        if timestamp_seconds - last_timestamp < settings.subtitle_ocr_min_interval_seconds:
            continue
        scanned_frames += 1
        text = _ocr_frame_subtitle(frame)
        if not text or text == last_text:
            continue
        lines.append(f"{frame['timestamp']} {text}")
        last_text = text
        last_timestamp = timestamp_seconds
    return "\n".join(lines)


def extract_subtitle_text(asset: VideoAsset, frames: list[dict] | None = None) -> tuple[str, str]:
    embedded = extract_embedded_subtitles(asset)
    if embedded.strip():
        return embedded, "embedded"
    if frames:
        ocr_text = extract_ocr_subtitles(frames)
        if ocr_text.strip():
            return ocr_text, "ocr"
    return "", "none"


def frame_dhash(path: Path, hash_size: int = 8) -> int:
    with Image.open(path) as image:
        image = image.convert("L").resize((hash_size + 1, hash_size), Image.Resampling.LANCZOS)
        data = image.get_flattened_data() if hasattr(image, "get_flattened_data") else image.getdata()
        pixels = list(data)
    bits = 0
    for row in range(hash_size):
        row_offset = row * (hash_size + 1)
        for col in range(hash_size):
            bits <<= 1
            if pixels[row_offset + col] > pixels[row_offset + col + 1]:
                bits |= 1
    return bits


def hamming_distance(left: int, right: int) -> int:
    return (left ^ right).bit_count()


def filter_distinct_frames(frames: list[dict], threshold: int, max_gap_seconds: float) -> list[dict]:
    selected: list[dict] = []
    last_hash: int | None = None
    last_kept_seconds = -max_gap_seconds
    for frame in frames:
        current_hash = frame_dhash(Path(frame["path"]))
        frame["perceptual_hash"] = f"{current_hash:016x}"
        timestamp_seconds = float(frame.get("timestamp_seconds") or 0)
        if last_hash is None:
            keep = True
        else:
            keep = (
                hamming_distance(current_hash, last_hash) > threshold
                or timestamp_seconds - last_kept_seconds >= max_gap_seconds
            )
        if keep:
            selected.append(frame)
            last_hash = current_hash
            last_kept_seconds = timestamp_seconds
    return selected


def build_frame_sheet(asset: VideoAsset, segment: SegmentPlan, frames: list[dict]) -> dict:
    rows = max(1, settings.frame_sheet_rows)
    cols = max(1, settings.frame_sheet_cols)
    cell_w = 320
    cell_h = 220
    label_h = 28
    sheet_dir = settings.derived_dir / asset.video_id / "sheets"
    sheet_dir.mkdir(parents=True, exist_ok=True)
    first_index = frames[0]["frame_index"] if frames else 0
    last_index = frames[-1]["frame_index"] if frames else 0
    sheet_path = sheet_dir / f"segment_{segment.segment_index}_{first_index}_{last_index}.jpg"
    sheet = Image.new("RGB", (cols * cell_w, rows * (cell_h + label_h)), "white")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    for idx, frame in enumerate(frames[: rows * cols]):
        row = idx // cols
        col = idx % cols
        x = col * cell_w
        y = row * (cell_h + label_h)
        with Image.open(frame["path"]) as image:
            image = image.convert("RGB")
            image.thumbnail((cell_w, cell_h), Image.Resampling.LANCZOS)
            bg = Image.new("RGB", (cell_w, cell_h), "black")
            offset = ((cell_w - image.width) // 2, (cell_h - image.height) // 2)
            bg.paste(image, offset)
        draw.rectangle((x, y, x + cell_w, y + label_h), fill=(245, 245, 245))
        draw.text((x + 8, y + 8), f"{idx + 1}. {frame['timestamp']}", fill=(0, 0, 0), font=font)
        sheet.paste(bg, (x, y + label_h))
        draw.rectangle((x, y, x + cell_w - 1, y + label_h + cell_h - 1), outline=(200, 200, 200))
    sheet.save(sheet_path, quality=86)
    return {
        "path": sheet_path,
        "frames": frames[: rows * cols],
        "timestamp": f"{frames[0]['timestamp']} - {frames[-1]['timestamp']}" if frames else "",
        "frame_count": len(frames[: rows * cols]),
    }
