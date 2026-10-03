"""SQLite 存储（仅本机 ``data/mynote.db``）。

用标准库 ``sqlite3``，不引入 ORM —— 本地单用户场景够用，且依赖更少。

**转写结果单独建表缓存**：同一视频重新生成笔记时不必再转写一次，
这对长视频动辄十几分钟的等待时间来说是刚需。

并发处理：sqlite 连接不跨线程共享，每次操作开一个短连接。
本地单用户应用的写入量极小，这样最简单也最不容易出错。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from app.schemas import (
    Job,
    JobStage,
    NoteRecord,
    Transcript,
    TranscriptSegment,
    TranscriptSource,
)

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id                TEXT PRIMARY KEY,
    bvid              TEXT NOT NULL,
    page              INTEGER NOT NULL DEFAULT 1,
    title             TEXT NOT NULL DEFAULT '',
    one_liner         TEXT NOT NULL DEFAULT '',
    tags              TEXT NOT NULL DEFAULT '[]',
    transcript_source TEXT NOT NULL DEFAULT 'unknown',
    duration          REAL NOT NULL DEFAULT 0,
    char_count        INTEGER NOT NULL DEFAULT 0,
    markdown_path     TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notes_bvid ON notes(bvid);
CREATE INDEX IF NOT EXISTS idx_notes_created ON notes(created_at DESC);

CREATE TABLE IF NOT EXISTS transcripts (
    bvid         TEXT NOT NULL,
    page         INTEGER NOT NULL DEFAULT 1,
    cid          INTEGER NOT NULL DEFAULT 0,
    source       TEXT NOT NULL DEFAULT 'unknown',
    language     TEXT NOT NULL DEFAULT 'zh',
    duration     REAL NOT NULL DEFAULT 0,
    note         TEXT NOT NULL DEFAULT '',
    segments     TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (bvid, page, cid)
);

CREATE TABLE IF NOT EXISTS jobs (
    id         TEXT PRIMARY KEY,
    url        TEXT NOT NULL,
    stage      TEXT NOT NULL DEFAULT 'queued',
    progress   REAL NOT NULL DEFAULT 0,
    message    TEXT NOT NULL DEFAULT '',
    error      TEXT NOT NULL DEFAULT '',
    bvid       TEXT NOT NULL DEFAULT '',
    note_id    TEXT,
    log        TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _loads(text: str, default):
    try:
        value = json.loads(text)
        return value if isinstance(value, type(default)) else default
    except (ValueError, TypeError):
        return default


class Database:
    """轻量 SQLite 封装。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._init_lock = threading.Lock()
        self._initialized = False

    # ------------------------------------------------------------------
    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """打开一个短连接。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init(self) -> None:
        """建表（幂等）。"""
        with self._init_lock:
            if self._initialized:
                return
            with self._connect() as conn:
                conn.executescript(_SCHEMA)
            self._initialized = True
            logger.debug("数据库就绪：%s", self.path)

    def reset(self) -> None:
        """数据库文件被删除后重新建表。

        ⚠️ 必须有这个方法：``Vault.clear_all()`` 会**删掉整个 .db 文件**，
        而 ``init()`` 有 ``_initialized`` 幂等标志 —— 不重置的话
        清空数据之后应用会一直报 ``no such table``，直到重启为止。
        """
        with self._init_lock:
            self._initialized = False
        self.init()

    # ------------------------------------------------------------------
    # 笔记
    # ------------------------------------------------------------------
    def save_note(self, record: NoteRecord, *, page: int = 1) -> None:
        self.init()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO notes (id, bvid, page, title, one_liner, tags,
                                   transcript_source, duration, char_count,
                                   markdown_path, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title,
                    one_liner=excluded.one_liner,
                    tags=excluded.tags,
                    transcript_source=excluded.transcript_source,
                    duration=excluded.duration,
                    char_count=excluded.char_count,
                    markdown_path=excluded.markdown_path,
                    updated_at=excluded.updated_at
                """,
                (
                    record.id,
                    record.bvid,
                    page,
                    record.title,
                    record.one_liner,
                    json.dumps(record.tags, ensure_ascii=False),
                    record.transcript_source.value,
                    record.duration,
                    record.char_count,
                    record.markdown_path,
                    record.created_at.isoformat(timespec="seconds"),
                    record.updated_at.isoformat(timespec="seconds"),
                ),
            )

    def list_notes(self, limit: int = 200) -> list[NoteRecord]:
        self.init()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM notes ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_note(r) for r in rows]

    def get_note(self, note_id: str) -> NoteRecord | None:
        self.init()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM notes WHERE id = ?", (note_id,)
            ).fetchone()
        return self._row_to_note(row) if row else None

    def find_note_by_bvid(self, bvid: str, page: int = 1) -> NoteRecord | None:
        self.init()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM notes WHERE bvid = ? AND page = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (bvid, page),
            ).fetchone()
        return self._row_to_note(row) if row else None

    def delete_note(self, note_id: str) -> bool:
        self.init()
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
            return cur.rowcount > 0

    def dedupe_notes(self) -> int:
        """清理同一 (bvid, page) 的重复记录，只保留最新的一条。

        历史遗留问题：早期版本每次生成都用新的 id，于是反复生成同一个视频
        会在列表里堆出多条一模一样的笔记（磁盘上却只有一份文件，被覆盖了）。
        根本原因已修（改为复用 id），这里在启动时再清一次，
        把已经产生的脏数据修好。返回删除的条数。
        """
        self.init()
        with self._connect() as conn:
            cur = conn.execute(
                """
                DELETE FROM notes
                WHERE id NOT IN (
                    SELECT id FROM (
                        SELECT id,
                               ROW_NUMBER() OVER (
                                   PARTITION BY bvid, page
                                   ORDER BY created_at DESC, rowid DESC
                               ) AS rn
                        FROM notes
                    ) WHERE rn = 1
                )
                """
            )
            removed = cur.rowcount
        if removed:
            logger.warning("清理了 %d 条重复的笔记记录", removed)
        return removed

    def count_notes(self) -> int:
        self.init()
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM notes").fetchone()
        return int(row["n"]) if row else 0

    @staticmethod
    def _row_to_note(row: sqlite3.Row) -> NoteRecord:
        return NoteRecord(
            id=row["id"],
            bvid=row["bvid"],
            title=row["title"],
            one_liner=row["one_liner"],
            tags=_loads(row["tags"], []),
            transcript_source=_safe_source(row["transcript_source"]),
            duration=float(row["duration"] or 0),
            char_count=int(row["char_count"] or 0),
            markdown_path=row["markdown_path"],
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
        )

    # ------------------------------------------------------------------
    # 转写缓存
    # ------------------------------------------------------------------
    def save_transcript(
        self, bvid: str, page: int, cid: int, transcript: Transcript
    ) -> None:
        self.init()
        payload = json.dumps(
            [s.model_dump() for s in transcript.segments], ensure_ascii=False
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO transcripts (bvid, page, cid, source, language,
                                         duration, note, segments, created_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(bvid, page, cid) DO UPDATE SET
                    source=excluded.source,
                    language=excluded.language,
                    duration=excluded.duration,
                    note=excluded.note,
                    segments=excluded.segments,
                    created_at=excluded.created_at
                """,
                (
                    bvid,
                    page,
                    cid,
                    transcript.source.value,
                    transcript.language,
                    transcript.duration,
                    transcript.note,
                    payload,
                    _now(),
                ),
            )

    def get_transcript(
        self, bvid: str, page: int, cid: int
    ) -> Transcript | None:
        """取缓存转写。没有则返回 None。"""
        self.init()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM transcripts WHERE bvid=? AND page=? AND cid=?",
                (bvid, page, cid),
            ).fetchone()
        if not row:
            return None
        raw = _loads(row["segments"], [])
        segments: list[TranscriptSegment] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                segments.append(TranscriptSegment(**item))
            except (TypeError, ValueError):
                continue
        if not segments:
            return None
        return Transcript(
            segments=segments,
            source=_safe_source(row["source"]),
            language=row["language"] or "zh",
            note=f"{row['note']}（来自本地缓存）",
        )

    def delete_transcript(self, bvid: str, page: int, cid: int) -> bool:
        self.init()
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM transcripts WHERE bvid=? AND page=? AND cid=?",
                (bvid, page, cid),
            )
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # 任务
    # ------------------------------------------------------------------
    def save_job(self, job: Job) -> None:
        self.init()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs (id, url, stage, progress, message, error, bvid,
                                  note_id, log, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    stage=excluded.stage,
                    progress=excluded.progress,
                    message=excluded.message,
                    error=excluded.error,
                    bvid=excluded.bvid,
                    note_id=excluded.note_id,
                    log=excluded.log,
                    updated_at=excluded.updated_at
                """,
                (
                    job.id,
                    job.url,
                    job.stage.value,
                    job.progress,
                    job.message,
                    job.error,
                    job.bvid,
                    job.note_id,
                    json.dumps(job.log[-60:], ensure_ascii=False),
                    job.created_at.isoformat(timespec="seconds"),
                    job.updated_at.isoformat(timespec="seconds"),
                ),
            )

    def get_job(self, job_id: str) -> Job | None:
        self.init()
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._row_to_job(row) if row else None

    def list_jobs(self, limit: int = 50) -> list[Job]:
        self.init()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_job(r) for r in rows]

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> Job:
        return Job(
            id=row["id"],
            url=row["url"],
            stage=_safe_stage(row["stage"]),
            progress=float(row["progress"] or 0),
            message=row["message"],
            error=row["error"],
            bvid=row["bvid"],
            note_id=row["note_id"],
            log=_loads(row["log"], []),
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
        )

    def purge_all(self) -> None:
        """清空所有表（用于「一键清除我的数据」）。"""
        self.init()
        with self._connect() as conn:
            for table in ("jobs", "transcripts", "notes"):
                conn.execute(f"DELETE FROM {table}")  # noqa: S608 - 表名是硬编码常量

    def stats(self) -> dict[str, int]:
        self.init()
        with self._connect() as conn:
            return {
                "notes": int(
                    conn.execute("SELECT COUNT(*) AS n FROM notes").fetchone()["n"]
                ),
                "transcripts": int(
                    conn.execute(
                        "SELECT COUNT(*) AS n FROM transcripts"
                    ).fetchone()["n"]
                ),
                "jobs": int(
                    conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
                ),
            }


def new_id() -> str:
    """生成一个短随机 ID。"""
    return uuid.uuid4().hex[:12]


def _parse_dt(value: str | None) -> datetime:
    if not value:
        return datetime.now()
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return datetime.now()


def _safe_source(value: str | None) -> TranscriptSource:
    try:
        return TranscriptSource(value or "unknown")
    except ValueError:
        return TranscriptSource.UNKNOWN


def _safe_stage(value: str | None) -> JobStage:
    try:
        return JobStage(value or "queued")
    except ValueError:
        return JobStage.QUEUED
