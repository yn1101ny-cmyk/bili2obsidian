"""端到端流水线：B站链接 → 可理解笔记。

完整链路::

    解析链接 → 取元数据 → 尝试字幕 ─┬─ 有字幕 → 直接用
                                    └─ 无字幕 → 下载音频 → Whisper 转写
                                              ↓
                                    生成笔记（本地模型）→ 渲染 Markdown → 落盘 + 入库

几个值得说明的工程决策：

* **转写结果会缓存**：同一视频再次生成笔记不必重新转写（长视频能省十几分钟）。
* **转写完立即卸载 Whisper**：8GB 显存要同时容纳 Whisper 和 Ollama 很勉强，
  转写完释放显存，让笔记生成有充足空间。
* **降级链明确**：字幕拿不到不是错误，是预期路径；只有「字幕和转写都没有」
  才算失败。
* **可取消**：转写/生成是长任务，支持中途取消并如实回报。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from app.asr.whisper_engine import WhisperEngine, build_hotwords
from app.bili.client import BiliClient
from app.bili.subtitles import merge_adjacent_segments
from app.bili.urls import parse_video_input
from app.config import Settings
from app.errors import MyNoteError, NoTranscriptAvailable
from app.llm.factory import build_provider
from app.media.downloader import AudioDownloader
from app.notes.chunking import transcript_to_text
from app.notes.pipeline import NoteGenerator
from app.notes.render import render_index_markdown, render_note_markdown
from app.schemas import (
    Job,
    JobStage,
    Note,
    NoteRecord,
    Transcript,
    TranscriptSource,
    VideoMeta,
)
from app.storage.db import Database, new_id
from app.storage.vault import Vault

logger = logging.getLogger(__name__)

StageCb = Callable[[JobStage, float, str], None]


@dataclass(slots=True)
class RunResult:
    """一次流水线的产出。"""

    note_id: str
    meta: VideoMeta
    transcript: Transcript
    note: Note
    markdown_path: str
    #: 给用户看的补充说明（比如「没有字幕，已转写」）
    notices: list[str] = field(default_factory=list)
    cancelled: bool = False


class Cancelled(MyNoteError):
    """用户主动取消了任务。"""


def merge_for_notes(transcript: Transcript) -> Transcript:
    """把碎句合并成可读段落，供笔记生成使用。

    B站 AI 字幕和 Whisper 都会产出很短的片段（几个字一条）。
    直接喂给模型会导致语义割裂、浪费上下文窗口；
    合并后模型看到的是完整句子，写出的要点明显更连贯。
    时间戳保留每段的首句，定位依然准确。
    """
    if len(transcript.segments) < 2:
        return transcript
    merged = merge_adjacent_segments(transcript.segments)
    if len(merged) >= len(transcript.segments):
        return transcript
    logger.info("合并碎句：%d -> %d 段", len(transcript.segments), len(merged))
    return transcript.model_copy(update={"segments": merged})


class NotePipeline:
    """串起所有环节的编排器。"""

    def __init__(
        self,
        settings: Settings,
        *,
        db: Database | None = None,
        vault: Vault | None = None,
    ) -> None:
        self.settings = settings
        self.settings.ensure_dirs()
        self.db = db or Database(settings.db_path)
        self.vault = vault or Vault(settings)
        self._asr = WhisperEngine(settings)

    # ------------------------------------------------------------------
    async def run(
        self,
        url: str,
        *,
        on_stage: StageCb | None = None,
        cancel: asyncio.Event | None = None,
        reuse_transcript: bool = True,
        force_transcribe: bool = False,
        force_note: bool = False,
    ) -> RunResult:
        """执行一次完整流程。"""
        notices: list[str] = []

        def report(stage: JobStage, progress: float, message: str) -> None:
            if on_stage:
                on_stage(stage, progress, message)

        def check_cancel() -> None:
            if cancel is not None and cancel.is_set():
                raise Cancelled("任务已被用户取消")

        # ---------------- 1. 解析链接 ----------------
        report(JobStage.RESOLVING, 0.02, "正在解析链接…")
        ref = parse_video_input(url)

        async with BiliClient(self.settings) as bili:
            if ref.needs_resolution:
                report(JobStage.RESOLVING, 0.03, "正在展开短链…")
                bvid = await bili.resolve_short_url(ref.bvid or "")
                ref = parse_video_input(bvid)

            # ---------------- 2. 元数据 ----------------
            report(JobStage.RESOLVING, 0.05, "正在获取视频信息…")
            meta = await bili.get_video_meta(ref)
            if not meta.title:
                raise MyNoteError("拿不到视频标题，可能视频不存在或已被删除")
            tags = await bili.get_tags(meta.bvid)
            if tags:
                meta.tags = tags

            page = meta.page
            cid = meta.cid or 0

            report(
                JobStage.RESOLVING,
                0.07,
                f"《{meta.display_title}》"
                + (f"（共 {meta.total_pages} P）" if meta.total_pages > 1 else ""),
            )

            # ---------------- 3. 转写：缓存 → 字幕 → Whisper ----------------
            transcript = await self._obtain_transcript(
                bili,
                meta,
                page=page,
                cid=cid,
                report=report,
                check_cancel=check_cancel,
                notices=notices,
                reuse_transcript=reuse_transcript,
                force_transcribe=force_transcribe,
            )

        check_cancel()

        # 无论转写来自缓存还是刚刚生成，都统一合并一次碎句再喂给模型。
        # 放在使用点而不是只放在保存点，是为了让改动前存下的旧缓存
        # 也能享受到同样的处理（merge_for_notes 是幂等的）。
        transcript = merge_for_notes(transcript)

        # ---------------- 4. 生成笔记 ----------------
        report(JobStage.SUMMARIZING, 0.62, "正在加载本地模型…")
        provider = build_provider(self.settings)
        try:
            generator = NoteGenerator(provider, self.settings)
            note = await generator.generate(
                transcript,
                meta,
                on_progress=lambda frac, msg: (
                    check_cancel(),
                    report(
                        JobStage.SUMMARIZING, 0.62 + 0.33 * frac, msg
                    ),
                )[1],
            )
        finally:
            await provider.aclose()

        check_cancel()

        if provider.is_remote:
            notices.append(
                f"⚠️ 本次笔记由**云端模型**（{provider.name}）生成，"
                "视频字幕文本已发送给第三方服务。"
            )

        # ---------------- 5. 渲染与落盘 ----------------
        report(JobStage.RENDERING, 0.96, "正在保存笔记…")
        note_id = new_id()
        markdown = render_note_markdown(note, meta, transcript)

        path = self.vault.build_note_path(meta.bvid, note.title or meta.display_title, page)
        self.vault.write_note(path, markdown)

        # 转写全文另存一份，便于全文搜索
        try:
            self.vault.write_transcript(
                meta,
                transcript_to_text(transcript.segments, with_timestamps=True),
                transcript.source.label,
            )
        except OSError as exc:
            logger.warning("保存转写全文失败：%s", exc)

        record = NoteRecord(
            id=note_id,
            bvid=meta.bvid,
            title=note.title or meta.display_title,
            one_liner=note.one_liner,
            tags=note.tags,
            transcript_source=transcript.source,
            duration=meta.duration or transcript.duration,
            char_count=transcript.char_count,
            markdown_path=str(path),
        )
        self.db.save_note(record, page=page)
        try:
            self.vault.write_index(render_index_markdown(self.db.list_notes()))
        except OSError as exc:
            logger.warning("更新索引失败：%s", exc)

        report(JobStage.DONE, 1.0, "完成")

        return RunResult(
            note_id=note_id,
            meta=meta,
            transcript=transcript,
            note=note,
            markdown_path=str(path),
            notices=notices,
        )

    # ------------------------------------------------------------------
    async def _obtain_transcript(
        self,
        bili: BiliClient,
        meta: VideoMeta,
        *,
        page: int,
        cid: int,
        report: StageCb,
        check_cancel: Callable[[], None],
        notices: list[str],
        reuse_transcript: bool,
        force_transcribe: bool,
    ) -> Transcript:
        """按「缓存 → B站字幕 → 本地转写」的优先级拿到转写。"""
        # ---- 1. 本地缓存 ----
        if reuse_transcript and not force_transcribe:
            cached = self.db.get_transcript(meta.bvid, page, cid)
            if cached is not None and not cached.is_empty():
                report(
                    JobStage.FETCHING_SUBS,
                    0.10,
                    f"复用已有的转写（{cached.source.label}）",
                )
                notices.append(
                    f"♻️ 复用了本机已缓存的转写（{cached.source.label}），"
                    "未重新下载或转写。"
                )
                return cached

        # ---- 2. B站字幕 ----
        check_cancel()
        report(JobStage.FETCHING_SUBS, 0.12, "正在查找视频字幕…")
        if meta.aid is not None and cid:
            transcript = await bili.try_get_subtitle(meta.aid, cid)
            if transcript is not None and not transcript.is_empty():
                transcript = merge_for_notes(transcript)
                self.db.save_transcript(meta.bvid, page, cid, transcript)
                notices.append(
                    f"✅ 直接使用了 B站的字幕（{transcript.source.label}），未做语音转写。"
                )
                return transcript

        why = (
            "未配置 B站登录态（B站AI字幕通常需要登录）"
            if not self.settings.has_bili_login
            else "该视频没有可用字幕"
        )
        notices.append(f"ℹ️ {why}，改用本地语音转写。")

        # ---- 3. 本地 Whisper 转写 ----
        check_cancel()
        report(JobStage.DOWNLOADING, 0.16, "正在下载音频…")
        downloader = AudioDownloader(self.settings)
        audio_path = await downloader.download(
            url=meta.url,
            bvid=meta.bvid,
            page=page,
            title=meta.part_title or meta.title,
            on_progress=lambda frac, msg: report(
                JobStage.DOWNLOADING, 0.16 + 0.16 * frac, msg
            ),
        )

        check_cancel()
        report(JobStage.TRANSCRIBING, 0.34, "正在本地转写语音（较慢，请稍候）…")
        # 把标题/标签作为热词注入，纠正专有名词的同音字错误
        # （实测：标题里的「刹帝利」被听成「差跌力」）
        hotwords = build_hotwords(
            title=meta.title,
            part_title=meta.part_title,
            owner=meta.owner_name,
            tags=meta.tags,
        )
        try:
            transcript = await asyncio.to_thread(
                self._asr.transcribe, audio_path, hotwords=hotwords or None
            )
        finally:
            # 转写完成立刻释放显存，把空间让给后续的本地大模型
            self._asr.unload()

        if transcript.is_empty():
            raise NoTranscriptAvailable(
                "转写结果为空：可能是纯音乐视频或无人声内容。"
            )

        transcript = merge_for_notes(transcript)
        notices.append(f"🎧 已用本地 Whisper 转写完成（{transcript.note}）。")
        self.db.save_transcript(meta.bvid, page, cid, transcript)
        return transcript

    # ------------------------------------------------------------------
    def unload_models(self) -> None:
        """释放转写模型占用的资源。"""
        self._asr.unload()


# ---------------------------------------------------------------------------
# 任务管理器：把流水线放到后台跑，并持续更新进度
# ---------------------------------------------------------------------------


class JobManager:
    """管理后台任务的生命周期与进度持久化。"""

    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self._tasks: dict[str, asyncio.Task] = {}
        self._cancels: dict[str, asyncio.Event] = {}

    # ------------------------------------------------------------------
    def submit(self, url: str) -> Job:
        """登记一个新任务并启动后台执行。"""
        job = Job(id=new_id(), url=url, stage=JobStage.QUEUED, message="已加入队列")
        self.db.save_job(job)
        cancel = asyncio.Event()
        self._cancels[job.id] = cancel
        task = asyncio.create_task(self._execute(job.id, url, cancel))
        self._tasks[job.id] = task
        task.add_done_callback(lambda _t: self._cleanup(job.id))
        return job

    def _cleanup(self, job_id: str) -> None:
        self._tasks.pop(job_id, None)
        self._cancels.pop(job_id, None)

    def cancel(self, job_id: str) -> bool:
        """请求取消任务。"""
        event = self._cancels.get(job_id)
        if event is None:
            return False
        event.set()
        return True

    def is_running(self, job_id: str) -> bool:
        task = self._tasks.get(job_id)
        return task is not None and not task.done()

    def active_count(self) -> int:
        return sum(1 for t in self._tasks.values() if not t.done())

    # ------------------------------------------------------------------
    async def _execute(
        self, job_id: str, url: str, cancel: asyncio.Event
    ) -> None:
        job = self.db.get_job(job_id)
        if job is None:
            return

        pipeline = NotePipeline(self.settings, db=self.db)

        def on_stage(stage: JobStage, progress: float, message: str) -> None:
            job.stage = stage
            job.progress = max(0.0, min(1.0, progress))
            job.push(message)
            self.db.save_job(job)

        try:
            result = await pipeline.run(
                url, on_stage=on_stage, cancel=cancel
            )
            job.stage = JobStage.DONE
            job.progress = 1.0
            job.bvid = result.meta.bvid
            job.note_id = result.note_id
            for n in result.notices:
                job.log.append(n)
            job.push(f"笔记已保存：{result.markdown_path}")
            self.db.save_job(job)

        except Cancelled:
            job.stage = JobStage.CANCELLED
            job.push("任务已取消")
            self.db.save_job(job)

        except asyncio.CancelledError:
            job.stage = JobStage.CANCELLED
            job.push("任务已取消")
            self.db.save_job(job)
            raise

        except Exception as exc:  # noqa: BLE001 - 任何失败都要如实记录给用户
            logger.exception("任务 %s 失败", job_id)
            job.stage = JobStage.FAILED
            job.error = str(exc)
            job.push(f"失败：{exc}")
            self.db.save_job(job)

        finally:
            pipeline.unload_models()

    # ------------------------------------------------------------------
    async def shutdown(self) -> None:
        """取消所有在跑的任务。"""
        for event in self._cancels.values():
            event.set()
        tasks = [t for t in self._tasks.values() if not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._cancels.clear()
