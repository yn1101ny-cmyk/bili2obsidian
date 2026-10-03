"""流水线辅助逻辑测试。

端到端跑真实视频由 ``scripts/mynote.py`` 负责；
这里覆盖流水线里**可纯函数验证**的那部分。
"""

from __future__ import annotations

import pytest

from app.pipeline import merge_for_notes
from app.schemas import Transcript, TranscriptSegment, TranscriptSource


def _seg(i: int, text: str, dur: float = 1.0) -> TranscriptSegment:
    return TranscriptSegment(start=float(i), end=float(i) + dur, text=text)


# ---------------------------------------------------------------------------
# 碎句合并
# ---------------------------------------------------------------------------


def test_merge_combines_fragmented_segments():
    """Whisper 与 B站 AI 字幕都产出几个字一条的碎片，必须合并后才喂给模型。"""
    transcript = Transcript(
        segments=[
            _seg(0, "大家好"),
            _seg(1, "今天我们"),
            _seg(2, "聊聊向量数据库"),
        ],
        source=TranscriptSource.WHISPER,
    )
    merged = merge_for_notes(transcript)

    assert len(merged.segments) == 1
    assert merged.segments[0].text == "大家好今天我们聊聊向量数据库"


def test_merge_keeps_first_timestamp():
    """合并后时间戳必须仍指向该段真正开始的位置，否则跳转会错位。"""
    transcript = Transcript(
        segments=[_seg(30, "第一句"), _seg(31, "第二句")]
    )
    merged = merge_for_notes(transcript)
    assert merged.segments[0].start == 30.0


def test_merge_preserves_source_and_language():
    transcript = Transcript(
        segments=[_seg(0, "甲"), _seg(1, "乙")],
        source=TranscriptSource.BILI_AI,
        language="zh",
        note="原始说明",
    )
    merged = merge_for_notes(transcript)
    assert merged.source is TranscriptSource.BILI_AI
    assert merged.language == "zh"


def test_merge_leaves_single_segment_alone():
    transcript = Transcript(segments=[_seg(0, "只有一句")])
    assert merge_for_notes(transcript).segments == transcript.segments


def test_merge_leaves_empty_transcript_alone():
    transcript = Transcript()
    assert merge_for_notes(transcript).segments == []


def test_merge_does_not_lose_text():
    """合并绝不能丢字。"""
    segments = [_seg(i, f"第{i}句") for i in range(40)]
    transcript = Transcript(segments=segments)
    merged = merge_for_notes(transcript)

    joined = "".join(s.text for s in merged.segments)
    for seg in segments:
        assert seg.text in joined


def test_merge_is_idempotent():
    """已经合并过的输入不应再被改变。"""
    transcript = Transcript(segments=[_seg(0, "很长的一句话" * 20)])
    once = merge_for_notes(transcript)
    twice = merge_for_notes(once)
    assert len(twice.segments) == len(once.segments)


def test_merge_splits_on_long_gap():
    transcript = Transcript(
        segments=[_seg(0, "开头"), _seg(100, "很久以后")]
    )
    merged = merge_for_notes(transcript)
    assert len(merged.segments) == 2


def test_merge_result_is_still_a_valid_transcript():
    transcript = Transcript(
        segments=[_seg(i, f"第{i}句") for i in range(20)],
        source=TranscriptSource.WHISPER,
    )
    merged = merge_for_notes(transcript)
    assert not merged.is_empty()
    assert merged.char_count > 0
    assert merged.segments == sorted(merged.segments, key=lambda s: s.start)
