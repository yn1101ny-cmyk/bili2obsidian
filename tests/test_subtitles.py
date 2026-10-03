"""字幕解析与片段合并测试。

B站接口结构时有微调，所以这里同时覆盖「结构正常」和「结构残缺」两种情况 ——
残缺时必须安静地返回空结果，让上层走 Whisper 兜底，而不是整条链路崩掉。
"""

from __future__ import annotations

import pytest

from app.bili.subtitles import (
    SubtitleTrack,
    merge_adjacent_segments,
    normalize_subtitle_url,
    parse_subtitle_body,
    parse_subtitle_tracks,
    pick_best_track,
)
from app.schemas import TranscriptSegment, TranscriptSource


# ---------------------------------------------------------------------------
# URL 规范化
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("//aisubtitle.hdslb.com/a.json", "https://aisubtitle.hdslb.com/a.json"),
        ("http://aisubtitle.hdslb.com/a.json", "https://aisubtitle.hdslb.com/a.json"),
        ("https://x.hdslb.com/a.json", "https://x.hdslb.com/a.json"),
        ("aisubtitle.hdslb.com/a.json", "https://aisubtitle.hdslb.com/a.json"),
        ("  //x/y.json  ", "https://x/y.json"),
        ("", ""),
    ],
)
def test_normalize_subtitle_url(raw, expected):
    assert normalize_subtitle_url(raw) == expected


def test_http_is_upgraded_to_https():
    """避免明文传输。"""
    assert normalize_subtitle_url("http://x/y").startswith("https://")


# ---------------------------------------------------------------------------
# 轨道解析
# ---------------------------------------------------------------------------

PLAYER_PAYLOAD = {
    "code": 0,
    "data": {
        "subtitle": {
            "allow_submit": False,
            "subtitles": [
                {
                    "id": 1,
                    "lan": "zh-CN",
                    "lan_doc": "中文（中国）",
                    "subtitle_url": "//aisubtitle.hdslb.com/zhcn.json",
                    "ai_type": 0,
                },
                {
                    "id": 2,
                    "lan": "ai-zh",
                    "lan_doc": "中文（自动生成）",
                    "subtitle_url": "//aisubtitle.hdslb.com/aizh.json",
                    "ai_type": 1,
                },
                {
                    "id": 3,
                    "lan": "en-US",
                    "lan_doc": "English(US)",
                    "subtitle_url": "https://x/en.json",
                },
                {
                    "id": 4,
                    "lan": "ja",
                    "lan_doc": "日本語",
                    "subtitle_url": "",  # 无内容地址，应跳过
                },
            ],
        }
    },
}


def test_parse_tracks_skips_empty_urls():
    tracks = parse_subtitle_tracks(PLAYER_PAYLOAD)
    assert len(tracks) == 3
    assert all(t.url for t in tracks)


def test_parse_tracks_detects_ai():
    tracks = {t.lan: t for t in parse_subtitle_tracks(PLAYER_PAYLOAD)}
    assert tracks["ai-zh"].is_ai is True
    assert tracks["ai-zh"].source is TranscriptSource.BILI_AI
    assert tracks["zh-CN"].is_ai is False
    assert tracks["zh-CN"].source is TranscriptSource.BILI_CC


def test_parse_tracks_accepts_inner_data_object():
    """直接传 data 本身也应能解析。"""
    tracks = parse_subtitle_tracks(PLAYER_PAYLOAD["data"])
    assert len(tracks) == 3


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        "string",
        {},
        {"data": None},
        {"data": {}},
        {"data": {"subtitle": None}},
        {"data": {"subtitle": {"subtitles": None}}},
        {"data": {"subtitle": {"subtitles": "oops"}}},
        {"data": {"subtitle": {"subtitles": [None, 1, "x"]}}},
    ],
)
def test_parse_tracks_tolerates_garbage(payload):
    """结构异常时安静返回空列表 —— 上层会自然回落到 Whisper。"""
    assert parse_subtitle_tracks(payload) == []


def test_ai_detected_by_lan_prefix():
    payload = {
        "data": {
            "subtitle": {
                "subtitles": [
                    {"lan": "ai-en", "subtitle_url": "//x/y.json"},
                ]
            }
        }
    }
    tracks = parse_subtitle_tracks(payload)
    assert len(tracks) == 1
    assert tracks[0].is_ai is True


# ---------------------------------------------------------------------------
# 轨道选择
# ---------------------------------------------------------------------------


def test_pick_prefers_human_chinese_over_ai():
    tracks = parse_subtitle_tracks(PLAYER_PAYLOAD)
    best = pick_best_track(tracks)
    assert best is not None
    assert best.lan == "zh-CN"
    assert not best.is_ai


def test_pick_prefers_zh_hans_above_zh_cn():
    tracks = [
        SubtitleTrack(lan="zh-CN", lan_doc="", url="https://a", is_ai=False),
        SubtitleTrack(lan="zh-Hans", lan_doc="", url="https://b", is_ai=False),
    ]
    assert pick_best_track(tracks).lan == "zh-Hans"


def test_pick_falls_back_to_ai_when_only_option():
    tracks = [
        SubtitleTrack(lan="ai-zh", lan_doc="自动", url="https://a", is_ai=True),
        SubtitleTrack(lan="en-US", lan_doc="English", url="https://b", is_ai=False),
    ]
    assert pick_best_track(tracks).lan == "ai-zh"


def test_pick_returns_none_for_empty():
    assert pick_best_track([]) is None


# ---------------------------------------------------------------------------
# 内容解析
# ---------------------------------------------------------------------------

BODY_PAYLOAD = {
    "font_size": 0.4,
    "type": "AIsubtitle",
    "body": [
        {"from": 1.86, "to": 4.46, "content": "第一句"},
        {"from": 5.82, "to": 13.25, "content": "第二句"},
        {"from": 15.759, "to": 17.999, "content": ""},  # 空内容应跳过
        {"from": "20.5", "to": "22.0", "content": "字符串时间戳"},
    ],
}


def test_parse_body():
    segs = parse_subtitle_body(BODY_PAYLOAD)
    assert len(segs) == 3
    assert segs[0].text == "第一句"
    assert segs[0].start == pytest.approx(1.86)


def test_parse_body_handles_string_timestamps():
    segs = parse_subtitle_body(BODY_PAYLOAD)
    assert segs[-1].start == pytest.approx(20.5)


def test_parse_body_sorts_by_start():
    payload = {
        "body": [
            {"from": 10, "to": 12, "content": "后"},
            {"from": 0, "to": 2, "content": "前"},
        ]
    }
    segs = parse_subtitle_body(payload)
    assert [s.text for s in segs] == ["前", "后"]


def test_parse_body_end_before_start_is_clamped():
    segs = parse_subtitle_body({"body": [{"from": 10, "to": 5, "content": "x"}]})
    assert segs[0].end >= segs[0].start


@pytest.mark.parametrize(
    "payload", [None, [], {}, {"body": None}, {"body": "x"}, {"body": [None, 1]}]
)
def test_parse_body_tolerates_garbage(payload):
    assert parse_subtitle_body(payload) == []


# ---------------------------------------------------------------------------
# 片段合并
# ---------------------------------------------------------------------------


def test_merge_combines_close_fragments():
    segs = [
        TranscriptSegment(start=0.0, end=1.0, text="今天"),
        TranscriptSegment(start=1.1, end=2.0, text="我们"),
        TranscriptSegment(start=2.1, end=3.0, text="聊聊向量数据库"),
    ]
    merged = merge_adjacent_segments(segs, max_gap=1.5, max_chars=120)
    assert len(merged) == 1
    assert merged[0].text == "今天我们聊聊向量数据库"
    # 保留首段时间戳，保证定位准确
    assert merged[0].start == 0.0
    assert merged[0].end == 3.0


def test_merge_splits_on_large_gap():
    segs = [
        TranscriptSegment(start=0.0, end=1.0, text="第一段"),
        TranscriptSegment(start=10.0, end=11.0, text="很久之后"),
    ]
    merged = merge_adjacent_segments(segs, max_gap=1.5)
    assert len(merged) == 2


def test_merge_respects_char_limit():
    segs = [
        TranscriptSegment(start=float(i), end=float(i) + 0.5, text="字" * 30)
        for i in range(10)
    ]
    merged = merge_adjacent_segments(segs, max_gap=5.0, max_chars=100)
    assert len(merged) > 1
    assert all(len(m.text) <= 100 for m in merged)


def test_merge_respects_duration_limit():
    segs = [
        TranscriptSegment(start=float(i * 10), end=float(i * 10 + 9), text="内容")
        for i in range(10)
    ]
    merged = merge_adjacent_segments(segs, max_gap=1.5, max_chars=10000, max_duration=30)
    assert all(m.duration <= 30 for m in merged)


def test_merge_inserts_space_between_latin_words():
    segs = [
        TranscriptSegment(start=0.0, end=1.0, text="hello"),
        TranscriptSegment(start=1.1, end=2.0, text="world"),
    ]
    assert merge_adjacent_segments(segs)[0].text == "hello world"


def test_merge_does_not_insert_space_in_chinese():
    segs = [
        TranscriptSegment(start=0.0, end=1.0, text="你好"),
        TranscriptSegment(start=1.1, end=2.0, text="世界"),
    ]
    assert merge_adjacent_segments(segs)[0].text == "你好世界"


def test_merge_preserves_all_text():
    """合并不能丢字 —— 这是最要命的回归。"""
    segs = [
        TranscriptSegment(start=float(i), end=float(i) + 0.5, text=f"片段{i}号")
        for i in range(50)
    ]
    merged = merge_adjacent_segments(segs, max_gap=5.0, max_chars=60)
    joined = "".join(m.text for m in merged)
    for i in range(50):
        assert f"片段{i}号" in joined


def test_merge_empty_input():
    assert merge_adjacent_segments([]) == []


def test_merge_single_segment_unchanged():
    segs = [TranscriptSegment(start=0.0, end=1.0, text="只有一句")]
    merged = merge_adjacent_segments(segs)
    assert len(merged) == 1
    assert merged[0].text == "只有一句"


def test_merge_sorts_unordered_input():
    segs = [
        TranscriptSegment(start=5.0, end=6.0, text="后"),
        TranscriptSegment(start=0.0, end=1.0, text="前"),
    ]
    merged = merge_adjacent_segments(segs, max_gap=0.1)
    assert merged[0].text == "前"
