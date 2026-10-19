"""音频下载（yt-dlp）。

**出网范围**：只访问 ``*.bilibili.com`` / ``*.bilivideo.com`` 等 B站 CDN。

设计要点：

* **只要音频流，不做转码**：选 ``ba[ext=m4a]`` 直接落盘，
  下游 faster-whisper 用 PyAV 解码，因此**主路径完全不需要 ffmpeg**。
* **登录态用请求头传递**：SESSDATA 通过 ``http_headers`` 传给 yt-dlp，
  不落 cookie 文件，避免敏感值留在磁盘上。
* **进度回传**：yt-dlp 的 progress hook 在工作线程里跑，
  用 ``call_soon_threadsafe`` 安全地把进度送回事件循环。
* **yt-dlp 是同步阻塞的**：放到线程里执行，不卡住 Web 服务。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from pathlib import Path

from app.config import Settings
from app.errors import DownloadError
from app.media.ffmpeg import ffmpeg_dir

logger = logging.getLogger(__name__)

#: 优先纯音频流；不指定容器，让 yt-dlp 选最优音频
_FORMAT_SELECTOR = "ba[abr<=64000]/ba[abr<=132000]/ba[ext=m4a]/ba/b"

#: 文件名里不允许出现的字符
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def video_format_selector(max_height: int) -> str:
    """挑「只含视频轨」的格式，分辨率压到 ``max_height`` 以内。

    竖屏视频（B站很常见）高宽是反的：1080×1920 的片源 ``height`` 是 1920，
    用 ``height<=480`` 一个都匹配不上，yt-dlp 直接报
    ``No video formats found!`` —— 抽帧 OCR 就这么悄悄失败了。
    所以先按 height 找，再退回按 width 找，最后才退到完整文件。
    """
    h = max(1, int(max_height))
    return (
        f"bv*[height<={h}][ext=mp4]/"
        f"bv*[height<={h}]/"
        f"bv*[width<={h}]/"
        f"b[height<={h}]/"
        f"b"
    )


ProgressCb = Callable[[float, str], None] | None


def _safe_name(text: str, *, limit: int = 60) -> str:
    """把标题清理成安全的文件名片段。"""
    cleaned = _UNSAFE.sub("_", (text or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:limit] or "audio"


class AudioDownloader:
    """把 B站视频的音频下载到本地 ``data/media``。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _out_base(self, bvid: str, page: int, title: str = "") -> Path:
        self.settings.media_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{bvid}_p{page}"
        if title:
            stem += f"_{_safe_name(title)}"
        return self.settings.media_dir / stem

    def _build_opts(
        self,
        out_base: Path,
        *,
        progress_hook,
        on_log,
    ) -> dict:
        """构造 yt-dlp 选项。"""
        headers = {
            "User-Agent": self.settings.bili_user_agent,
            "Referer": "https://www.bilibili.com/",
        }
        if self.settings.has_bili_login:
            # 只放进内存里的请求头，不写 cookie 文件
            headers["Cookie"] = f"SESSDATA={self.settings.bili_sessdata}"

        opts: dict = {
            "format": _FORMAT_SELECTOR,
            "outtmpl": str(out_base) + ".%(ext)s",
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "noplaylist": True,          # 只下当前分P，合集由上层拆开处理
            "http_headers": headers,
            "retries": 5,
            "fragment_retries": 5,
            "socket_timeout": 30,
            "progress_hooks": [progress_hook],
            "logger": on_log,
            # 不写任何可能残留登录态的文件
            "cookiefile": None,
            "cachedir": str(self.settings.cache_dir / "ytdlp"),
        }

        location = ffmpeg_dir()
        if location:
            # 只有需要合并/转码时才用得上，主路径其实不触发
            opts["ffmpeg_location"] = location
        return opts

    async def download(
        self,
        *,
        url: str,
        bvid: str,
        page: int = 1,
        title: str = "",
        on_progress: ProgressCb = None,
        force: bool = False,
    ) -> Path:
        """下载音频，返回本地文件路径。

        已存在且非空则直接复用（除非 ``force=True``）。
        """
        out_base = self._out_base(bvid, page, title)

        if not force:
            existing = self._find_existing(out_base)
            if existing is not None:
                logger.info("复用已下载的音频：%s", existing.name)
                if on_progress:
                    on_progress(1.0, "复用已下载的音频")
                return existing

        loop = asyncio.get_running_loop()

        def progress_hook(d: dict) -> None:
            if on_progress is None:
                return
            status = d.get("status")
            if status == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                done = d.get("downloaded_bytes") or 0
                frac = (done / total) if total else 0.0
                speed = d.get("speed") or 0
                speed_text = f"{speed / 1024 / 1024:.1f} MB/s" if speed else ""
                loop.call_soon_threadsafe(
                    on_progress,
                    min(0.99, frac),
                    f"下载音频中 {frac * 100:.0f}% {speed_text}".strip(),
                )
            elif status == "finished":
                loop.call_soon_threadsafe(on_progress, 0.99, "音频下载完成")

        class _Logger:
            """把 yt-dlp 的日志收进 logging，避免它直接打印到 stdout。"""

            def debug(self, msg: str) -> None:
                if not msg.startswith("[debug]"):
                    logger.debug("yt-dlp: %s", msg)

            def info(self, msg: str) -> None:
                logger.debug("yt-dlp: %s", msg)

            def warning(self, msg: str) -> None:
                logger.warning("yt-dlp: %s", msg)

            def error(self, msg: str) -> None:
                logger.error("yt-dlp: %s", msg)

        opts = self._build_opts(
            out_base, progress_hook=progress_hook, on_log=_Logger()
        )

        def _run() -> Path:
            try:
                import yt_dlp
            except ImportError as exc:
                raise DownloadError(
                    "未安装 yt-dlp。请执行：pip install yt-dlp"
                ) from exc

            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([url])
            except Exception as exc:  # noqa: BLE001 - yt-dlp 抛的异常类型很杂
                raise DownloadError(
                    f"下载音频失败：{exc}\n"
                    "常见原因：视频需要登录（配置 BILI_SESSDATA 后重试）、"
                    "视频已下架、或网络受限。"
                ) from exc

            found = self._find_existing(out_base)
            if found is None:
                raise DownloadError(
                    "yt-dlp 执行完成但没有找到音频文件。"
                    "可能是该视频没有可用的纯音频流。"
                )
            return found

        logger.info("开始下载音频：%s (P%d)", bvid, page)
        result = await asyncio.to_thread(_run)
        size_mb = result.stat().st_size / 1024 / 1024
        logger.info("音频已保存：%s（%.1f MB）", result.name, size_mb)
        if on_progress:
            on_progress(1.0, f"音频就绪（{size_mb:.1f} MB）")
        return result

    # ------------------------------------------------------------------
    async def download_video(
        self,
        *,
        url: str,
        bvid: str,
        page: int = 1,
        title: str = "",
        max_height: int = 480,
        on_progress: ProgressCb = None,
        force: bool = False,
    ) -> Path:
        """下载**低清**视频，只为了抽帧做画面识别。

        ⚠️ 几个实测要点：

        * **必须带浏览器指纹**：不带 ``impersonate`` 会被 B站 412 拒绝。
          装了 ``curl-cffi`` 并指定 impersonate 之后，
          **≤480p 不需要登录 cookie**就能下 —— 所以这一路
          不需要用户交出 B站登录态。
        * **只下低清**：抽帧做 OCR 用 360p/480p 足够了。
          实测 20 分钟视频 @360p 约 31~51MB 视频 + 10MB 音频。
        * 抽帧只需要**视频轨**，音频轨已经在转写阶段下过了，这里 `+ba` 不要。
        """
        out_base = self._out_base(bvid, page, title or "video")
        out_base = out_base.with_name(out_base.name + "_video")

        if not force:
            existing = self._find_existing(out_base, video_only=True)
            if existing is not None:
                logger.info("复用已下载的视频：%s", existing.name)
                if on_progress:
                    on_progress(1.0, "复用已下载的视频")
                return existing

        loop = asyncio.get_running_loop()

        def progress_hook(d: dict) -> None:
            if on_progress is None:
                return
            status = d.get("status")
            if status == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                done = d.get("downloaded_bytes") or 0
                frac = (done / total) if total else 0.0
                loop.call_soon_threadsafe(
                    on_progress,
                    min(0.99, frac),
                    f"下载视频中 {frac * 100:.0f}%",
                )
            elif status == "finished":
                loop.call_soon_threadsafe(on_progress, 0.99, "视频下载完成")

        def _run() -> Path:
            try:
                import yt_dlp
            except ImportError as exc:
                raise DownloadError("未安装 yt-dlp") from exc

            headers = {
                "User-Agent": self.settings.bili_user_agent,
                "Referer": "https://www.bilibili.com/",
            }
            if self.settings.has_bili_login:
                headers["Cookie"] = f"SESSDATA={self.settings.bili_sessdata}"

            base_opts: dict = {
                # 只要视频轨；分辨率压到 max_height 以内。
                # 抽帧做 OCR 用 360p/480p 足够 —— 实测 20 分钟 @360p 约 40~60MB。
                "format": video_format_selector(max_height),
                "outtmpl": str(out_base) + ".%(ext)s",
                "quiet": True,
                "no_warnings": True,
                "noprogress": True,
                "noplaylist": True,
                "http_headers": headers,
                "retries": 3,
                "socket_timeout": 30,
                "progress_hooks": [progress_hook],
                "cookiefile": None,
                "cachedir": str(self.settings.cache_dir / "ytdlp"),
            }

            location = ffmpeg_dir()
            if location:
                base_opts["ffmpeg_location"] = location

            # ⚠️ B站 的下载接口很看心情，同一视频、同样选项实测：
            #
            #   默认指纹                              → ✓ 拿到 15 个格式
            #   显式 ImpersonateTarget("chrome")      → 时好时坏：有时 15 个格式，
            #                                           有时直接空格式表 →
            #                                           No video formats found!
            #   显式 ImpersonateTarget("chrome","136") → ✗ 必失败
            #
            # 「空格式表」是**间歇性**风控，不是选项永久不兼容 —— 隔几秒再来
            # 一次往往就能过。所以这里不赌单一路径：先按默认来，失败了退回
            # 显式指纹；两路都失败也不是终点，整轮再来一次。
            attempts: list[tuple[str, object | None]] = [("默认", None)]
            try:
                from yt_dlp.networking.impersonate import ImpersonateTarget

                attempts.append(
                    ("显式浏览器指纹", ImpersonateTarget("chrome"))
                )
            except Exception as exc:  # noqa: BLE001 - 老版本没有这个 API
                logger.debug("yt-dlp 不支持 impersonate：%s", exc)

            last_error: Exception | None = None
            for round_no in range(1, 3):
                for label, target in attempts:
                    opts = dict(base_opts)
                    if target is not None:
                        opts["impersonate"] = target
                    try:
                        with yt_dlp.YoutubeDL(opts) as ydl:
                            ydl.download([url])
                    except Exception as exc:  # noqa: BLE001
                        last_error = exc
                        logger.warning(
                            "下载视频失败（第 %d 轮 · %s）：%s", round_no, label, exc
                        )
                        continue

                    found = self._find_existing(out_base, video_only=True)
                    if found is not None:
                        if label != attempts[0][0]:
                            logger.info("改用「%s」后才下载成功", label)
                        return found
                    last_error = DownloadError("yt-dlp 报告完成但没有生成视频文件")

                if round_no < 2:
                    logger.info("视频整轮下载失败，2 秒后重试")
                    time.sleep(2)

            raise DownloadError(
                f"下载视频失败：{last_error}\n"
                "常见原因：yt-dlp 版本过旧，或缺少 curl-cffi。" 
                "请执行：pip install -U yt-dlp curl-cffi"
            ) from last_error

        logger.info("开始下载视频（≤%dp）：%s (P%d)", max_height, bvid, page)
        result = await asyncio.to_thread(_run)
        size_mb = result.stat().st_size / 1024 / 1024
        logger.info("视频已保存：%s（%.1f MB）", result.name, size_mb)
        if on_progress:
            on_progress(1.0, f"视频就绪（{size_mb:.1f} MB）")
        return result

    # ------------------------------------------------------------------
    @staticmethod
    def _find_existing(out_base: Path, *, video_only: bool = False) -> Path | None:
        """找出已下载的文件（后缀由 yt-dlp 决定）。"""
        parent = out_base.parent
        if not parent.exists():
            return None
        candidates = [
            p
            for p in parent.glob(out_base.name + ".*")
            if p.is_file()
            and p.suffix.lower() not in (".part", ".ytdl", ".temp")
            and p.stat().st_size > 0
        ]
        if not candidates:
            return None
        if video_only:
            priority = {".mp4": 0, ".mkv": 1, ".webm": 2, ".flv": 3}
        else:
            priority = {".m4a": 0, ".webm": 1, ".mp3": 2, ".aac": 3, ".opus": 4}
        candidates.sort(key=lambda p: priority.get(p.suffix.lower(), 9))
        return candidates[0]

    async def probe_formats(self, url: str) -> tuple[bool, str]:
        """自检用：确认 yt-dlp 能识别这个视频（不下载）。"""
        def _run() -> tuple[bool, str]:
            try:
                import yt_dlp
            except ImportError:
                return False, "未安装 yt-dlp"
            headers = {
                "User-Agent": self.settings.bili_user_agent,
                "Referer": "https://www.bilibili.com/",
            }
            if self.settings.has_bili_login:
                headers["Cookie"] = f"SESSDATA={self.settings.bili_sessdata}"
            try:
                with yt_dlp.YoutubeDL(
                    {"quiet": True, "no_warnings": True, "http_headers": headers}
                ) as ydl:
                    info = ydl.extract_info(url, download=False)
            except Exception as exc:  # noqa: BLE001
                return False, f"yt-dlp 解析失败：{exc}"
            if not isinstance(info, dict):
                return False, "yt-dlp 未返回有效信息"
            return True, f"yt-dlp 可用，识别到标题：{info.get('title', '未知')}"

        return await asyncio.to_thread(_run)
