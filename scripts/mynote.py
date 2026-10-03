"""命令行入口：贴一个 B站链接，产出一篇笔记。

这是不依赖 Web 界面的最小可用形态，也是调参和排查问题时最好用的入口。

用法::

    .\\.venv\\Scripts\\python.exe -m scripts.mynote BV1GJ411x7h7
    .\\.venv\\Scripts\\python.exe -m scripts.mynote "<链接>" --model qwen3:8b
    .\\.venv\\Scripts\\python.exe -m scripts.mynote "<链接>" --force-transcribe

转写结果会缓存在本机，重复运行同一视频不会重新转写。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.console import enable_utf8_output  # noqa: E402
from app.pipeline import NotePipeline  # noqa: E402
from app.schemas import JobStage  # noqa: E402


def _print_stage(stage: JobStage, progress: float, message: str) -> None:
    bar_len = 24
    filled = int(bar_len * progress)
    bar = "█" * filled + "·" * (bar_len - filled)
    print(f"  [{bar}] {progress * 100:5.1f}%  {stage.label:<10} {message}", flush=True)


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    if args.model:
        settings.ollama_model = args.model
    if args.provider:
        settings.llm_provider = args.provider

    print(f"模型：{settings.llm_provider} / {settings.ollama_model}")
    print(f"数据目录：{settings.data_dir}")
    print()

    pipeline = NotePipeline(settings)
    try:
        result = await pipeline.run(
            args.url,
            on_stage=_print_stage,
            force_transcribe=args.force_transcribe,
            reuse_transcript=not args.no_cache,
        )
    finally:
        pipeline.unload_models()

    print()
    print("=" * 70)
    print(f"  《{result.note.title or result.meta.display_title}》")
    print("=" * 70)
    for notice in result.notices:
        print(f"  {notice}")
    print()
    print(f"  一句话总结：{result.note.one_liner}")
    print()
    print(f"  大纲 {len(result.note.outline)} 条 ｜ "
          f"要点 {len(result.note.key_points)} 条 ｜ "
          f"概念 {len(result.note.concepts)} 条 ｜ "
          f"补充 {len(result.note.supplements)} 条 ｜ "
          f"存疑 {len(result.note.uncertainties)} 条")
    print()
    print(f"  笔记已保存：{result.markdown_path}")
    print()

    if args.show:
        print("-" * 70)
        print(Path(result.markdown_path).read_text(encoding="utf-8"))
    return 0


def main() -> int:
    enable_utf8_output()
    parser = argparse.ArgumentParser(
        prog="mynote", description="把 B站视频变成一篇笔记"
    )
    parser.add_argument("url", help="B站链接 / BV号 / av号 / 短链")
    parser.add_argument("--model", help="覆盖本地模型名，如 qwen3:8b")
    parser.add_argument(
        "--provider",
        choices=["ollama", "openai", "none"],
        help="覆盖模型提供方",
    )
    parser.add_argument(
        "--force-transcribe", action="store_true", help="忽略转写缓存，重新转写"
    )
    parser.add_argument(
        "--no-cache", action="store_true", help="不使用已缓存的转写"
    )
    parser.add_argument(
        "--show", action="store_true", help="直接打印生成的 Markdown"
    )
    args = parser.parse_args()

    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI 要把错误完整展示出来
        print(f"\n❌ 失败：{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
