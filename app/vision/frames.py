"""关键帧抽取。

## 为什么不用 ``fps=1/N``

调研实测发现一个**很阴险**的坑：``-vf fps=1/15`` 抽出的帧，
真实时间戳是 ``t_i = (i - 0.5) * N``，**不是** ``(i-1) * N``。
对 15 秒间隔就是每条笔记的时间标签错 **7.5 秒**。

更坑的是 ``fps=1/15,showinfo`` 会打印 ``[0.0, 15.0, 30.0, 45.0]`` ——
看着完全正确，但全是假的，因为 fps 滤镜会重写 PTS。

所以这里改用**逐点定位**：``-ss <t> -i <video> -frames:v 1``。
每个时间点单独取一帧，时间戳就是我们自己传进去的那个值，**不存在推算误差**。
（稀疏采样时这反而比单遍解码更快，因为不用解码整段视频。）

另外 ``imageio-ffmpeg`` 只带 ffmpeg 不带 ffprobe，
所以视频时长靠解析 ``ffmpeg -i`` 的 stderr 拿到。
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.errors import MyNoteError
from app.media.ffmpeg import find_ffmpeg

logger = logging.getLogger(__name__)

#: 抽帧时缩放到这个宽度 —— 够 OCR 用，又能大幅省时间和磁盘
DEFAULT_WIDTH = 960

#: jpeg 质量（2 最好、31 最差）
DEFAULT_QUALITY = 3

#: ffmpeg 的启动超时（秒）
_FFMPEG_TIMEOUT = 60.0

#: 从 ``ffmpeg -i`` 输出里抠时长，形如 ``Duration: 00:24:55.12``
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)")


class FFmpegMissing(MyNoteError):
    """找不到可用的 ffmpeg。"""


@dataclass(frozen=True, slots=True)
class Frame:
    """一帧画面。``timestamp`` 是它**在视频里的真实秒数**。"""

    timestamp: float
    path: Path

    def clock(self) -> str:
        from app.schemas import format_clock

        return format_clock(self.timestamp)


def ffmpeg_path() -> str:
    exe = find_ffmpeg()
    if not exe:
        raise FFmpegMissing(
            "找不到 ffmpeg，无法抽帧。请执行：pip install imageio-ffmpeg"
        )
    return exe


def probe_duration(video: str | Path) -> float:
    """读出视频时长（秒）。拿不到返回 0。

    用 ``ffmpeg -i``（不带输出文件）让它把信息打到 stderr，
    再正则解析 —— 因为 imageio-ffmpeg 不提供 ffprobe。
    """
    exe = ffmpeg_path()
    try:
        proc = subprocess.run(
            [exe, "-hide_banner", "-i", str(video)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_FFMPEG_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("探测视频时长失败：%s", exc)
        return 0.0

    match = _DURATION_RE.search(proc.stderr or "")
    if not match:
        return 0.0
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def plan_timestamps(
    duration: float,
    *,
    interval: float = 15.0,
    max_frames: int = 240,
) -> list[float]:
    """规划要抽哪些时间点。

    长视频按 ``max_frames`` 自动放宽间隔，避免一小时的视频抽出几百帧。
    """
    if duration <= 0:
        return [0.0]

    interval = max(1.0, interval)
    count = int(duration // interval) + 1
    if count > max_frames:
        interval = duration / max_frames
        count = max_frames

    stamps = [round(i * interval, 3) for i in range(count)]
    return [t for t in stamps if t < duration]


def extract_frame(
    video: str | Path,
    timestamp: float,
    target: Path,
    *,
    width: int = DEFAULT_WIDTH,
    quality: int = DEFAULT_QUALITY,
) -> bool:
    """抽取指定时间点的**一帧**。成功返回 True。

    ``-ss`` 放在 ``-i`` **之前**（快速定位），``-frames:v 1`` 保证只出一帧。
    """
    exe = ffmpeg_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            [
                exe, "-y", "-hide_banner", "-loglevel", "error",
                "-ss", f"{timestamp:.3f}",
                "-i", str(video),
                "-frames:v", "1",
                "-vf", f"scale={width}:-2",
                "-q:v", str(quality),
                str(target),
            ],
            capture_output=True,
            timeout=_FFMPEG_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("抽帧失败 t=%.1f：%s", timestamp, exc)
        return False

    if proc.returncode != 0 or not target.exists() or target.stat().st_size == 0:
        logger.debug(
            "抽帧无输出 t=%.1f：%s",
            timestamp,
            (proc.stderr or b"")[:200],
        )
        return False
    return True


def extract_frames(
    video: str | Path,
    *,
    interval: float = 15.0,
    max_frames: int = 240,
    width: int = DEFAULT_WIDTH,
    work_dir: Path,
    on_progress=None,
) -> list[Frame]:
    """按固定间隔抽帧，返回带**准确时间戳**的帧列表。"""
    video_path = Path(video)
    if not video_path.exists():
        raise MyNoteError(f"视频文件不存在：{video_path}")

    duration = probe_duration(video_path)
    if duration <= 0:
        logger.warning("读不出视频时长，退化为只抽第 0 秒一帧")

    stamps = plan_timestamps(duration, interval=interval, max_frames=max_frames)
    work_dir.mkdir(parents=True, exist_ok=True)

    frames: list[Frame] = []
    for i, stamp in enumerate(stamps):
        target = work_dir / f"f_{i:05d}_{int(stamp * 1000):09d}.jpg"
        if extract_frame(video_path, stamp, target, width=width):
            frames.append(Frame(timestamp=stamp, path=target))
        if on_progress and stamps:
            on_progress((i + 1) / len(stamps), f"抽帧 {i + 1}/{len(stamps)}")

    logger.info("抽帧完成：%d/%d 帧（间隔 %.0fs）", len(frames), len(stamps), interval)
    return frames
