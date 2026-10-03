"""分块测试。

分块质量直接决定笔记质量：切碎了语义就断，切漏了就丢内容。
所以这里重点验证「不丢内容」和「有重叠」。
"""

from __future__ import annotations

import pytest

from app.notes.chunking import (
    chunk_segments,
    estimate_tokens,
    transcript_to_text,
)
from app.schemas import TranscriptSegment


def segs(n: int, chars: int = 50) -> list[TranscriptSegment]:
    """造 n 条片段，每条 chars 个字，间隔 1 秒。"""
    return [
        TranscriptSegment(
            start=float(i * 10),
            end=float(i * 10 + 9),
            text="字" * chars,
        )
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# 基本行为
# ---------------------------------------------------------------------------


def test_empty_input_gives_no_chunks():
    assert chunk_segments([]) == []


def test_blank_segments_are_dropped():
    chunks = chunk_segments(
        [
            TranscriptSegment(start=0, end=1, text="有内容"),
            TranscriptSegment(start=1, end=2, text="   "),
            TranscriptSegment(start=2, end=3, text=""),
        ]
    )
    assert len(chunks) == 1
    assert len(chunks[0].segments) == 1


def test_small_input_is_single_chunk():
    chunks = chunk_segments(segs(3, 20), max_chars=2400)
    assert len(chunks) == 1
    assert chunks[0].index == 0
    assert chunks[0].total == 1


def test_large_input_splits():
    chunks = chunk_segments(segs(40, 60), max_chars=400, overlap_chars=0)
    assert len(chunks) > 1
    assert all(c.total == len(chunks) for c in chunks)
    assert [c.index for c in chunks] == list(range(len(chunks)))


# ---------------------------------------------------------------------------
# 不能丢内容
# ---------------------------------------------------------------------------


def test_every_segment_appears_in_some_chunk():
    """最要命的回归：分块过程丢内容。"""
    original = segs(50, 60)
    chunks = chunk_segments(original, max_chars=400, overlap_chars=100)
    included = {id(s) for c in chunks for s in c.segments}
    for s in original:
        assert id(s) in included, f"片段 {s.start}s 丢失了"


def test_all_text_is_recoverable():
    original = segs(30, 30)
    chunks = chunk_segments(original, max_chars=300, overlap_chars=50)
    joined = "".join(s.text for c in chunks for s in c.segments)
    for s in original:
        assert s.text in joined


def test_chunks_are_in_time_order():
    chunks = chunk_segments(segs(30, 50), max_chars=400)
    starts = [c.start for c in chunks]
    assert starts == sorted(starts)


# ---------------------------------------------------------------------------
# 重叠
# ---------------------------------------------------------------------------


def test_overlap_shares_segments():
    chunks = chunk_segments(segs(30, 40), max_chars=300, overlap_chars=100)
    assert len(chunks) >= 3
    first = {id(s) for s in chunks[0].segments}
    second = {id(s) for s in chunks[1].segments}
    assert first & second, "相邻块之间应有重叠，避免结论被切断"


def test_zero_overlap_has_no_shared_segments():
    chunks = chunk_segments(segs(30, 40), max_chars=300, overlap_chars=0)
    id_sets = [{id(s) for s in c.segments} for c in chunks]
    for a, b in zip(id_sets, id_sets[1:]):
        assert not (a & b)


def test_overlap_is_clamped_to_half_of_max_chars():
    """防止 overlap >= max_chars 导致死循环。"""
    chunks = chunk_segments(segs(20, 40), max_chars=200, overlap_chars=99999)
    assert len(chunks) > 1
    # 不会无限膨胀
    assert len(chunks) < 40


# ---------------------------------------------------------------------------
# 尺寸约束
# ---------------------------------------------------------------------------


def test_chunks_respect_char_budget():
    """除单条片段本身超限外，块不应超出预算太多。"""
    max_chars = 400
    chunks = chunk_segments(segs(40, 30), max_chars=max_chars, overlap_chars=0)
    for c in chunks:
        # +12 是时间戳前缀的估算开销
        assert c.char_count + 12 * len(c.segments) <= max_chars + 12 + 30


def test_oversized_single_segment_gets_its_own_chunk():
    huge = TranscriptSegment(start=0, end=10, text="超" * 5000)
    chunks = chunk_segments([huge], max_chars=400)
    assert len(chunks) == 1
    assert chunks[0].segments[0] is huge


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def test_render_with_timestamps():
    chunks = chunk_segments(segs(2, 10))
    text = chunks[0].render(with_timestamps=True)
    assert "[00:00]" in text
    assert "[00:10]" in text


def test_render_without_timestamps():
    chunks = chunk_segments(segs(2, 10))
    text = chunks[0].render(with_timestamps=False)
    assert "[" not in text


def test_time_range_format():
    chunks = chunk_segments(segs(3, 10))
    assert chunks[0].time_range == "00:00 - 00:29"


def test_char_offset_is_monotonic():
    chunks = chunk_segments(segs(30, 40), max_chars=300, overlap_chars=0)
    offsets = [c.char_offset for c in chunks]
    assert offsets == sorted(offsets)


# ---------------------------------------------------------------------------
# token 估算
# ---------------------------------------------------------------------------


def test_estimate_tokens_chinese():
    # 中文约 1 字 1 token
    assert estimate_tokens("中" * 100) == pytest.approx(101, abs=3)


def test_estimate_tokens_ascii():
    # 英文约 3-4 字符 1 token
    assert estimate_tokens("a" * 300) == pytest.approx(101, abs=3)


def test_estimate_tokens_empty():
    assert estimate_tokens("") == 0


def test_estimate_tokens_is_monotonic():
    assert estimate_tokens("字" * 200) > estimate_tokens("字" * 100)


# ---------------------------------------------------------------------------
# 整段渲染
# ---------------------------------------------------------------------------


def test_transcript_to_text_includes_timestamps():
    text = transcript_to_text(segs(3, 5), with_timestamps=True)
    assert "[00:00]" in text
    assert "[00:10]" in text


def test_transcript_to_text_without_timestamps():
    text = transcript_to_text(segs(3, 5), with_timestamps=False)
    assert "[" not in text


def test_transcript_to_text_truncates():
    text = transcript_to_text(segs(100, 50), max_chars=300)
    assert "截断" in text
    assert len(text) < 600


def test_transcript_to_text_skips_blank():
    text = transcript_to_text(
        [
            TranscriptSegment(start=0, end=1, text="有内容"),
            TranscriptSegment(start=1, end=2, text="  "),
        ]
    )
    assert "有内容" in text
    assert text.count("\n") == 0
