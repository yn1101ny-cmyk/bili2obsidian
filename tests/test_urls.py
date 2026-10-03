"""链接解析测试。

这是用户接触产品的第一个环节，也是最容易被各种粘贴格式搞崩的地方，
所以用例覆盖得比较密。
"""

from __future__ import annotations

import pytest

from app.bili.urls import VideoRef, extract_first_url, parse_video_input
from app.errors import InvalidVideoUrl

BV = "BV1GJ411x7h7"


# ---------------------------------------------------------------------------
# 裸 ID
# ---------------------------------------------------------------------------


def test_bare_bv():
    ref = parse_video_input(BV)
    assert ref.kind == "bvid"
    assert ref.bvid == BV
    assert ref.page == 1
    assert not ref.needs_resolution


def test_bare_bv_with_whitespace():
    assert parse_video_input(f"  {BV}\n").bvid == BV


def test_bare_bv_wrapped_in_brackets():
    """从分享文案里复制常常带上书名号/引号。"""
    for wrapped in (f"《{BV}》", f"「{BV}」", f'"{BV}"', f"【{BV}】"):
        assert parse_video_input(wrapped).bvid == BV


def test_bare_av_lowercase():
    ref = parse_video_input("av170001")
    assert ref.kind == "aid"
    assert ref.aid == 170001


def test_bare_av_uppercase():
    ref = parse_video_input("AV170001")
    assert ref.kind == "aid"
    assert ref.aid == 170001


# ---------------------------------------------------------------------------
# 完整链接
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        f"https://www.bilibili.com/video/{BV}",
        f"https://www.bilibili.com/video/{BV}/",
        f"https://m.bilibili.com/video/{BV}",
        f"http://www.bilibili.com/video/{BV}",
        f"https://www.bilibili.com/video/{BV}/?spm_id_from=333.999.0.0",
        f"https://www.bilibili.com/video/{BV}#reply",
    ],
)
def test_full_urls(url):
    ref = parse_video_input(url)
    assert ref.kind == "bvid"
    assert ref.bvid == BV


def test_av_url():
    ref = parse_video_input("https://www.bilibili.com/video/av170001")
    assert ref.kind == "aid"
    assert ref.aid == 170001


def test_page_param():
    ref = parse_video_input(f"https://www.bilibili.com/video/{BV}?p=3")
    assert ref.page == 3


def test_page_param_with_other_params_first():
    ref = parse_video_input(f"https://www.bilibili.com/video/{BV}?spm_id_from=x&p=7")
    assert ref.page == 7


def test_invalid_page_falls_back_to_one():
    for bad in ("p=0", "p=-2", "p=abc"):
        ref = parse_video_input(f"https://www.bilibili.com/video/{BV}?{bad}")
        assert ref.page == 1


# ---------------------------------------------------------------------------
# 时间点
# ---------------------------------------------------------------------------


def test_time_plain_seconds():
    assert parse_video_input(f"https://www.bilibili.com/video/{BV}?t=90").t == 90.0


def test_time_hms_format():
    assert parse_video_input(f"https://www.bilibili.com/video/{BV}?t=1m30s").t == 90.0
    assert parse_video_input(f"https://www.bilibili.com/video/{BV}?t=1h2m3s").t == 3723.0


def test_time_absent():
    assert parse_video_input(f"https://www.bilibili.com/video/{BV}").t is None


# ---------------------------------------------------------------------------
# 短链
# ---------------------------------------------------------------------------


def test_short_link_needs_resolution():
    ref = parse_video_input("https://b23.tv/AbCdEf")
    assert ref.kind == "short"
    assert ref.needs_resolution
    assert ref.bvid == "AbCdEf"


def test_short_link_without_scheme_is_treated_as_bv_like_text():
    """``b23.tv/xxx`` 不带协议时不该被误判成 BV 号，而应报错。"""
    with pytest.raises(InvalidVideoUrl):
        parse_video_input("b23.tv/AbCdEf")


# ---------------------------------------------------------------------------
# 从分享文案里提取
# ---------------------------------------------------------------------------


def test_extract_url_from_share_text():
    text = f"【标题】看看这个 https://www.bilibili.com/video/{BV}?share_source=copy 很有意思"
    ref = parse_video_input(text)
    assert ref.bvid == BV


def test_extract_url_strips_trailing_punctuation():
    text = f"链接：https://www.bilibili.com/video/{BV}。"
    assert extract_first_url(text) == f"https://www.bilibili.com/video/{BV}"


def test_extract_first_url_returns_none_without_url():
    assert extract_first_url("这里没有链接") is None


def test_extract_first_url_picks_the_first():
    a = f"https://www.bilibili.com/video/{BV}"
    b = "https://www.bilibili.com/video/BV1xx411c7mD"
    assert extract_first_url(f"{a} 和 {b}") == a


# ---------------------------------------------------------------------------
# 错误处理
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "hello world",
        f"https://www.youtube.com/watch?v={BV}",
        "https://www.bilibili.com/",
        f"https://www.bilibili.com/video/notavalidid",
    ],
)
def test_invalid_inputs(text):
    with pytest.raises(InvalidVideoUrl):
        parse_video_input(text)


def test_bv_too_short_is_rejected():
    with pytest.raises(InvalidVideoUrl):
        parse_video_input("BV123")


def test_unsupported_pages_give_specific_reason():
    """番剧/课程这类要给出明确解释，而不是笼统说「解析失败」。"""
    cases = {
        "https://www.bilibili.com/bangumi/play/ep123456": "番剧",
        "https://www.bilibili.com/cheese/play/ep123": "课程",
        "https://www.bilibili.com/read/cv123": "专栏",
        "https://www.bilibili.com/live/12345": "直播",
    }
    for url, keyword in cases.items():
        with pytest.raises(InvalidVideoUrl) as exc:
            parse_video_input(url)
        assert keyword in str(exc.value)


def test_error_carries_original_input():
    with pytest.raises(InvalidVideoUrl) as exc:
        parse_video_input("bad-input")
    assert exc.value.raw == "bad-input"


# ---------------------------------------------------------------------------
# VideoRef 行为
# ---------------------------------------------------------------------------


def test_video_ref_is_frozen():
    ref = VideoRef(raw="x", kind="bvid", bvid=BV)
    with pytest.raises(Exception):
        ref.page = 2  # type: ignore[misc]
