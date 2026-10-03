"""端到端冒烟：链接 → 元数据 → 音频 → 本地转写。

**不调用大模型**，所以不需要 Ollama 就能验证整条采集与转写链路。
（笔记生成那一段由 tests/test_notes.py 用假模型覆盖，真实质量用真实视频单独评估。）

用法::

    .\\.venv\\Scripts\\python.exe -m scripts.smoke_transcribe BV1GJ411x7h7
    .\\.venv\\Scripts\\python.exe -m scripts.smoke_transcribe <链接> --save
    .\\.venv\\Scripts\\python\\python.exe -m scripts.smoke_transcribe <链接> --force

``--save`` 会把转写全文写到 ``data/notes/transcripts/`` 便于肉眼核对。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.asr.whisper_engine import WhisperEngine  # noqa: E402
from app.bili.client import BiliClient  # noqa: E402
from app.bili.subtitles import merge_adjacent_segments  # noqa: E402
from app.bili.urls import parse_video_input  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.console import enable_utf8_output  # noqa: E402
from app.media.downloader import AudioDownloader  # noqa: E402
from app.notes.chunking import transcript_to_text  # noqa: E402
from app.schemas import Transcript, VideoMeta, format_clock  # noqa: E402


def format_report(meta: VideoMeta, transcript: Transcript, elapsed: float) -> str:
    """把结果渲染成人看的报告（纯函数，便于测试）。"""
    segs = transcript.segments
    lines = [
        "=" * 70,
        "  转写冒烟测试结果",
        "=" * 70,
        "",
        f"  标题      {meta.display_title}",
        f"  UP主      {meta.owner_name or '未知'}",
        f"  时长      {format_clock(meta.duration)}",
        f"  链接      {meta.url}",
        f"  分P       {meta.page}/{meta.total_pages}",
        "",
        "-" * 70,
        f"  转写来源   {transcript.source.label}（可信度 {transcript.source.confidence}）",
        f"  片段数     {len(segs)}",
        f"  字符数     {transcript.char_count}",
        f"  耗时       {elapsed:.1f}s",
        f"  诊断       {transcript.note}",
        "-" * 70,
        "",
        "  转写预览（前 25 条）：",
        "",
    ]
    for seg in segs[:25]:
        lines.append(f"    [{format_clock(seg.start)}] {seg.text}")
    if len(segs) > 25:
        lines.append(f"    …… 还有 {len(segs) - 25} 条")
    lines.append("")
    return "\n".join(lines)


async def run(url: str, *, force: bool, save: bool) -> int:
    settings = get_settings()
    settings.ensure_dirs()

    # ---- 1. 解析链接 ----
    print(f"→ 解析链接：{url}")
    ref = parse_video_input(url)

    async with BiliClient(settings) as client:
        if ref.needs_resolution:
            print("→ 展开短链…")
            ref = parse_video_input(await client.resolve_short_url(ref.bvid or ""))

        # ---- 2. 元数据 ----
        print("→ 获取视频信息…")
        meta = await client.get_video_meta(ref)
        print(f"  《{meta.display_title}》  {format_clock(meta.duration)}  "
              f"UP: {meta.owner_name}  分P: {meta.page}/{meta.total_pages}")

        # ---- 3. 优先尝试字幕（未登录一般拿不到，会落到转写） ----
        transcript: Transcript | None = None
        if meta.aid is not None and meta.cid:
            print("→ 查找 B站字幕…")
            transcript = await client.try_get_subtitle(meta.aid, meta.cid)
        if transcript is not None:
            print(f"  ✅ 直接拿到字幕（{transcript.source.label}），跳过语音转写")
        else:
            print("  ℹ️  没有可用字幕（未登录时属正常），改用本地转写")

    # ---- 4. 本地 Whisper 转写 ----
    if transcript is None:
        downloader = AudioDownloader(settings)
        print("→ 下载音频…")
        t0 = time.time()
        audio = await downloader.download(
            url=meta.url,
            bvid=meta.bvid,
            page=meta.page,
            title=meta.part_title or meta.title,
            force=force,
            on_progress=lambda f, m: None,  # 后台跑，不打进度刷屏
        )
        size_mb = audio.stat().st_size / 1024 / 1024
        print(f"  ✅ {audio.name}（{size_mb:.1f} MB，{time.time() - t0:.1f}s）")

        print("→ 加载模型并转写（首次会下载约 1.6GB 权重，请耐心）…")
        engine = WhisperEngine(settings)
        try:
            t0 = time.time()
            transcript = await asyncio.to_thread(engine.transcribe, audio)
            elapsed = time.time() - t0
        finally:
            engine.unload()

        # 合并碎句，让预览更接近最终喂给模型的样子
        merged = merge_adjacent_segments(transcript.segments)
        transcript = transcript.model_copy(update={"segments": merged})
    else:
        elapsed = 0.0

    # ---- 5. 输出 ----
    print()
    print(format_report(meta, transcript, elapsed))

    if save:
        settings.ensure_dirs()
        out_dir = settings.notes_dir / "transcripts"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{meta.bvid}_p{meta.page}.txt"
        path.write_text(
            transcript_to_text(transcript.segments, with_timestamps=True),
            encoding="utf-8",
        )
        print(f"  转写全文已保存：{path}")
        print()

    return 0


def main() -> int:
    enable_utf8_output()
    parser = argparse.ArgumentParser(
        prog="smoke_transcribe",
        description="验证「链接 → 音频 → 本地转写」链路（不调用大模型）",
    )
    parser.add_argument("url", help="B站链接 / BV号 / av号 / 短链")
    parser.add_argument("--force", action="store_true", help="重新下载音频，不用缓存")
    parser.add_argument("--save", action="store_true", help="保存转写全文")
    args = parser.parse_args()

    try:
        return asyncio.run(run(args.url, force=args.force, save=args.save))
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130
    except Exception as exc:  # noqa: BLE001 - 冒烟脚本要把错误完整展示出来
        print(f"\n❌ 失败：{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
