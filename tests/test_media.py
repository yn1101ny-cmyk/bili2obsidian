"""下载器的格式选择与重试。

这几条是踩坑之后补的：

* 竖屏视频没有 ``height<=480`` 的格式，只按 height 匹配会直接
  ``No video formats found!``；
* B站 会间歇性返回空格式表，一次失败不代表真失败 —— 原来两路都失败
  就静默降级，「画面识别」那一路的文字就丢了。
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

from app.errors import DownloadError
from app.media.downloader import AudioDownloader, video_format_selector


def test_video_selector_falls_back_to_width_for_portrait():
    """⛔ 回归守卫：竖屏（高 > 宽）的视频必须能按 width 命中。"""
    sel = video_format_selector(480)
    assert "bv*[height<=480]" in sel
    assert "bv*[width<=480]" in sel
    # height 优先，width 兜底，最后才退到完整文件
    assert sel.index("bv*[height<=480]") < sel.index("bv*[width<=480]")
    assert sel.index("bv*[width<=480]") < sel.index("b[height<=480]")


def test_video_selector_clamps_nonsense_height():
    assert "bv*[height<=1]" in video_format_selector(0)
    assert "bv*[width<=1]" in video_format_selector(-5)


def test_video_download_retries_a_whole_round(settings, monkeypatch):
    """⛔ 回归守卫：一路失败要整轮重试，而不是立刻静默降级。"""
    calls: list[dict] = []

    class FakeYDL:
        def __init__(self, opts: dict) -> None:
            calls.append(opts)

        def __enter__(self) -> "FakeYDL":
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def download(self, urls: list[str]) -> None:
            raise RuntimeError("ERROR: [BiliBili] BV1xx: No video formats found!")

    fake = types.ModuleType("yt_dlp")
    fake.YoutubeDL = FakeYDL  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "yt_dlp", fake)
    monkeypatch.setattr("app.media.downloader.time.sleep", lambda _s: None)

    downloader = AudioDownloader(settings)
    with pytest.raises(DownloadError):
        asyncio.run(
            downloader.download_video(
                url="https://www.bilibili.com/video/BV1xx", bvid="BV1xx"
            )
        )

    # 假模块里没有 impersonate API，所以每轮只有「默认」一路：两轮 = 2 次
    assert len(calls) == 2
