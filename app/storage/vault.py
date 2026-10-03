"""笔记文件仓库 —— 管理 ``data/`` 下的 Markdown 文件。

数据安全的落点：
  * 笔记只写在本机 ``data/notes``；
  * 提供 **一键清除全部本地数据** 的能力，并把「删了什么」如实回报；
  * 所有删除操作都做**路径归属校验**，杜绝越界删除（比如误传 ``../``）。
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings
from app.schemas import NoteRecord, VideoMeta

logger = logging.getLogger(__name__)

#: 文件名里不允许出现的字符
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(text: str, *, limit: int = 80, fallback: str = "note") -> str:
    """把任意标题清理成安全的文件名（不含扩展名）。"""
    cleaned = _UNSAFE.sub("_", (text or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    # Windows 保留名
    if cleaned.upper().split(".")[0] in {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        cleaned = f"_{cleaned}"
    return cleaned[:limit] or fallback


@dataclass(slots=True)
class VaultStats:
    """本地数据占用情况 —— 用于 UI 展示与「清除数据」前确认。"""

    notes: int = 0
    media_files: int = 0
    media_bytes: int = 0
    cache_files: int = 0
    cache_bytes: int = 0
    model_files: int = 0
    model_bytes: int = 0

    @property
    def total_bytes(self) -> int:
        return self.media_bytes + self.cache_bytes + self.model_bytes

    def human(self) -> str:
        return (
            f"笔记 {self.notes} 篇 ｜ "
            f"音频 {self.media_files} 个（{_mb(self.media_bytes)}）｜ "
            f"缓存 {self.cache_files} 个（{_mb(self.cache_bytes)}）｜ "
            f"模型 {self.model_files} 个（{_mb(self.model_bytes)}）"
        )


def _mb(size: int) -> str:
    if size >= 1024**3:
        return f"{size / 1024**3:.2f} GB"
    return f"{size / 1024**2:.1f} MB"


class Vault:
    """笔记文件读写与数据清理。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # ------------------------------------------------------------------
    def ensure(self) -> None:
        self.settings.ensure_dirs()

    def note_path(self, record: NoteRecord) -> Path:
        """确定一篇笔记的落盘路径。"""
        if record.markdown_path:
            return Path(record.markdown_path)
        return self.build_note_path(record.bvid, record.title, page=1)

    def build_note_path(self, bvid: str, title: str, page: int = 1) -> Path:
        """``data/notes/<标题>_<bvid>_p<N>.md`` —— 标题在前便于人眼查找。"""
        stem = safe_filename(title, fallback=bvid)
        suffix = f"_{bvid}"
        if page > 1:
            suffix += f"_p{page}"
        return self.settings.notes_dir / f"{stem}{suffix}.md"

    # ------------------------------------------------------------------
    def write_note(self, path: Path, markdown: str) -> Path:
        """写入笔记文件（覆盖同名文件）。"""
        path = self._guard(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")
        logger.info("笔记已保存：%s", path.name)
        return path

    def read_note(self, path: Path) -> str:
        path = self._guard(path)
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    def read_note_by_id(self, record: NoteRecord) -> str:
        return self.read_note(self.note_path(record))

    def delete_note_file(self, path: Path) -> bool:
        try:
            target = self._guard(path)
        except ValueError:
            return False
        if target.exists() and target.is_file():
            target.unlink()
            return True
        return False

    def list_note_files(self) -> list[Path]:
        if not self.settings.notes_dir.exists():
            return []
        return sorted(
            self.settings.notes_dir.glob("*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

    def write_index(self, markdown: str) -> Path:
        """写一份索引页，方便直接用 Obsidian 等工具打开整个目录。"""
        path = self.settings.notes_dir / "index.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")
        return path

    def write_transcript(
        self, meta: VideoMeta, text: str, source_label: str
    ) -> Path:
        """把转写全文另存一份纯文本，便于全文搜索与校对。"""
        self.settings.ensure_dirs()
        stem = safe_filename(meta.display_title, fallback=meta.bvid)
        path = self.settings.notes_dir / "transcripts" / f"{stem}_{meta.bvid}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        header = (
            f"# {meta.display_title}\n"
            f"# UP主：{meta.owner_name}\n"
            f"# 来源：{meta.url}\n"
            f"# 转写方式：{source_label}\n\n"
        )
        path.write_text(header + text, encoding="utf-8")
        return path

    # ------------------------------------------------------------------
    def stats(self) -> VaultStats:
        """统计本地数据占用。"""
        s = self.settings
        stats = VaultStats()
        stats.notes = len(self.list_note_files())
        stats.media_files, stats.media_bytes = _dir_usage(s.media_dir)
        stats.cache_files, stats.cache_bytes = _dir_usage(s.cache_dir)
        stats.model_files, stats.model_bytes = _dir_usage(s.models_dir)
        return stats

    def clear_media(self) -> int:
        """清除下载的音频（最占空间的部分）。"""
        return _clear_dir(self.settings.media_dir, self.settings.data_dir)

    def clear_cache(self) -> int:
        return _clear_dir(self.settings.cache_dir, self.settings.data_dir)

    def clear_all(self) -> dict[str, int]:
        """删除全部本地数据。

        会删掉 ``data`` 下的所有内容（含笔记、转写缓存、音频、模型），
        随后重建空目录。**这是不可撤销的操作**，调用方必须先跟用户确认。
        """
        target = self.settings.data_dir.resolve()
        removed = {"media": 0, "cache": 0, "models": 0, "notes": 0, "db": 0}

        removed["media"] = _clear_dir(self.settings.media_dir, target)
        removed["cache"] = _clear_dir(self.settings.cache_dir, target)
        removed["models"] = _clear_dir(self.settings.models_dir, target)
        removed["notes"] = _clear_dir(self.settings.notes_dir, target)

        db = self.settings.db_path
        if _inside(db, target) and db.exists():
            for suffix in ("", "-wal", "-shm"):
                p = Path(str(db) + suffix)
                if p.exists():
                    try:
                        p.unlink()
                        removed["db"] += 1
                    except OSError as exc:
                        logger.warning("删除 %s 失败：%s", p, exc)

        self.settings.ensure_dirs()
        logger.warning("已清除全部本地数据：%s", removed)
        return removed

    # ------------------------------------------------------------------
    def _guard(self, path: Path) -> Path:
        """确保路径落在 data 目录内，防止越界读写。"""
        resolved = Path(path).resolve()
        if not _inside(resolved, self.settings.data_dir.resolve()):
            raise ValueError(
                f"拒绝操作 {resolved}：路径不在数据目录 "
                f"{self.settings.data_dir} 内。"
            )
        return resolved


def _inside(path: Path, root: Path) -> bool:
    """判断 path 是否位于 root 之内（含 root 自身）。"""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _dir_usage(path: Path) -> tuple[int, int]:
    """返回 (文件数, 总字节)。目录不存在则 (0, 0)。"""
    if not path.exists():
        return 0, 0
    count = 0
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            count += 1
            try:
                total += p.stat().st_size
            except OSError:
                continue
    return count, total


def _clear_dir(path: Path, root: Path) -> int:
    """删除目录内容但保留目录本身。返回删除的文件数。

    只允许删除 ``root`` 内部的目录。
    """
    if not _inside(path, root):
        logger.error("拒绝删除 %s：不在 %s 内", path, root)
        return 0
    if not path.exists():
        return 0

    removed = 0
    for child in list(path.iterdir()):
        try:
            if child.is_dir():
                removed += sum(1 for p in child.rglob("*") if p.is_file())
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink()
                removed += 1
        except OSError as exc:
            logger.warning("删除 %s 失败：%s", child, exc)
    return removed
