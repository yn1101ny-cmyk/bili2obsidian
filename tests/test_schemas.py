"""领域模型测试。"""

from __future__ import annotations

import pytest

from app.schemas import (
    Concept,
    KeyPoint,
    Note,
    Transcript,
    TranscriptSegment,
    TranscriptSource,
    VideoMeta,
    VideoPage,
    format_clock,
)


# ---------------------------------------------------------------------------
# format_clock
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0, "00:00"),
        (5, "00:05"),
        (59, "00:59"),
        (60, "01:00"),
        (90, "01:30"),
        (599, "09:59"),
        (3599, "59:59"),
        (3600, "1:00:00"),
        (3661, "1:01:01"),
        (7325, "2:02:05"),
    ],
)
def test_format_clock(seconds, expected):
    assert format_clock(seconds) == expected


def test_format_clock_handles_none():
    assert format_clock(None) == ""


def test_format_clock_clamps_negative():
    assert format_clock(-10) == "00:00"


def test_format_clock_truncates_fraction():
    assert format_clock(90.9) == "01:30"


# ---------------------------------------------------------------------------
# 时间戳链接（这里曾经有个真实 bug）
# ---------------------------------------------------------------------------


def test_single_page_timestamp_uses_question_mark():
    meta = VideoMeta(bvid="BV1GJ411x7h7", title="t", page=1)
    assert meta.timestamped_url(90) == "https://www.bilibili.com/video/BV1GJ411x7h7?t=90"


def test_multi_page_timestamp_uses_ampersand():
    meta = VideoMeta(bvid="BV1GJ411x7h7", title="t", page=3)
    assert (
        meta.timestamped_url(90)
        == "https://www.bilibili.com/video/BV1GJ411x7h7?p=3&t=90"
    )


def test_timestamp_url_never_produces_bare_ampersand():
    """单P视频不能生成 ``...BVxxx&t=90`` 这种非法链接。"""
    meta = VideoMeta(bvid="BV1GJ411x7h7", title="t", page=1)
    assert "&t=" not in meta.timestamped_url(90)


def test_url_without_page_param():
    meta = VideoMeta(bvid="BV1GJ411x7h7", title="t", page=1)
    assert meta.url == "https://www.bilibili.com/video/BV1GJ411x7h7"


def test_url_with_page_param():
    meta = VideoMeta(bvid="BV1GJ411x7h7", title="t", page=2)
    assert meta.url == "https://www.bilibili.com/video/BV1GJ411x7h7?p=2"


# ---------------------------------------------------------------------------
# 标题展示
# ---------------------------------------------------------------------------


def test_display_title_uses_part_title_when_different():
    meta = VideoMeta(bvid="BV1", title="课程合集", part_title="第三讲：索引")
    assert meta.display_title == "课程合集 - 第三讲：索引"


def test_display_title_falls_back_to_title():
    meta = VideoMeta(bvid="BV1", title="单个视频", part_title="")
    assert meta.display_title == "单个视频"


def test_display_title_ignores_duplicate_part_title():
    meta = VideoMeta(bvid="BV1", title="一样的", part_title="一样的")
    assert meta.display_title == "一样的"


# ---------------------------------------------------------------------------
# TranscriptSegment
# ---------------------------------------------------------------------------


def test_segment_duration():
    seg = TranscriptSegment(start=3.0, end=8.5, text="x")
    assert seg.duration == pytest.approx(5.5)


def test_segment_end_before_start_is_clamped():
    seg = TranscriptSegment(start=10.0, end=2.0, text="x")
    assert seg.end == 10.0
    assert seg.duration == 0.0


def test_segment_clock():
    assert TranscriptSegment(start=95.0, end=100.0, text="x").clock() == "01:35"


def test_segment_rejects_negative_start():
    with pytest.raises(Exception):
        TranscriptSegment(start=-1.0, end=1.0, text="x")


# ---------------------------------------------------------------------------
# Transcript
# ---------------------------------------------------------------------------


def test_transcript_full_text(sample_transcript: Transcript):
    assert "向量数据库" in sample_transcript.full_text
    assert not sample_transcript.is_empty()


def test_transcript_full_text_skips_blank():
    t = Transcript(
        segments=[
            TranscriptSegment(start=0, end=1, text="有内容"),
            TranscriptSegment(start=1, end=2, text="   "),
        ]
    )
    assert t.full_text == "有内容"


def test_transcript_duration_is_last_end(sample_transcript: Transcript):
    assert sample_transcript.duration == 30.0


def test_transcript_empty_detection():
    assert Transcript().is_empty()
    assert Transcript(segments=[TranscriptSegment(start=0, end=1, text=" ")]).is_empty()


def test_transcript_char_count(sample_segments):
    t = Transcript(segments=sample_segments)
    assert t.char_count == sum(len(s.text) for s in sample_segments)


def test_transcript_duration_zero_when_empty():
    assert Transcript().duration == 0.0


# ---------------------------------------------------------------------------
# 转写来源标签
# ---------------------------------------------------------------------------


def test_transcript_source_labels_are_chinese():
    for src in TranscriptSource:
        assert src.label
        assert src.confidence in ("高", "中", "低")


def test_human_subtitles_have_highest_confidence():
    assert TranscriptSource.BILI_CC.confidence == "高"
    assert TranscriptSource.BILI_AI.confidence == "中"
    assert TranscriptSource.WHISPER.confidence == "中"


# ---------------------------------------------------------------------------
# Note
# ---------------------------------------------------------------------------


def test_note_is_thin_when_empty():
    assert Note(title="t", one_liner="", summary="").is_thin()


def test_note_not_thin_with_key_points():
    note = Note(
        title="t",
        one_liner="一句话",
        summary="",
        key_points=[KeyPoint(title="要点", detail="细节")],
    )
    assert not note.is_thin()


def test_key_point_clock_handles_none():
    kp = KeyPoint(title="t", detail="d")
    assert kp.clock() == ""


def test_concept_clock():
    c = Concept(term="ANN", explanation="近似最近邻", timestamp=75.0)
    assert c.clock() == "01:15"


def test_note_defaults_are_empty_lists():
    note = Note(title="t", one_liner="o", summary="s")
    assert note.outline == []
    assert note.key_points == []
    assert note.concepts == []
    assert note.follow_ups == []
    assert note.uncertainties == []
    assert note.tags == []


# ---------------------------------------------------------------------------
# VideoMeta 分P
# ---------------------------------------------------------------------------


def test_video_meta_pages_default_empty():
    assert VideoMeta(bvid="BV1", title="t").pages == []


def test_video_page_model():
    p = VideoPage(cid=123, page=2, title="第二讲", duration=600)
    assert p.cid == 123
    assert p.duration == 600
