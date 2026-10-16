"""BiliClient 的字幕路径单测：全部用 respx 拦住，**不联网**。

背景（回归用例）：B站字幕存在 CDN 上，响应体是 ``{"body": [...]}``，
**没有 B站接口惯用的 ``code`` 字段**。早期 ``_get_json`` 对所有响应都做
``code == 0`` 校验，于是字幕下载必然抛 :class:`BiliApiError`，
``try_get_subtitle`` 再静默降级成 Whisper —— 即便配好了 SESSDATA 也永远拿不到官方字幕。
这里锁定修复后的行为，同时保证普通接口仍照旧校验业务码。
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.bili.client import BiliClient
from app.bili.subtitles import SubtitleTrack
from app.errors import BiliApiError, NoTranscriptAvailable
from app.schemas import TranscriptSource

PLAYER_URL = "https://api.bilibili.com/x/player/v2"
CDN_URL = "https://aisubtitle.hdslb.com/bfs/ai_subtitle/prod/1/2.json"

#: 真实的字幕 CDN 响应：没有 code / message，只有 font_* 与 body
CDN_PAYLOAD = {
    "font_size": 0.4,
    "font_color": "#FFFFFF",
    "background_alpha": 0.5,
    "body": [
        {"from": 0.5, "to": 2.0, "content": "大家好"},
        {"from": 2.0, "to": 4.5, "content": "今天聊聊 502 胶水"},
    ],
}

#: /x/player/v2 的真实响应形状：字幕轨道在 data.subtitle.subtitles
PLAYER_PAYLOAD = {
    "code": 0,
    "message": "0",
    "data": {
        "subtitle": {
            "allow_submit": False,
            "subtitles": [
                {
                    "id": 7,
                    "lan": "ai-zh",
                    "lan_doc": "中文（自动生成）",
                    "ai_type": 1,
                    "subtitle_url": "//aisubtitle.hdslb.com/bfs/ai_subtitle/prod/1/2.json",
                    "is_lock": False,
                }
            ],
        }
    },
}


def _track(url: str = CDN_URL) -> SubtitleTrack:
    return SubtitleTrack(
        lan="ai-zh", lan_doc="中文（自动生成）", url=url, is_ai=True, track_id=7
    )


@respx.mock
async def test_fetch_subtitle_parses_payload_without_code(settings) -> None:
    """字幕 CDN 响应没有 code 字段，必须能正常解析（曾经的 bug）。"""
    route = respx.get(CDN_URL).mock(return_value=httpx.Response(200, json=CDN_PAYLOAD))

    async with BiliClient(settings) as bili:
        segments = await bili.fetch_subtitle(_track())

    assert route.called
    assert [s.text for s in segments] == ["大家好", "今天聊聊 502 胶水"]
    assert segments[0].start == pytest.approx(0.5)
    assert segments[-1].end == pytest.approx(4.5)


@respx.mock
async def test_fetch_subtitle_raises_when_body_empty(settings) -> None:
    respx.get(CDN_URL).mock(return_value=httpx.Response(200, json={"body": []}))

    async with BiliClient(settings) as bili:
        with pytest.raises(NoTranscriptAvailable):
            await bili.fetch_subtitle(_track())


@respx.mock
async def test_api_call_still_rejects_nonzero_code(settings) -> None:
    """修复不能放宽普通接口：默认仍要求 code == 0。"""
    respx.get(PLAYER_URL).mock(
        return_value=httpx.Response(200, json={"code": -404, "message": "啥都木有"})
    )

    async with BiliClient(settings) as bili:
        with pytest.raises(BiliApiError):
            await bili.get_subtitle_tracks(aid=1, cid=2)


@respx.mock
async def test_try_get_subtitle_end_to_end(settings) -> None:
    """轨道接口 → 选轨 → CDN 下载，整条 A1 路径能出 Transcript。"""
    respx.get(PLAYER_URL).mock(return_value=httpx.Response(200, json=PLAYER_PAYLOAD))
    respx.get(CDN_URL).mock(return_value=httpx.Response(200, json=CDN_PAYLOAD))

    async with BiliClient(settings) as bili:
        transcript = await bili.try_get_subtitle(aid=1, cid=2)

    assert transcript is not None
    assert transcript.source is TranscriptSource.BILI_AI
    assert transcript.language == "ai-zh"
    assert transcript.char_count == len("大家好今天聊聊 502 胶水")


@respx.mock
async def test_try_get_subtitle_returns_none_when_track_download_fails(settings) -> None:
    """字幕下载失败要返回 None 触发 Whisper 兜底，不能把异常抛穿。"""
    respx.get(PLAYER_URL).mock(return_value=httpx.Response(200, json=PLAYER_PAYLOAD))
    respx.get(CDN_URL).mock(return_value=httpx.Response(403))

    async with BiliClient(settings) as bili:
        assert await bili.try_get_subtitle(aid=1, cid=2) is None
