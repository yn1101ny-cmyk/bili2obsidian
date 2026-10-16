"""ffmpeg 定位。

本项目的**主路径刻意不需要 ffmpeg**：
直接下载 B站的 m4a 音频流，faster-whisper 用 PyAV 解码，全程不调用外部二进制。

但为了应对「下载到的容器格式特殊」这类边角情况，这里用 ``imageio-ffmpeg``
（纯 pip 安装，自带静态 ffmpeg，无需管理员权限）提供一个可选的 ffmpeg。
"""

from __future__ import annotations

import logging
import shutil
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

#: 项目内的工具目录（已 gitignore）
TOOLS_BIN = Path(__file__).resolve().parent.parent.parent / "tools" / "bin"


@lru_cache(maxsize=1)
def find_ffmpeg() -> str | None:
    """返回可用的 ffmpeg 可执行文件路径；找不到返回 None。

    查找顺序：项目 tools/bin -> 系统 PATH -> imageio-ffmpeg 自带。
    """
    # 1. 项目内
    local = TOOLS_BIN / ("ffmpeg.exe" if _is_windows() else "ffmpeg")
    if local.exists():
        return str(local)

    # 2. 系统 PATH
    system = shutil.which("ffmpeg")
    if system:
        return system

    # 3. imageio-ffmpeg 自带的静态版本
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).exists():
            logger.debug("使用 imageio-ffmpeg 自带的 ffmpeg：%s", exe)
            return exe
    except Exception as exc:  # noqa: BLE001 - 缺 ffmpeg 不致命
        logger.debug("imageio-ffmpeg 不可用：%s", exc)

    return None


def ffmpeg_dir() -> str | None:
    """返回 yt-dlp ``ffmpeg_location`` 可用的目录。"""
    exe = find_ffmpeg()
    return str(Path(exe).parent) if exe else None


def _is_windows() -> bool:
    import sys

    return sys.platform == "win32"
