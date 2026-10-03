"""流水线辅助逻辑测试。

端到端跑真实视频由 ``scripts/mynote.py`` 负责；
这里覆盖流水线里**可纯函数验证**的那部分。
"""

from __future__ import annotations

import pytest

from app.pipeline import (
    merge_for_notes,
    note_title_for_filename,
    reuse_or_new_note_id,
)
from app.schemas import (
    NoteRecord,
    Transcript,
    TranscriptSegment,
    TranscriptSource,
    VideoMeta,
)
from app.storage.db import Database


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


# ---------------------------------------------------------------------------
# 文件名稳定性
# ---------------------------------------------------------------------------


def test_note_filename_uses_video_title_not_model_title():
    """⛔ 回归守卫。

    实测教训：模型每次生成的笔记标题都不一样（同一视频先后得到过
    「青椒虽小…」和「MGSD刹帝利…」），文件名跟着变，
    重复生成时会堆出多个内容重复、文件名不同的笔记。
    """
    meta = VideoMeta(bvid="BV1", title="视频原标题", part_title="")
    assert note_title_for_filename(meta) == "视频原标题"


def test_note_filename_is_stable_across_calls():
    """同样的视频元数据必须给出同样的文件名依据。"""
    meta = VideoMeta(bvid="BV1", title="标题", part_title="")
    assert note_title_for_filename(meta) == note_title_for_filename(meta)


def test_note_filename_includes_part_title_for_multipart():
    meta = VideoMeta(bvid="BV1", title="课程", part_title="第三讲")
    assert note_title_for_filename(meta) == "课程 - 第三讲"


# ---------------------------------------------------------------------------
# 记录 id 稳定性
# ---------------------------------------------------------------------------


@pytest.fixture
def db(settings) -> Database:
    d = Database(settings.db_path)
    d.init()
    return d


def test_reuse_or_new_note_id_generates_when_absent(db):
    """第一次生成应当是新 id。"""
    note_id = reuse_or_new_note_id(db, "BV1GJ411x7h7", 1)
    assert note_id
    assert db.get_note(note_id) is None


def test_reuse_or_new_note_id_reuses_existing(db):
    """⛔ 回归守卫。

    实测：反复生成同一个视频后，笔记列表里堆了 5 条一模一样的记录，
    而磁盘上只有一份文件。必须复用已有 id，让重复生成变成"更新"。
    """
    db.save_note(
        NoteRecord(id="existing1", bvid="BV1GJ411x7h7", title="t", one_liner=""),
        page=1,
    )
    assert reuse_or_new_note_id(db, "BV1GJ411x7h7", 1) == "existing1"


def test_reuse_or_new_note_id_is_stable_across_calls(db):
    db.save_note(
        NoteRecord(id="only", bvid="BV1", title="t", one_liner=""), page=1
    )
    ids = {reuse_or_new_note_id(db, "BV1", 1) for _ in range(5)}
    assert ids == {"only"}


def test_reuse_or_new_note_id_separates_pages(db):
    db.save_note(NoteRecord(id="p1", bvid="BV1", title="a", one_liner=""), page=1)
    db.save_note(NoteRecord(id="p2", bvid="BV1", title="b", one_liner=""), page=2)
    assert reuse_or_new_note_id(db, "BV1", 1) == "p1"
    assert reuse_or_new_note_id(db, "BV1", 2) == "p2"


def test_reuse_or_new_note_id_separates_videos(db):
    db.save_note(NoteRecord(id="a", bvid="BV1", title="x", one_liner=""))
    db.save_note(NoteRecord(id="b", bvid="BV2", title="y", one_liner=""))
    assert reuse_or_new_note_id(db, "BV1", 1) == "a"
    assert reuse_or_new_note_id(db, "BV2", 1) == "b"
