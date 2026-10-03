"""日志配置。

两个要点：

1. **编码**：Windows 控制台默认 GBK，日志里出现中文或 ✅ 会直接
   抛 ``UnicodeEncodeError`` 把进程搞挂，所以先把输出流切到 UTF-8。
2. **落盘**：日志写一份到 ``data/logs/mynote.log``，出问题时能回溯。
   日志里**不会出现密钥**（配置类只输出 ``redacted()`` 的结果）。
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from app.config import Settings
from app.console import enable_utf8_output

_FORMAT = "%(asctime)s  %(levelname)-7s %(name)-28s %(message)s"
_DATEFMT = "%H:%M:%S"

#: 这些库话太多，压一压
_NOISY = {
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    "urllib3": logging.WARNING,
    "faster_whisper": logging.WARNING,
    "huggingface_hub": logging.WARNING,
    "filelock": logging.WARNING,
    "asyncio": logging.WARNING,
}


def setup_logging(settings: Settings, *, verbose: bool = False) -> None:
    """配置根日志器（幂等）。"""
    enable_utf8_output()

    level = logging.DEBUG if verbose else getattr(
        logging, settings.log_level.upper(), logging.INFO
    )

    root = logging.getLogger()
    root.setLevel(level)

    # 清掉重复的 handler，避免重复输出（pytest / 多次调用时会遇到）
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    console = logging.StreamHandler(stream=sys.stderr)
    console.setFormatter(formatter)
    root.addHandler(console)

    try:
        log_dir = settings.log_dir
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / "mynote.log",
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError as exc:  # 日志写不了不该让程序起不来
        logging.getLogger(__name__).warning("无法写入日志文件：%s", exc)

    for name, noisy_level in _NOISY.items():
        logging.getLogger(name).setLevel(noisy_level)
