"""``python -m app`` —— 启动本地 Web 服务。

服务只监听回环地址（``app/config.py`` 里硬校验），
启动后会打印可点击的地址。
"""

from __future__ import annotations

import argparse
import sys

from app.config import get_settings
from app.console import enable_utf8_output
from app.logging_setup import setup_logging


def main() -> int:
    enable_utf8_output()

    parser = argparse.ArgumentParser(
        prog="python -m app", description="启动 MyNoteApp 本地 Web 界面"
    )
    parser.add_argument("--port", type=int, help="覆盖端口（默认取 .env / 8765）")
    parser.add_argument("--host", help="覆盖监听地址（只允许回环地址）")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    parser.add_argument(
        "--reload", action="store_true", help="改动代码后自动重启（开发用）"
    )
    args = parser.parse_args()

    settings = get_settings()
    if args.port:
        settings.port = args.port
    if args.host:
        settings.host = args.host  # 非法值会在下一行的校验里被拒

    # host 校验放在这里再走一遍：命令行覆盖也要受限
    from app.config import LOOPBACK_HOSTS

    if settings.host not in LOOPBACK_HOSTS:
        print(
            f"❌ 出于数据安全，只能监听回环地址 {sorted(LOOPBACK_HOSTS)}，"
            f"收到 {settings.host!r}。",
            file=sys.stderr,
        )
        return 2

    setup_logging(settings, verbose=args.verbose)

    import uvicorn

    from app.web.server import create_app

    url = f"http://{settings.host}:{settings.port}"
    print()
    print("  📒 MyNoteApp")
    print(f"  ➜  {url}")
    print(f"  ➜  数据目录 {settings.data_dir}")
    print(f"  ➜  笔记模型 {settings.llm_provider} / {settings.ollama_model}")
    print("  ➜  Ctrl+C 停止")
    print()

    try:
        if args.reload:
            uvicorn.run(
                "app.web.server:create_app",
                factory=True,
                host=settings.host,
                port=settings.port,
                log_config=None,
                access_log=False,
                reload=True,
            )
        else:
            uvicorn.run(
                create_app(settings),
                host=settings.host,
                port=settings.port,
                log_config=None,
                access_log=False,
            )
    except OSError as exc:
        print(f"\n❌ 启动失败：{exc}", file=sys.stderr)
        print(
            f"   端口 {settings.port} 可能已被占用。"
            f"换一个：python -m app --port {settings.port + 1}",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print("\n已停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
