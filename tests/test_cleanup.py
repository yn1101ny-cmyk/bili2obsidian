"""转写清洗测试。

Whisper 的复读幻觉是实测发现的问题：56 分钟的视频里多处出现
整段重复同一个字（``替替替替…``），这些垃圾会被模型当成内容去总结。
"""

from __future__ import annotations

import pytest

from app.asr.cleanup import (
    DEFAULT_MIN_RUN,
    clean_segments,
    clean_text,
    collapse_repeats,
    is_mostly_garbage,
)
from app.schemas import TranscriptSegment


# ---------------------------------------------------------------------------
# 字符压缩
# ---------------------------------------------------------------------------


def test_collapses_long_repeat_run():
    assert collapse_repeats("替替替替替替") == "替"


def test_collapses_run_above_threshold():
    assert collapse_repeats("宗" * 20) == "宗"


def test_keeps_short_repeats():
    """正常的「哈哈」「谢谢」不能被误伤。"""
    assert collapse_repeats("哈哈哈") == "哈哈哈"
    assert collapse_repeats("谢谢谢谢") == "谢谢谢谢"


def test_keeps_text_around_the_run():
    assert collapse_repeats("前面替替替替替替后面") == "前面替后面"


def test_collapses_multiple_runs():
    assert collapse_repeats("替替替替替替中间宗宗宗宗宗宗") == "替中间宗"


def test_respects_custom_threshold():
    assert collapse_repeats("啊啊啊啊", min_run=4) == "啊"
    assert collapse_repeats("啊啊啊啊", min_run=10) == "啊啊啊啊"


def test_handles_empty_and_short():
    assert collapse_repeats("") == ""
    assert collapse_repeats("正常文本") == "正常文本"


def test_min_run_below_two_is_a_noop():
    assert collapse_repeats("哈哈哈", min_run=1) == "哈哈哈"


def test_detects_repeated_multi_char_phrase():
    """实测还有「余帽余帽余帽…」这种整词重复。

    单字符正则处理不了（没有连续 6 个相同字符），
    必须靠周期性检测才能识别出来。
    """
    from app.asr.cleanup import repetition_ratio

    text = "余帽" * 30
    assert repetition_ratio(text) == pytest.approx(1.0)
    assert is_mostly_garbage(text, collapse_repeats(text)) is True


def test_period_ratio_low_for_normal_text():
    from app.asr.cleanup import repetition_ratio

    text = "这是一段正常的转写内容，讲的是电脑配置推荐，没有任何复读现象。"
    assert repetition_ratio(text) < 0.5


def test_period_ratio_ignores_very_short_text():
    from app.asr.cleanup import repetition_ratio

    assert repetition_ratio("哈哈") == 0.0
    assert repetition_ratio("") == 0.0


# ---------------------------------------------------------------------------
# 垃圾判定
# ---------------------------------------------------------------------------


def test_mostly_garbage_for_pure_repeat():
    text = "替" * 100
    assert is_mostly_garbage(text, collapse_repeats(text)) is True


def test_not_garbage_for_normal_text():
    text = "这是一段正常的转写内容，没有复读问题。"
    assert is_mostly_garbage(text, collapse_repeats(text)) is False


def test_not_garbage_when_repeat_is_a_small_part():
    text = "这段话很长" * 10 + "替替替替替替" + "后面还有很多内容" * 5
    assert is_mostly_garbage(text, collapse_repeats(text)) is False


def test_empty_text_is_garbage():
    assert is_mostly_garbage("", "") is True
    assert is_mostly_garbage("   ", "   ") is True


# ---------------------------------------------------------------------------
# 单段清洗
# ---------------------------------------------------------------------------


def test_clean_text_returns_flag_for_garbage():
    cleaned, garbage = clean_text("替" * 50)
    assert garbage is True
    assert cleaned == ""


def test_clean_text_keeps_normal():
    text = "正常的转写内容"
    cleaned, garbage = clean_text(text)
    assert garbage is False
    assert cleaned == text


def test_clean_text_rewrites_mixed():
    cleaned, garbage = clean_text("前替替替替替替后")
    assert garbage is False
    assert cleaned == "前替后"


# ---------------------------------------------------------------------------
# 批量清洗
# ---------------------------------------------------------------------------


def _seg(i: int, text: str) -> TranscriptSegment:
    return TranscriptSegment(start=float(i), end=float(i) + 1, text=text)


def test_clean_segments_drops_garbage():
    segments = [
        _seg(0, "正常内容一"),
        _seg(1, "替" * 80),
        _seg(2, "正常内容二"),
    ]
    kept, dropped, rewritten = clean_segments(segments)
    assert len(kept) == 2
    assert dropped == 1
    assert [s.text for s in kept] == ["正常内容一", "正常内容二"]


def test_clean_segments_rewrites_mixed():
    segments = [_seg(0, "前替替替替替替后")]
    kept, dropped, rewritten = clean_segments(segments)
    assert dropped == 0
    assert rewritten == 1
    assert kept[0].text == "前替后"


def test_clean_segments_preserves_timestamps():
    segments = [_seg(42, "前替替替替替替后")]
    kept, _, _ = clean_segments(segments)
    assert kept[0].start == 42.0


def test_clean_segments_noop_on_clean_input():
    segments = [_seg(0, "干净的内容"), _seg(1, "也很干净")]
    kept, dropped, rewritten = clean_segments(segments)
    assert len(kept) == 2
    assert dropped == 0
    assert rewritten == 0


def test_clean_segments_empty_input():
    kept, dropped, rewritten = clean_segments([])
    assert kept == []
    assert dropped == 0


def test_clean_segments_all_garbage():
    kept, dropped, _ = clean_segments([_seg(0, "宗" * 60), _seg(1, "达" * 60)])
    assert kept == []
    assert dropped == 2


def test_default_min_run_is_reasonable():
    """阈值太小会误伤正常口语，太大又抓不到复读。"""
    assert 4 <= DEFAULT_MIN_RUN <= 10
