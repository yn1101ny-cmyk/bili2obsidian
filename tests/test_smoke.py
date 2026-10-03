"""命令行脚本的纯函数测试。

脚本本身需要真实网络与真实模型，不适合放进单测；
但其中的**报告渲染**是纯逻辑，必须被覆盖 —— 它是你排查问题唯一能看到的东西。
"""

from __future__ import annotations

import pytest

from app.schemas import (
    Transcript,
    TranscriptSegment,
    TranscriptSource,
    VideoMeta,
)
from scripts.smoke_transcribe import format_report


@pytest.fixture
def meta() -> VideoMeta:
    return VideoMeta(
        bvid="BV1GJ411x7h7",
        title="向量数据库入门",
        part_title="第一讲",
        owner_name="某UP主",
        duration=1800,
        page=1,
        total_pages=3,
    )


@pytest.fixture
def transcript() -> Transcript:
    return Transcript(
        segments=[
            TranscriptSegment(start=0.0, end=3.5, text="大家好，今天聊向量数据库。"),
            TranscriptSegment(start=3.5, end=8.0, text="先说结论：它不是银弹。"),
        ],
        source=TranscriptSource.WHISPER,
        language="zh",
        note="模型 large-v3-turbo，设备 cuda（float16，批处理），耗时 42.0s（25.7x 实时）",
    )


def test_report_includes_video_identity(meta, transcript):
    text = format_report(meta, transcript, 42.0)
    assert "向量数据库入门 - 第一讲" in text
    assert "某UP主" in text
    assert "BV1GJ411x7h7" in text


def test_report_includes_source_and_confidence(meta, transcript):
    text = format_report(meta, transcript, 42.0)
    assert "本地Whisper转写" in text
    assert "中" in text  # 可信度


def test_report_includes_counts(meta, transcript):
    text = format_report(meta, transcript, 42.0)
    assert "片段数     2" in text
    assert str(transcript.char_count) in text


def test_report_includes_timestamps_and_text(meta, transcript):
    text = format_report(meta, transcript, 42.0)
    assert "[00:00] 大家好，今天聊向量数据库。" in text
    assert "[00:03] 先说结论：它不是银弹。" in text


def test_report_shows_page_info(meta, transcript):
    text = format_report(meta, transcript, 42.0)
    assert "1/3" in text


def test_report_truncates_long_transcripts(meta):
    transcript = Transcript(
        segments=[
            TranscriptSegment(start=float(i), end=float(i) + 1, text=f"第{i}句")
            for i in range(100)
        ],
        source=TranscriptSource.WHISPER,
    )
    text = format_report(meta, transcript, 10.0)
    assert "还有 75 条" in text
    assert "第99句" not in text


def test_report_handles_empty_transcript(meta):
    text = format_report(meta, Transcript(), 0.0)
    assert "片段数     0" in text
    assert "转写冒烟测试结果" in text


def test_report_handles_missing_owner():
    meta = VideoMeta(bvid="BV1", title="无UP主")
    text = format_report(meta, Transcript(), 0.0)
    assert "未知" in text


def test_report_shows_diagnostic_note(meta, transcript):
    text = format_report(meta, transcript, 42.0)
    assert "large-v3-turbo" in text
    assert "cuda" in text
