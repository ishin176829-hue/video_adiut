from video_review.preprocessor import make_segment_plan


def test_make_segment_plan_can_limit_review_range():
    segments = make_segment_plan(100, 30, start_seconds=10, end_seconds=75)

    assert [segment.start_seconds for segment in segments] == [10, 40, 70]
    assert [segment.end_seconds for segment in segments] == [40, 70, 75]
    assert segments[0].start_time == "00:10"
    assert segments[-1].end_time == "01:15"
