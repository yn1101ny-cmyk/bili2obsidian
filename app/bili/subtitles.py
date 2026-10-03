"""字幕解析 —— 纯函数，重点覆盖测试。

涉及两种 payload：

1. ``/x/player/v2`` 返回的 ``data.subtitle.subtitles`` —— 字幕**轨道列表**；
2. 轨道 ``subtitle_url`` 指向的 JSON —— 真正的字幕**内容**（``body`` 数组）。

所有解析都对字段缺失保持宽容：B站接口结构时有微调，
宁可少拿信息，也不要因为一个可选字段缺失就整条链路崩掉。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.schemas import Chapter, TranscriptSegment, TranscriptSource

#: 简体中文语言的优先顺序（人工字幕优先于 AI 字幕）
_PREFERRED_LANGS: tuple[str, ...] = (
    "zh-Hans",
    "zh-CN",
    "zh-Hans-CN",
    "ai-zh",
    "zh",
    "zh-Hant",
    "zh-TW",
)


@dataclass(frozen=True, slots=True)
class SubtitleTrack:
    """一条字幕轨道。"""

    lan: str
    lan_doc: str
    url: str
    is_ai: bool
    track_id: int | None = None
    is_lock: bool = False

    @property
    def source(self) -> TranscriptSource:
        return TranscriptSource.BILI_AI if self.is_ai else TranscriptSource.BILI_CC

    @property
    def label(self) -> str:
        return f"{self.lan_doc or self.lan}{'（AI）' if self.is_ai else ''}"


def normalize_subtitle_url(url: str) -> str:
    """``//aisubtitle.hdslb.com/x.json`` -> ``https://aisubtitle.hdslb.com/x.json``。"""
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        return f"https:{url}"
    if url.startswith("http://"):
        # 统一走 https，避免明文传输
        return "https://" + url[len("http://") :]
    if not url.startswith("https://"):
        return f"https://{url.lstrip('/')}"
    return url


def parse_subtitle_tracks(payload: Any) -> list[SubtitleTrack]:
    """从 ``/x/player/v2`` 响应里解析出字幕轨道列表。

    容忍 payload 是完整响应（含 ``data``）或已经是 ``data`` 本身。
    结构异常时返回空列表，而不是抛异常 —— 空列表会自然触发 Whisper 兜底。
    """
    if not isinstance(payload, dict):
        return []

    data = payload.get("data", payload)
    if not isinstance(data, dict):
        return []

    subtitle = data.get("subtitle")
    if not isinstance(subtitle, dict):
        return []

    raw_list = subtitle.get("subtitles")
    if not isinstance(raw_list, list):
        return []

    tracks: list[SubtitleTrack] = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        url = normalize_subtitle_url(str(item.get("subtitle_url") or ""))
        if not url:
            continue  # 没有内容地址的轨道没有意义
        lan = str(item.get("lan") or "")
        # ai_type 存在 / lan 以 ai- 开头 => B站自动生成
        is_ai = bool(item.get("ai_type")) or lan.lower().startswith("ai-")
        track_id = item.get("id")
        tracks.append(
            SubtitleTrack(
                lan=lan,
                lan_doc=str(item.get("lan_doc") or ""),
                url=url,
                is_ai=is_ai,
                track_id=track_id if isinstance(track_id, int) else None,
                is_lock=bool(item.get("is_lock")),
            )
        )
    return tracks


def pick_best_track(
    tracks: list[SubtitleTrack],
    preferred: tuple[str, ...] = _PREFERRED_LANGS,
) -> SubtitleTrack | None:
    """挑最合适的中文字幕轨道。

    排序规则：语言优先级 > 人工字幕优先于 AI 字幕 > 稳定性。
    """
    if not tracks:
        return None

    order = {lang.lower(): i for i, lang in enumerate(preferred)}

    def sort_key(t: SubtitleTrack) -> tuple[int, int, str]:
        lang_rank = order.get(t.lan.lower(), len(preferred))
        # 同语言下，人工字幕(0) 优于 AI 字幕(1)
        ai_rank = 1 if t.is_ai else 0
        return (lang_rank, ai_rank, t.lan)

    return min(tracks, key=sort_key)


def parse_subtitle_body(payload: Any) -> list[TranscriptSegment]:
    """解析字幕内容 JSON 的 ``body`` 数组为 TranscriptSegment 列表。

    同时容忍 ``from``/``to`` 为字符串、以及 ``content`` 缺失的情况。
    """
    if not isinstance(payload, dict):
        return []

    body = payload.get("body")
    if not isinstance(body, list):
        return []

    segments: list[TranscriptSegment] = []
    for item in body:
        if not isinstance(item, dict):
            continue
        text = str(item.get("content") or "").strip()
        if not text:
            continue
        start = _to_float(item.get("from"))
        end = _to_float(item.get("to"))
        if start is None:
            start = segments[-1].end if segments else 0.0
        if end is None or end < start:
            end = start
        segments.append(TranscriptSegment(start=start, end=end, text=text))

    segments.sort(key=lambda s: s.start)
    return segments


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def parse_view_points(payload: Any) -> list[Chapter]:
    """从 ``/x/player/v2`` 响应里解析 B站 AI 生成的章节。

    **这是未登录就能拿到的免费结构化线索** —— 相当于平台已经替我们
    把视频分好了块。用它来推断重点，比让模型从零去猜靠谱得多。

    实测（调研子代理）：未登录时 ``subtitle.subtitles`` 是空的，
    但 ``view_points`` 有内容（8 段）。所以这是「不分析音频」的
    第一个可用信号。
    """
    if not isinstance(payload, dict):
        return []

    data = payload.get("data", payload)
    if not isinstance(data, dict):
        return []

    raw = data.get("view_points")
    if not isinstance(raw, list):
        return []

    chapters: list[Chapter] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        title = str(
            item.get("content") or item.get("title") or item.get("name") or ""
        ).strip()
        if not title:
            continue
        start = _to_float(item.get("from"))
        if start is None:
            start = _to_float(item.get("start")) or _to_float(item.get("time")) or 0.0
        chapters.append(Chapter(title=title, start=start))

    chapters.sort(key=lambda c: c.start)
    return chapters


# ---------------------------------------------------------------------------
# 片段合并
# ---------------------------------------------------------------------------


def merge_adjacent_segments(
    segments: list[TranscriptSegment],
    *,
    max_gap: float = 1.5,
    max_chars: int = 120,
    max_duration: float = 30.0,
) -> list[TranscriptSegment]:
    """把细碎的字幕行合并成可读的句子。

    B站 AI 字幕和 Whisper 都会产出很短的片段（几个字一条），
    直接喂给模型会浪费上下文且语义割裂。这里按「间隔小、总长不超限」合并，
    同时保留首行时间戳，保证时间定位仍然准确。

    :param max_gap: 相邻片段间隔超过这个秒数就不合并（视为换话题/停顿）
    :param max_chars: 合并后单段字数上限
    :param max_duration: 合并后单段时长上限（秒）
    """
    if not segments:
        return []

    ordered = sorted(segments, key=lambda s: s.start)
    merged: list[TranscriptSegment] = []
    cur_start = ordered[0].start
    cur_end = ordered[0].end
    buf = ordered[0].text

    for seg in ordered[1:]:
        gap = seg.start - cur_end
        would_be = len(buf) + len(seg.text)
        would_dur = seg.end - cur_start
        # 中文按字符直接拼接，英文/数字之间补空格
        joiner = "" if _is_cjk_boundary(buf, seg.text) else " "
        fits = (
            gap <= max_gap
            and would_be <= max_chars
            and would_dur <= max_duration
        )
        if fits:
            buf = f"{buf}{joiner}{seg.text}"
            cur_end = max(cur_end, seg.end)
        else:
            merged.append(
                TranscriptSegment(start=cur_start, end=cur_end, text=buf.strip())
            )
            cur_start, cur_end, buf = seg.start, seg.end, seg.text

    if buf.strip():
        merged.append(
            TranscriptSegment(start=cur_start, end=cur_end, text=buf.strip())
        )
    return merged


def _is_cjk_boundary(left: str, right: str) -> bool:
    """判断两段之间是否该直接拼接（中文）而不是加空格。"""
    if not left or not right:
        return True
    return _is_cjk(left[-1]) or _is_cjk(right[0])


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (
        0x4E00 <= code <= 0x9FFF      # CJK 统一表意
        or 0x3400 <= code <= 0x4DBF   # 扩展A
        or 0x3000 <= code <= 0x303F   # CJK 标点
        or 0xFF00 <= code <= 0xFFEF   # 全角
        or 0x3040 <= code <= 0x30FF   # 假名
    )
