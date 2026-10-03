"""控制台输出编码处理。

Windows 控制台默认是 GBK(cp936)：

* 打印中文会乱码；
* 打印 ✅ / ❌ / ⚠️ 这类字符会直接抛 ``UnicodeEncodeError`` 崩掉脚本。

任何命令行入口都应该先调用 :func:`enable_utf8_output`。
（Web 界面走 HTTP 不受影响，但服务端日志仍会经过控制台，所以也建议调用。）
"""

from __future__ import annotations

import sys


def enable_utf8_output() -> None:
    """把 stdout / stderr 切到 UTF-8，并对无法编码的字符降级替换。

    幂等，且在任何失败情况下安静跳过 —— 编码问题不该导致程序挂掉。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            # 被重定向到非常规对象时可能不支持 reconfigure，忽略即可
            pass


__all__ = ["enable_utf8_output"]
