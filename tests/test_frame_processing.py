from pathlib import Path

from PIL import Image

from video_review.models import SegmentPlan, VideoAsset
from video_review.preprocessor import build_frame_sheet, ffmpeg_executable, filter_distinct_frames, hamming_distance


def _make_frame(path: Path, color: tuple[int, int, int]) -> None:
    image = Image.new("RGB", (64, 64), color)
    image.save(path)


def _make_split_frame(path: Path) -> None:
    image = Image.new("RGB", (64, 64), (0, 0, 0))
    for x in range(32, 64):
        for y in range(64):
            image.putpixel((x, y), (255, 255, 255))
    image.save(path)


def test_filter_distinct_frames_keeps_visual_changes(tmp_path, monkeypatch):
    first = tmp_path / "first.jpg"
    duplicate = tmp_path / "duplicate.jpg"
    changed = tmp_path / "changed.jpg"
    _make_frame(first, (0, 0, 0))
    _make_frame(duplicate, (0, 0, 0))
    _make_split_frame(changed)
    frames = [
        {"path": first, "timestamp_seconds": 0, "timestamp": "00:00", "frame_index": 1},
        {"path": duplicate, "timestamp_seconds": 1, "timestamp": "00:01", "frame_index": 2},
        {"path": changed, "timestamp_seconds": 2, "timestamp": "00:02", "frame_index": 3},
    ]

    selected = filter_distinct_frames(frames, threshold=6, max_gap_seconds=100)

    assert [frame["frame_index"] for frame in selected] == [1, 3]
    assert hamming_distance(int(frames[0]["perceptual_hash"], 16), int(frames[1]["perceptual_hash"], 16)) == 0


def test_build_frame_sheet_writes_contact_sheet(tmp_path, monkeypatch):
    monkeypatch.setattr("video_review.preprocessor.settings.data_dir", tmp_path)
    paths = []
    for index in range(4):
        path = tmp_path / f"{index}.jpg"
        _make_frame(path, (index * 40, index * 40, index * 40))
        paths.append(path)
    frames = [
        {"path": path, "timestamp_seconds": index, "timestamp": f"00:0{index}", "frame_index": index + 1}
        for index, path in enumerate(paths)
    ]
    asset = VideoAsset(video_id="video_test", local_path=str(tmp_path / "video.mp4"), sha256="hash")
    segment = SegmentPlan(segment_index=1, start_seconds=0, end_seconds=4, start_time="00:00", end_time="00:04")

    sheet = build_frame_sheet(asset, segment, frames)

    assert sheet["path"].exists()
    assert sheet["frame_count"] == 4


def test_ffmpeg_executable_falls_back_when_system_binary_missing(monkeypatch):
    monkeypatch.setattr("video_review.preprocessor.shutil.which", lambda _name: None)

    assert Path(ffmpeg_executable()).name.startswith("ffmpeg")
