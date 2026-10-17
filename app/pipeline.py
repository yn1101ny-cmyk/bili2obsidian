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
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from app.asr.cleanup import clean_segments
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


def note_title_for_filename(meta: VideoMeta) -> str:
    """笔记文件名该用哪个标题。

    **用视频原标题，不要用模型改写的标题。**
    实测教训：模型每次生成的笔记标题都不一样（同一个视频先后得到过
    「青椒虽小…」和「MGSD刹帝利…」两个标题），文件名跟着变，
    于是重复生成时会堆出好几个内容重复、文件名不同的笔记。
    原标题是稳定的，重复生成会覆盖同一个文件。
    """
    return meta.display_title


def reuse_or_new_note_id(db: Any, bvid: str, page: int) -> str:
    """同一视频重复生成时，复用已有的记录 id。

    **记录 id 必须和文件名一样稳定。**
    实测：反复生成同一个视频后，笔记列表里堆了 5 条一模一样的「青椒虽小…」
    —— 每次都 ``new_id()`` 插入新记录，而磁盘上只有一份文件（被覆盖了）。
    复用 id 之后，重复生成是"更新"而不是"新增"。
    """
    existing = db.find_note_by_bvid(bvid, page)
    return existing.id if existing is not None else new_id()


def merge_for_notes(transcript: Transcript, *, repeat_min_run: int = 6) -> Transcript:
    """把转写整理成适合喂给模型的形式：**先清洗复读，再合并碎句**。

    分两步，顺序不能反：

    1. **清洗复读幻觉**。长视频里 Whisper 会整段重复同一个字
       （``替替替替…``）或同一个词（``余帽余帽…``）。这些垃圾会被模型
       当成内容去总结，必须在合并之前先丢掉 —— 否则合并会把它们
       和正常内容粘在一起，更难清理。

    2. **合并碎句**。Whisper 与 B站 AI 字幕都产出几个字一条的碎片，
       直接喂给模型会导致语义割裂、浪费上下文窗口。
       时间戳保留每段的首句，定位依然准确。

    这个函数是**幂等**的，所以可以同时放在「保存前」和「使用前」，
    让改动之前存下的旧缓存也能享受到同样的处理。
    """
    if not transcript.segments:
        return transcript

    # ---- 1. 清洗复读 ----
    cleaned, dropped, rewritten = clean_segments(
        transcript.segments, min_run=repeat_min_run
    )
    if dropped or rewritten:
        transcript = transcript.model_copy(update={"segments": cleaned})

    # ---- 2. 合并碎句 ----
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

            # 章节是「不分析音频也能拿到」的结构化线索：
            # 相当于平台已经替我们划分好了视频的主干，
            # 用它来推断重点比让模型从零猜靠谱得多。
            # 与字幕共用 player/v2 的缓存，不会多一次请求。
            if meta.aid is not None and meta.cid:
                meta.chapters = await bili.get_chapters(meta.aid, meta.cid)

            # 高赞评论：观众经常在下面指出视频里的错误、补上更准的型号或数据。
            # 跟截图、字幕一样属于锦上添花，拿不到不影响出笔记。
            if self.settings.bili_top_comments > 0 and meta.aid is not None:
                meta.comments = await bili.get_top_comments(
                    meta.aid,
                    owner_mid=meta.owner_mid,
                    limit=self.settings.bili_top_comments,
                )

            page = meta.page
            cid = meta.cid or 0

            report(
                JobStage.RESOLVING,
                0.07,
                f"《{meta.display_title}》"
                + (f"（共 {meta.total_pages} P）" if meta.total_pages > 1 else "")
                + (f"（含 {len(meta.chapters)} 个章节）" if meta.chapters else "")
                + (f"（{len(meta.comments)} 条高赞评论）" if meta.comments else ""),
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

        # ---------------- 3.5 画面文字（OCR）----------------
        # 「不单纯分析音频」：教程类视频的提示词、代码、命令往往
        # **只出现在画面上**，音频里听不到或者听错。
        on_screen = await self._obtain_on_screen(
            meta,
            transcript,
            report=report,
            check_cancel=check_cancel,
            notices=notices,
        )

        check_cancel()

        # 无论转写来自缓存还是刚刚生成，都统一合并一次碎句再喂给模型。
        # 放在使用点而不是只放在保存点，是为了让改动前存下的旧缓存
        # 也能享受到同样的处理（merge_for_notes 是幂等的）。
        transcript = merge_for_notes(transcript, repeat_min_run=self.settings.whisper_repeat_min_run)

        # ---------------- 4. 生成笔记 ----------------
        report(JobStage.SUMMARIZING, 0.72, "正在加载本地模型…")
        provider = build_provider(self.settings)
        try:
            generator = NoteGenerator(provider, self.settings)
            note = await generator.generate(
                transcript,
                meta,
                on_screen=on_screen,
                on_progress=lambda frac, msg: (
                    check_cancel(),
                    report(
                        JobStage.SUMMARIZING, 0.72 + 0.24 * frac, msg
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
        note_id = reuse_or_new_note_id(self.db, meta.bvid, page)
        markdown = render_note_markdown(note, meta, transcript)

        path = self.vault.build_note_path(
            meta.bvid, note_title_for_filename(meta), page
        )
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
                transcript = merge_for_notes(
                    transcript, repeat_min_run=self.settings.whisper_repeat_min_run
                )
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

        transcript = merge_for_notes(transcript, repeat_min_run=self.settings.whisper_repeat_min_run)
        notices.append(f"🎧 已用本地 Whisper 转写完成（{transcript.note}）。")
        self.db.save_transcript(meta.bvid, page, cid, transcript)
        return transcript

    # ------------------------------------------------------------------
    # 画面文字（OCR）
    # ------------------------------------------------------------------

    def _vision_cache_path(self, bvid: str, page: int) -> Path:
        return self.settings.cache_dir / "vision" / f"{bvid}_p{page}.json"

    def _read_vision_cache(self, path: Path) -> list[tuple[float, str]] | None:
        """读画面文字缓存。缓存的有效性不设 TTL —— 视频内容不会变。"""
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, list):
            return None
        out: list[tuple[float, str]] = []
        for item in raw:
            if isinstance(item, list) and len(item) == 2:
                try:
                    out.append((float(item[0]), str(item[1])))
                except (TypeError, ValueError):
                    continue
        return out or None

    def _write_vision_cache(
        self, path: Path, blocks: list[tuple[float, str]]
    ) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(blocks, ensure_ascii=False), encoding="utf-8"
            )
        except OSError as exc:
            logger.debug("写画面文字缓存失败：%s", exc)

    async def _obtain_on_screen(
        self,
        meta: VideoMeta,
        transcript: Transcript,
        *,
        report: StageCb,
        check_cancel: Callable[[], None],
        notices: list[str],
    ) -> list[tuple[float, str]]:
        """下载低清视频 → 抽帧 → OCR，拿到画面上的文字。

        **失败一律降级为空列表**，绝不因为 OCR 出问题就让整篇笔记生不出来。
        """
        if not self.settings.vision_enabled:
            return []

        from app.vision.ocr import OcrEngine, is_available

        ok, reason = is_available()
        if not ok:
            notices.append(f"ℹ️ 已跳过画面识别：{reason}")
            return []

        cache_path = self._vision_cache_path(meta.bvid, meta.page)
        cached = self._read_vision_cache(cache_path)
        if cached is not None:
            notices.append(f"♻️ 复用了已缓存的画面文字（{len(cached)} 段）")
            return cached

        try:
            report(JobStage.VISION, 0.56, "正在下载低清视频（用于识别画面）…")
            downloader = AudioDownloader(self.settings)
            video = await downloader.download_video(
                url=meta.url,
                bvid=meta.bvid,
                page=meta.page,
                title=meta.part_title or meta.title,
                max_height=self.settings.vision_max_height,
                on_progress=lambda f, m: report(
                    JobStage.VISION, 0.56 + 0.04 * f, m
                ),
            )
            check_cancel()

            from app.vision.pipeline import extract_on_screen_text

            def on_prog(frac: float, msg: str) -> None:
                check_cancel()
                report(JobStage.VISION, 0.60 + 0.12 * frac, msg)

            work_dir = (
                self.settings.cache_dir / "vision" / f"{meta.bvid}_p{meta.page}"
            )
            blocks = await asyncio.to_thread(
                extract_on_screen_text,
                video,
                work_dir=work_dir,
                transcript_text=transcript_to_text(transcript.segments),
                interval=self.settings.vision_interval,
                max_frames=self.settings.vision_max_frames,
                engine=OcrEngine(threads=self.settings.vision_ocr_threads),
                on_progress=on_prog,
            )
        except Exception as exc:  # noqa: BLE001 - 画面识别是增强项
            logger.warning("画面识别失败（不影响笔记生成）：%s", exc)
            notices.append(f"⚠️ 画面识别失败，本次只依据音频内容：{exc}")
            return []

        blocks = [b for b in blocks if b.lines]
        total_lines = sum(len(b.lines) for b in blocks)
        if total_lines < self.settings.vision_min_lines:
            notices.append(
                f"ℹ️ 画面上文字很少（{total_lines} 行），未纳入笔记"
            )
            return []

        result = [(b.timestamp, b.render()) for b in blocks]
        notices.append(
            f"👁️ 从画面识别出 {total_lines} 行文字（{len(result)} 段）"
        )
        self._write_vision_cache(cache_path, result)
        return result

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
