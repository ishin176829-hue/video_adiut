import json

from video_review.semantic import (
    CharacterCandidate,
    build_screenplay_semantic_prompt,
    eligible_character_assets,
    json_payload,
    segment_frame_batches,
)


def test_json_payload_accepts_fenced_and_wrapped_model_output():
    raw = """```json
{"SegmentScreenplaySemantic": {"segment_index": 1, "time_range": "00:00-01:00"}}
```"""

    assert json_payload(raw, unwrap_key="SegmentScreenplaySemantic") == {
        "segment_index": 1,
        "time_range": "00:00-01:00",
    }


def test_build_screenplay_semantic_prompt_requires_1fps_and_script_format():
    prompt = build_screenplay_semantic_prompt(
        episode_id="1164174676",
        title="空置车位等一场公道",
        segment_index=2,
        start_time="01:00",
        end_time="02:00",
        frame_interval_seconds=1,
    )

    assert "1 秒 1 帧" in prompt
    assert "剧本式" in prompt
    assert "script_scenes" in prompt
    assert "character_candidates" in prompt
    assert "asset_observations" in prompt
    assert "不能返回 Markdown" in prompt


def test_segment_frame_batches_splits_full_1fps_frames():
    frames = [
        {"path": f"{second:06d}.jpg", "timestamp_seconds": second, "timestamp": f"00:{second:02d}"}
        for second in range(125)
    ]

    batches = segment_frame_batches(frames, segment_seconds=60)

    assert [(batch.start_seconds, batch.end_seconds, len(batch.frames)) for batch in batches] == [
        (0, 60, 60),
        (60, 120, 60),
        (120, 125, 5),
    ]


def test_eligible_character_assets_keeps_only_repeated_characters():
    candidates = [
        CharacterCandidate(
            candidate_id="CAND_A",
            role_label="女主",
            visual_description="年轻女性",
            appearance_count=4,
            first_seen="00:01",
            last_seen="00:30",
            evidence_timestamps=["00:01", "00:10", "00:20", "00:30"],
            face_visible=True,
            asset_quality="good",
        ),
        CharacterCandidate(
            candidate_id="CAND_B",
            role_label="路人",
            visual_description="短暂出现",
            appearance_count=3,
            first_seen="00:05",
            last_seen="00:09",
            evidence_timestamps=["00:05", "00:07", "00:09"],
            face_visible=True,
            asset_quality="usable",
        ),
    ]

    assert eligible_character_assets(candidates) == [candidates[0]]


def test_prompt_schema_is_valid_json_schema():
    prompt = build_screenplay_semantic_prompt(
        episode_id="E1",
        title="测试",
        segment_index=1,
        start_time="00:00",
        end_time="01:00",
        frame_interval_seconds=1,
    )
    marker = "JSON Schema："
    schema_text = prompt.split(marker, 1)[1].strip()

    parsed = json.loads(schema_text)
    assert parsed["properties"]["script_scenes"]
