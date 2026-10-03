"""存储层测试：SQLite 索引 + 笔记文件仓库。

重点验证两件事：
  1. 转写缓存能原样往返（这是「重新生成笔记不用重转写」的基础）；
  2. 文件操作**出不了 data 目录**（数据边界）。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from app.schemas import (
    NoteRecord,
    Transcript,
    TranscriptSegment,
    TranscriptSource,
)
from app.storage.db import Database, new_id
from app.storage.vault import Vault, safe_filename


@pytest.fixture
def db(settings) -> Database:
    d = Database(settings.db_path)
    d.init()
    return d


@pytest.fixture
def vault(settings) -> Vault:
    v = Vault(settings)
    v.ensure()
    return v


# ===========================================================================
# Database
# ===========================================================================


def test_init_is_idempotent(settings):
    d = Database(settings.db_path)
    d.init()
    d.init()
    assert settings.db_path.exists()


def test_new_id_is_unique():
    ids = {new_id() for _ in range(100)}
    assert len(ids) == 100
    assert all(len(i) == 12 for i in ids)


# --- 笔记 ---


def test_save_and_get_note(db):
    rec = NoteRecord(id="n1", bvid="BV1", title="标题", one_liner="总结")
    db.save_note(rec)

    got = db.get_note("n1")
    assert got is not None
    assert got.title == "标题"
    assert got.one_liner == "总结"


def test_get_missing_note_returns_none(db):
    assert db.get_note("nope") is None


def test_save_note_upserts(db):
    db.save_note(NoteRecord(id="n1", bvid="BV1", title="旧", one_liner=""))
    db.save_note(NoteRecord(id="n1", bvid="BV1", title="新", one_liner="o"))

    assert db.count_notes() == 1
    assert db.get_note("n1").title == "新"


def test_note_tags_round_trip(db):
    rec = NoteRecord(id="n1", bvid="BV1", title="t", one_liner="", tags=["a", "b", "中文"])
    db.save_note(rec)
    assert db.get_note("n1").tags == ["a", "b", "中文"]


def test_note_transcript_source_round_trip(db):
    for src in TranscriptSource:
        db.save_note(
            NoteRecord(
                id=f"n-{src.value}",
                bvid="BV1",
                title="t",
                one_liner="",
                transcript_source=src,
            )
        )
        assert db.get_note(f"n-{src.value}").transcript_source is src


def test_list_notes_newest_first(db):
    for i in range(3):
        db.save_note(
            NoteRecord(
                id=f"n{i}",
                bvid=f"BV{i}",
                title=f"标题{i}",
                one_liner="",
                created_at=datetime(2026, 1, 1 + i),
            )
        )
    titles = [r.title for r in db.list_notes()]
    assert titles == ["标题2", "标题1", "标题0"]


def test_find_note_by_bvid(db):
    db.save_note(NoteRecord(id="n1", bvid="BV1", title="t", one_liner=""))
    assert db.find_note_by_bvid("BV1") is not None
    assert db.find_note_by_bvid("BV999") is None


def test_find_note_by_bvid_respects_page(db):
    db.save_note(NoteRecord(id="n1", bvid="BV1", title="p1", one_liner=""), page=1)
    db.save_note(NoteRecord(id="n2", bvid="BV1", title="p2", one_liner=""), page=2)

    assert db.find_note_by_bvid("BV1", page=1).title == "p1"
    assert db.find_note_by_bvid("BV1", page=2).title == "p2"


def test_delete_note(db):
    db.save_note(NoteRecord(id="n1", bvid="BV1", title="t", one_liner=""))
    assert db.delete_note("n1") is True
    assert db.get_note("n1") is None
    assert db.delete_note("n1") is False


# --- 重复记录清理 ---


def test_dedupe_removes_duplicate_bvid_page(db):
    """⛔ 回归守卫。

    历史遗留：早期每次生成都用新 id，反复生成同一视频会堆出多条重复记录
    （磁盘上却只有一份文件）。实测出现过 9 条记录对 4 个文件。
    """
    for i in range(5):
        db.save_note(
            NoteRecord(
                id=f"n{i}",
                bvid="BV1",
                title=f"第{i}次",
                one_liner="",
                created_at=datetime(2026, 1, 1 + i),
            ),
            page=1,
        )
    assert db.count_notes() == 5

    removed = db.dedupe_notes()

    assert removed == 4
    assert db.count_notes() == 1
    # 保留的是最新的那条
    assert db.list_notes()[0].title == "第4次"


def test_dedupe_keeps_different_pages(db):
    db.save_note(NoteRecord(id="a", bvid="BV1", title="p1", one_liner=""), page=1)
    db.save_note(NoteRecord(id="b", bvid="BV1", title="p2", one_liner=""), page=2)
    assert db.dedupe_notes() == 0
    assert db.count_notes() == 2


def test_dedupe_keeps_different_videos(db):
    db.save_note(NoteRecord(id="a", bvid="BV1", title="x", one_liner=""))
    db.save_note(NoteRecord(id="b", bvid="BV2", title="y", one_liner=""))
    assert db.dedupe_notes() == 0
    assert db.count_notes() == 2


def test_dedupe_on_empty_db(db):
    assert db.dedupe_notes() == 0


def test_dedupe_is_idempotent(db):
    for i in range(3):
        db.save_note(NoteRecord(id=f"n{i}", bvid="BV1", title="t", one_liner=""))
    db.dedupe_notes()
    assert db.dedupe_notes() == 0


def test_count_notes(db):
    assert db.count_notes() == 0
    db.save_note(NoteRecord(id="n1", bvid="BV1", title="t", one_liner=""))
    assert db.count_notes() == 1


# --- 转写缓存 ---


def test_transcript_round_trip(db, sample_transcript):
    db.save_transcript("BV1", 1, 100, sample_transcript)

    got = db.get_transcript("BV1", 1, 100)
    assert got is not None
    assert len(got.segments) == len(sample_transcript.segments)
    assert got.segments[0].text == sample_transcript.segments[0].text
    assert got.segments[0].start == sample_transcript.segments[0].start
    assert got.source is sample_transcript.source


def test_transcript_missing_returns_none(db):
    assert db.get_transcript("BV_none", 1, 0) is None


def test_transcript_upsert(db, sample_transcript):
    db.save_transcript("BV1", 1, 100, sample_transcript)
    modified = sample_transcript.model_copy(
        update={"segments": [TranscriptSegment(start=0, end=1, text="新内容")]}
    )
    db.save_transcript("BV1", 1, 100, modified)

    got = db.get_transcript("BV1", 1, 100)
    assert len(got.segments) == 1
    assert got.segments[0].text == "新内容"


def test_transcript_cache_note_mentions_cache(db, sample_transcript):
    db.save_transcript("BV1", 1, 100, sample_transcript)
    got = db.get_transcript("BV1", 1, 100)
    assert "缓存" in got.note


def test_transcript_keyed_by_page_and_cid(db, sample_transcript):
    db.save_transcript("BV1", 1, 100, sample_transcript)
    db.save_transcript("BV1", 2, 200, sample_transcript)

    assert db.get_transcript("BV1", 1, 100) is not None
    assert db.get_transcript("BV1", 2, 200) is not None
    assert db.get_transcript("BV1", 1, 999) is None


def test_transcript_empty_segments_not_cached(db):
    db.save_transcript("BV1", 1, 100, Transcript())
    # 空转写没有缓存价值
    assert db.get_transcript("BV1", 1, 100) is None


def test_delete_transcript(db, sample_transcript):
    db.save_transcript("BV1", 1, 100, sample_transcript)
    assert db.delete_transcript("BV1", 1, 100) is True
    assert db.get_transcript("BV1", 1, 100) is None


# --- 任务 ---


def test_job_round_trip(db):
    from app.schemas import Job, JobStage

    job = Job(id="j1", url="https://x", stage=JobStage.QUEUED)
    db.save_job(job)

    got = db.get_job("j1")
    assert got is not None
    assert got.url == "https://x"
    assert got.stage is JobStage.QUEUED


def test_job_progress_update(db):
    from app.schemas import Job, JobStage

    db.save_job(Job(id="j1", url="u"))
    job = db.get_job("j1")
    job.stage = JobStage.TRANSCRIBING
    job.progress = 0.5
    job.push("正在转写")
    db.save_job(job)

    got = db.get_job("j1")
    assert got.stage is JobStage.TRANSCRIBING
    assert got.progress == pytest.approx(0.5)
    assert "正在转写" in got.log


def test_job_unknown_stage_falls_back(db):
    from app.schemas import JobStage

    db.save_job(__import__("app.schemas", fromlist=["Job"]).Job(id="j1", url="u"))
    with db._connect() as conn:  # noqa: SLF001 - 测试里直接改库模拟脏数据
        conn.execute("UPDATE jobs SET stage='nonsense' WHERE id='j1'")
    assert db.get_job("j1").stage is JobStage.QUEUED


def test_list_jobs(db):
    from app.schemas import Job

    for i in range(3):
        db.save_job(Job(id=f"j{i}", url=f"u{i}"))
    assert len(db.list_jobs()) == 3


# --- 清理与统计 ---


def test_purge_all(db, sample_transcript):
    db.save_note(NoteRecord(id="n1", bvid="BV1", title="t", one_liner=""))
    db.save_transcript("BV1", 1, 100, sample_transcript)

    db.purge_all()

    assert db.count_notes() == 0
    assert db.get_transcript("BV1", 1, 100) is None


def test_stats(db, sample_transcript):
    db.save_note(NoteRecord(id="n1", bvid="BV1", title="t", one_liner=""))
    db.save_transcript("BV1", 1, 100, sample_transcript)

    stats = db.stats()
    assert stats["notes"] == 1
    assert stats["transcripts"] == 1


# ===========================================================================
# Vault
# ===========================================================================


@pytest.mark.parametrize(
    "raw,forbidden",
    [
        ("a/b", "/"),
        ("a\\b", "\\"),
        ("a:b", ":"),
        ("a*b", "*"),
        ("a?b", "?"),
        ('a"b', '"'),
        ("a<b", "<"),
        ("a>b", ">"),
        ("a|b", "|"),
    ],
)
def test_safe_filename_strips_unsafe_chars(raw, forbidden):
    assert forbidden not in safe_filename(raw)


def test_safe_filename_handles_windows_reserved_names():
    assert safe_filename("CON") == "_CON"
    assert safe_filename("nul") == "_nul"
    assert safe_filename("COM1") == "_COM1"


def test_safe_filename_uses_fallback_for_empty():
    assert safe_filename("", fallback="BV1") == "BV1"
    assert safe_filename("   ", fallback="BV1") == "BV1"


def test_safe_filename_truncates():
    assert len(safe_filename("字" * 500)) <= 80


def test_safe_filename_collapses_whitespace():
    assert safe_filename("a    b") == "a b"


def test_build_note_path_single_page(vault, settings):
    path = vault.build_note_path("BV1GJ411x7h7", "向量数据库入门")
    assert path.parent == settings.notes_dir
    assert path.name == "向量数据库入门_BV1GJ411x7h7.md"


def test_build_note_path_multi_page(vault):
    path = vault.build_note_path("BV1GJ411x7h7", "课程", page=3)
    assert path.name == "课程_BV1GJ411x7h7_p3.md"


def test_build_note_path_handles_unsafe_title(vault):
    path = vault.build_note_path("BV1", "a/b:c*d?")
    assert "/" not in path.name
    assert ":" not in path.name


def test_write_and_read_note(vault, settings):
    path = vault.build_note_path("BV1", "标题")
    vault.write_note(path, "# 内容\n\n正文")

    assert path.exists()
    assert "正文" in vault.read_note(path)


def test_write_note_creates_parent_dirs(vault, settings):
    path = settings.notes_dir / "sub" / "deep" / "note.md"
    vault.write_note(path, "x")
    assert path.exists()


def test_read_missing_note_returns_empty(vault, settings):
    assert vault.read_note(settings.notes_dir / "nope.md") == ""


def test_vault_guard_blocks_outside_paths(vault, tmp_path):
    outside = tmp_path / "outside.txt"
    with pytest.raises(ValueError):
        vault.write_note(outside, "x")
    assert not outside.exists()


def test_vault_guard_blocks_traversal(vault, settings):
    evil = settings.notes_dir / ".." / ".." / "evil.md"
    with pytest.raises(ValueError):
        vault.write_note(evil, "x")


def test_delete_note_file(vault):
    path = vault.build_note_path("BV1", "标题")
    vault.write_note(path, "x")
    assert vault.delete_note_file(path) is True
    assert not path.exists()
    assert vault.delete_note_file(path) is False


def test_list_note_files(vault):
    for i in range(3):
        vault.write_note(vault.build_note_path(f"BV{i}", f"标题{i}"), "x")
    assert len(vault.list_note_files()) == 3


def test_list_note_files_excludes_index(vault):
    """⛔ 回归守卫：索引页不是笔记。

    实测过统计里多出一篇 —— 因为 glob("*.md") 把 index.md 也算进来了，
    于是「笔记 4 篇」而数据库里只有 3 条。
    """
    vault.write_note(vault.build_note_path("BV1", "真笔记"), "x")
    vault.write_index("# 索引")
    files = vault.list_note_files()
    assert [p.name for p in files] == ["真笔记_BV1.md"]


def test_stats_excludes_index(vault):
    vault.write_note(vault.build_note_path("BV1", "真笔记"), "x")
    vault.write_index("# 索引")
    assert vault.stats().notes == 1


def test_write_index(vault, settings):
    path = vault.write_index("# 索引")
    assert path == settings.notes_dir / "index.md"
    assert path.read_text(encoding="utf-8") == "# 索引"


def test_write_transcript(vault, settings):
    from app.schemas import VideoMeta

    meta = VideoMeta(bvid="BV1", title="标题", owner_name="UP")
    path = vault.write_transcript(meta, "[00:00] 内容", "本地Whisper转写")

    text = path.read_text(encoding="utf-8")
    assert "标题" in text
    assert "本地Whisper转写" in text
    assert "[00:00] 内容" in text
    assert path.parent.name == "transcripts"


def test_stats_reports_usage(vault, settings, sample_transcript):
    vault.write_note(vault.build_note_path("BV1", "标题"), "x" * 100)
    (settings.media_dir / "a.m4a").write_bytes(b"x" * 2048)

    stats = vault.stats()
    assert stats.notes == 1
    assert stats.media_files == 1
    assert stats.media_bytes == 2048
    assert "笔记 1 篇" in stats.human()


def test_stats_on_empty_vault(vault):
    stats = vault.stats()
    assert stats.notes == 0
    assert stats.total_bytes == 0


def test_clear_media(vault, settings):
    (settings.media_dir / "audio.m4a").write_bytes(b"x" * 100)
    (settings.media_dir / "nested").mkdir()
    (settings.media_dir / "nested" / "b.m4a").write_bytes(b"y" * 100)

    removed = vault.clear_media()
    assert removed == 2
    assert list(settings.media_dir.iterdir()) == []


def test_clear_cache(vault, settings):
    (settings.cache_dir / "c.json").write_text("{}", encoding="utf-8")
    assert vault.clear_cache() == 1
    assert list(settings.cache_dir.iterdir()) == []


def test_clear_all_removes_everything_and_recreates_dirs(vault, settings, sample_transcript):
    db = Database(settings.db_path)
    db.init()
    db.save_note(NoteRecord(id="n1", bvid="BV1", title="t", one_liner=""))

    vault.write_note(vault.build_note_path("BV1", "标题"), "x")
    (settings.media_dir / "a.m4a").write_bytes(b"x")
    (settings.cache_dir / "c.json").write_text("{}", encoding="utf-8")

    removed = vault.clear_all()

    assert removed["notes"] >= 1
    assert removed["media"] >= 1
    assert removed["db"] >= 1
    assert not list(settings.notes_dir.glob("*.md"))
    assert not settings.db_path.exists()
    # 目录要重建，否则后续写入会失败
    assert settings.notes_dir.is_dir()
    assert settings.media_dir.is_dir()
