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

from app.bili.client import BiliClient, mine_view_payload
from app.bili.subtitles import SubtitleTrack
from app.bili.urls import VideoRef
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


# ---------------------------------------------------------------------------
# view 接口被 -412 风控时的兜底：改从视频页 HTML 里挖 videoData
#
# 背景（回归用例）：`x/web-interface/view` 是所有链路的第一个请求，它一吃 -412
# 整个流程就断了；而视频页（给人看的）风控宽松得多，页面里内联的
# `window.__INITIAL_STATE__` 恰好带着 aid / cid / 分P，够字幕与章节接口用。
# ---------------------------------------------------------------------------

VIEW_URL = "https://api.bilibili.com/x/web-interface/view"
PAGE_URL = "https://www.bilibili.com/video/BV1xx411c7mD"

#: 视频页里内联的播放状态，形状与 view 接口的 data 一致（只留够用的字段）
PAGE_HTML = (
    "<!DOCTYPE html><html><head><title>t</title></head><body><script>"
    "window.__INITIAL_STATE__="
    '{"videoData":{"bvid":"BV1xx411c7mD","aid":12345,"title":"测试视频",'
    '"desc":"说明","duration":600,"pubdate":1700000000,'
    '"pic":"//i0.hdslb.com/cover.jpg","owner":{"name":"某UP","mid":7},'
    '"pages":[{"cid":111,"page":1,"part":"第一P","duration":300},'
    '{"cid":222,"page":2,"part":"第二P","duration":300}]}}'
    "</script></body></html>"
)


def _ref(*, bvid: str | None = "BV1xx411c7mD", aid: int | None = None, page: int = 1) -> VideoRef:
    return VideoRef(
        raw=bvid or f"av{aid}",
        kind="bvid" if bvid else "aid",
        bvid=bvid,
        aid=aid,
        page=page,
    )


def test_mine_view_payload_variants() -> None:
    """挖掘函数要能吃下三种页面：正常、含裸 undefined、完全没有内联状态。"""
    payload = mine_view_payload(PAGE_HTML)
    assert payload is not None
    assert payload["aid"] == 12345
    assert [p["cid"] for p in payload["pages"]] == [111, 222]

    undefined_html = (
        '<script>window.__INITIAL_STATE__={"a":undefined,'
        '"videoData":{"aid":7,"pages":[{"cid":8}]}}</script>'
    )
    payload = mine_view_payload(undefined_html)
    assert payload is not None and payload["aid"] == 7

    # 内联状态被裁掉时退回正则：够字幕用（标题等信息留空）
    trimmed = '<script>var x={"videoData":{"aid":99,"cid":5,"bvid":"BV1yy4y1H7Zz"}}</script>'
    payload = mine_view_payload(trimmed)
    assert payload is not None and payload["aid"] == 99
    assert payload["pages"][0]["cid"] == 5

    assert mine_view_payload("<html>nope</html>") is None


@respx.mock
async def test_view_payload_uses_page_when_view_is_blocked(settings) -> None:
    """view 接口吃 -412 时必须回落到视频页，而不是把整条链路断掉。"""
    local = settings.model_copy(update={"bili_max_retries": 0})
    respx.get(VIEW_URL).mock(return_value=httpx.Response(412))
    page = respx.get(PAGE_URL).mock(return_value=httpx.Response(200, text=PAGE_HTML))

    async with BiliClient(local) as bili:
        meta = await bili.get_video_meta(_ref(page=2))

    assert page.called
    assert meta.bvid == "BV1xx411c7mD"
    assert meta.aid == 12345
    assert meta.title == "测试视频"
    assert meta.owner_name == "某UP"
    assert meta.cid == 222
    assert meta.part_title == "第二P"
    assert meta.page == 2
    assert meta.total_pages == 2


@respx.mock
async def test_view_payload_prefers_api_when_available(settings) -> None:
    """接口正常时不要去抓页面（多一次请求、也多一次被风控的机会）。"""
    respx.get(VIEW_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "bvid": "BV1xx411c7mD",
                    "aid": 12345,
                    "title": "接口来的",
                    "pages": [{"cid": 111, "page": 1, "part": "第一P", "duration": 300}],
                },
            },
        )
    )
    page = respx.get(PAGE_URL).mock(return_value=httpx.Response(200, text=PAGE_HTML))

    async with BiliClient(settings) as bili:
        meta = await bili.get_video_meta(_ref())

    assert not page.called
    assert meta.title == "接口来的"
    assert meta.cid == 111


@respx.mock
async def test_page_fallback_raises_when_page_has_no_data(settings) -> None:
    """页面也拿不到数据时要明确失败，不能返回半个元数据。"""
    local = settings.model_copy(update={"bili_max_retries": 0})
    respx.get(VIEW_URL).mock(return_value=httpx.Response(412))
    respx.get(PAGE_URL).mock(return_value=httpx.Response(200, text="<html>nothing</html>"))

    async with BiliClient(local) as bili:
        with pytest.raises(BiliApiError):
            await bili.get_video_meta(_ref())


@respx.mock
async def test_aid_only_ref_does_not_try_page(settings) -> None:
    """只有 aid 时没有视频页可抓，直接报错而不是去拼一个假 URL。"""
    local = settings.model_copy(update={"bili_max_retries": 0})
    route = respx.get(VIEW_URL).mock(return_value=httpx.Response(412))

    async with BiliClient(local) as bili:
        with pytest.raises(BiliApiError):
            await bili.get_video_meta(_ref(bvid=None, aid=999))

    assert route.call_count == 1


# ---------------------------------------------------------------------------
# 高赞评论（观众勘误）
# ---------------------------------------------------------------------------

REPLY_URL = "https://api.bilibili.com/x/v2/reply"


def _reply_payload(replies: list[dict]) -> dict:
    return {
        "code": 0,
        "message": "0",
        "data": {"replies": replies},
    }


def _reply(
    *,
    user: str = "某人",
    mid: int = 1,
    like: int = 0,
    message: str = "这条评论有点内容",
) -> dict:
    return {
        "member": {"uname": user, "mid": mid},
        "like": like,
        "content": {"message": message},
    }


@respx.mock
async def test_top_comments_sorted_filtered_and_marked(settings) -> None:
    """评论按点赞降序、去掉空/重复、能标出 UP 主本人。"""
    payload = _reply_payload(
        [
            _reply(user="甲", like=3, message="UP 说错了，型号是 MGSD 不是 RG\n配色也不一样"),
            _reply(user="乙", like=99, message="8:30 那句我确认过，是对的"),
            _reply(user="丙", like=50, message="[doge]"),  # 太短
            _reply(user="丁", like=50, message="8:30 那句我确认过，是对的"),  # 重复
            _reply(user="戊", like=40, message="省流总结看这里 https://pan.quark.cn/s/abc"),  # 引流
            _reply(user="UP", mid=42, like=7, message="补充一下：这个券只在周五可用"),
        ]
    )
    respx.get(REPLY_URL).mock(return_value=httpx.Response(200, json=payload))

    async with BiliClient(settings) as bili:
        comments = await bili.get_top_comments(123, owner_mid=42, limit=5)

    assert [c.like for c in comments] == [99, 7, 3]
    assert comments[0].text.startswith("8:30")
    assert comments[1].is_up is True
    assert comments[1].user == "UP"
    # 换行被压平成一行，方便塞进提示词与笔记
    assert "\n" not in comments[2].text
    assert comments[2].is_up is False


@respx.mock
async def test_top_comments_failure_is_not_fatal(settings) -> None:
    """评论接口报错（含风控）时返回空列表，不拖垮出笔记。"""
    local = settings.model_copy(update={"bili_max_retries": 0})
    respx.get(REPLY_URL).mock(
        return_value=httpx.Response(200, json={"code": -412, "message": "风控"})
    )

    async with BiliClient(local) as bili:
        assert await bili.get_top_comments(123, limit=5) == []


async def test_top_comments_disabled_does_not_request(settings) -> None:
    """BILI_TOP_COMMENTS=0 时不该发请求。"""
    async with BiliClient(settings) as bili:
        assert await bili.get_top_comments(123, limit=0) == []
