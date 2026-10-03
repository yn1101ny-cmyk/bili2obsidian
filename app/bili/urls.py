"""B站链接解析 —— 纯函数，无网络，重点覆盖测试。

支持的输入形态::

    BV1xx411c7mD                                  # 裸 BV 号
    av170001 / AV170001                           # 裸 av 号
    https://www.bilibili.com/video/BV1xx411c7mD   # 标准
    https://m.bilibili.com/video/BV1xx411c7mD     # 移动端
    https://www.bilibili.com/video/av170001       # av 形式
    https://b23.tv/AbCdEf                          # 短链（需联网展开）
    分享文案里夹带的链接                            # 从任意文本里提取

``?p=3`` 分P、``?t=90`` 时间点会被解析出来。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import parse_qs, urlparse

from app.errors import InvalidVideoUrl

#: BV 号形如 BV + 10 位 base58 字符
_BV_RE = re.compile(r"^BV[0-9A-Za-z]{10}$")
#: av 号
_AV_RE = re.compile(r"^av(\d+)$", re.IGNORECASE)

#: 从文本中捞出第一个 http(s) 链接
_URL_RE = re.compile(r"https?://[^\s<>\"'，。、）)】\]]+", re.IGNORECASE)

#: b23.tv 短链域名
_SHORT_HOSTS = frozenset({"b23.tv", "www.b23.tv"})

#: 已知但暂不支持的页面类型 -> 给用户明确解释
_UNSUPPORTED_PATHS: dict[str, str] = {
    "/bangumi/play": "番剧/影视页面暂不支持（它走的是另一套接口）",
    "/cheese/play": "付费课程页面暂不支持",
    "/read/": "专栏/文章不是视频，暂不支持",
    "/live/": "直播间不是点播视频，暂不支持",
    "/audio/": "音频区暂不支持",
    "/festival": "活动页暂不支持",
}

Kind = Literal["bvid", "aid", "short"]


@dataclass(frozen=True, slots=True)
class VideoRef:
    """解析后的视频引用。

    ``short`` 类型还需要联网展开成真实 BV 号（由 client 负责），
    保持这里是无副作用的纯解析。
    """

    raw: str
    kind: Kind
    bvid: str | None = None
    aid: int | None = None
    page: int = 1
    #: 用户链接里带的起始时间点（秒），仅用于参考
    t: float | None = None

    @property
    def needs_resolution(self) -> bool:
        return self.kind == "short"


def extract_first_url(text: str) -> str | None:
    """从分享文案里提取第一个链接。没有则返回 None。"""
    m = _URL_RE.search(text)
    return m.group(0).rstrip(".,;:!?") if m else None


def _normalize_bv(token: str) -> str | None:
    """校验并规范化 BV 号（大小写敏感，B站用 Base58 区分大小写）。"""
    token = token.strip()
    return token if _BV_RE.match(token) else None


def _parse_page(qs: dict[str, list[str]]) -> int:
    for key in ("p", "page"):
        vals = qs.get(key)
        if vals:
            try:
                p = int(vals[0])
                if p >= 1:
                    return p
            except (TypeError, ValueError):
                pass
    return 1


def _parse_time(qs: dict[str, list[str]]) -> float | None:
    """解析 ?t=90 / ?t=1m30s / ?start=90。"""
    raw = None
    for key in ("t", "start", "start_time"):
        vals = qs.get(key)
        if vals:
            raw = vals[0]
            break
    if not raw:
        return None
    raw = raw.strip()
    if raw.isdigit():
        return float(raw)
    # 形如 1h2m3s
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", raw, re.IGNORECASE)
    if m and any(m.groups()):
        h, mi, s = (int(x) if x else 0 for x in m.groups())
        return float(h * 3600 + mi * 60 + s)
    return None


def parse_video_input(text: str) -> VideoRef:
    """把用户输入解析成 :class:`VideoRef`。失败抛 :class:`InvalidVideoUrl`。

    先用整个输入当裸 ID 试，失败再尝试从中提取 URL。
    """
    if not text or not text.strip():
        raise InvalidVideoUrl(text, "输入为空")

    raw = text.strip()
    # 去掉常见的包裹字符（分享文案里常带 《》 「」 等）
    candidate = raw.strip("《》「」『』\"'<>()（）[]【】 \t\r\n")

    # ---- 1. 裸 BV 号 ----
    bv = _normalize_bv(candidate)
    if bv:
        return VideoRef(raw=raw, kind="bvid", bvid=bv)

    # ---- 2. 裸 av 号 ----
    m = _AV_RE.match(candidate)
    if m:
        return VideoRef(raw=raw, kind="aid", aid=int(m.group(1)))

    # ---- 3. 从文本里找 URL ----
    url = candidate if candidate.lower().startswith(("http://", "https://")) else None
    if url is None:
        url = extract_first_url(raw)
    if url is None:
        raise InvalidVideoUrl(raw, "既不是 BV/av 号，也不含 http 链接")

    return _parse_url(url, raw)


def _parse_url(url: str, raw: str) -> VideoRef:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()

    # ---- 短链 ----
    if host in _SHORT_HOSTS:
        tail = parsed.path.strip("/")
        if not tail:
            raise InvalidVideoUrl(raw, "b23.tv 短链缺少路径部分")
        return VideoRef(raw=raw, kind="short", bvid=tail)

    if not host.endswith("bilibili.com"):
        raise InvalidVideoUrl(raw, f"不是 B站域名（{host or '未知域名'}）")

    path = parsed.path
    qs = parse_qs(parsed.query)

    # ---- 已知但不支持的类型，给出明确原因而不是含糊报错 ----
    for prefix, reason in _UNSUPPORTED_PATHS.items():
        if path.startswith(prefix):
            raise InvalidVideoUrl(raw, reason)

    # ---- /video/BVxxx  或  /video/av123 ----
    m = re.search(r"/video/([^/?#]+)", path)
    if not m:
        raise InvalidVideoUrl(raw, f"无法从这个路径识别视频：{path}")

    token = m.group(1)

    bv = _normalize_bv(token)
    if bv:
        return VideoRef(
            raw=raw,
            kind="bvid",
            bvid=bv,
            page=_parse_page(qs),
            t=_parse_time(qs),
        )

    av = _AV_RE.match(token)
    if av:
        return VideoRef(
            raw=raw,
            kind="aid",
            aid=int(av.group(1)),
            page=_parse_page(qs),
            t=_parse_time(qs),
        )

    raise InvalidVideoUrl(raw, f"{token!r} 既不是合法 BV 号也不是 av 号")
