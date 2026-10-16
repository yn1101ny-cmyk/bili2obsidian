"""转写文本分块 —— 纯函数。

长视频的字幕远超模型上下文，必须分块做 map-reduce。
分块质量直接决定笔记质量，所以这里有三条原则：

1. **不切断语义**：只在片段边界切，不切在句子中间；
2. **带时间戳**：每块都保留 ``[mm:ss]``，让模型能输出可跳转的时间点；
3. **留重叠**：相邻块重叠一部分，避免结论被切断在两个块之间。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.schemas import TranscriptSegment, format_clock


@dataclass(slots=True)
class Chunk:
    """一个待送入模型的文本块。"""

    index: int
    total: int
    segments: list[TranscriptSegment] = field(default_factory=list)
    #: 该块在原始转写里的字符偏移，便于回溯
    char_offset: int = 0

    @property
    def start(self) -> float:
        return self.segments[0].start if self.segments else 0.0

    @property
    def end(self) -> float:
        return self.segments[-1].end if self.segments else 0.0

    @property
    def char_count(self) -> int:
        return sum(len(s.text) for s in self.segments)

    def render(self, *, with_timestamps: bool = True) -> str:
        """渲染成给模型看的文本。"""
        lines: list[str] = []
        for seg in self.segments:
            text = seg.text.strip()
            if not text:
                continue
            if with_timestamps:
                lines.append(f"[{format_clock(seg.start)}] {text}")
            else:
                lines.append(text)
        return "\n".join(lines)

    @property
    def time_range(self) -> str:
        return f"{format_clock(self.start)} - {format_clock(self.end)}"


def chunk_segments(
    segments: list[TranscriptSegment],
    *,
    max_chars: int = 2400,
    overlap_chars: int = 200,
) -> list[Chunk]:
    """把转写片段切成若干块。

    按字符预算切分，块间保留约 ``overlap_chars`` 字符的重叠。

    :param max_chars: 单块字符上限（含时间戳前缀的粗略估计）
    :param overlap_chars: 重叠字符数；0 表示不重叠
    """
    usable = [s for s in segments if s.text.strip()]
    if not usable:
        return []

    max_chars = max(200, max_chars)
    overlap_chars = max(0, min(overlap_chars, max_chars // 2))

    # 先按字符预算把片段归组
    groups: list[list[TranscriptSegment]] = []
    cur: list[TranscriptSegment] = []
    cur_len = 0

    for seg in usable:
        # +12 估算时间戳前缀 "[00:00] " 的开销
        seg_len = len(seg.text) + 12
        if cur and cur_len + seg_len > max_chars:
            groups.append(cur)
            # 回退若干片段作为下一组的重叠开头
            carry: list[TranscriptSegment] = []
            carry_len = 0
            for prev in reversed(cur):
                if carry_len + len(prev.text) + 12 > overlap_chars:
                    break
                carry.insert(0, prev)
                carry_len += len(prev.text) + 12
            cur = list(carry)
            cur_len = carry_len
        cur.append(seg)
        cur_len += seg_len

    if cur:
        groups.append(cur)

    total = len(groups)
    chunks: list[Chunk] = []
    offset = 0
    for i, group in enumerate(groups):
        chunks.append(
            Chunk(index=i, total=total, segments=group, char_offset=offset)
        )
        offset += sum(len(s.text) for s in group)
    return chunks


def transcript_to_text(
    segments: list[TranscriptSegment],
    *,
    with_timestamps: bool = True,
    max_chars: int | None = None,
) -> str:
    """把整段转写渲染成文本（可选截断）。"""
    lines: list[str] = []
    size = 0
    for seg in segments:
        text = seg.text.strip()
        if not text:
            continue
        line = f"[{format_clock(seg.start)}] {text}" if with_timestamps else text
        if max_chars is not None and size + len(line) > max_chars:
            lines.append("……（后续内容已截断）")
            break
        lines.append(line)
        size += len(line) + 1
    return "\n".join(lines)


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数。

    中文约 1 字 ≈ 1 token，英文约 4 字符 ≈ 1 token。
    这里给一个偏保守（偏高）的估计，用于判断是否需要分块。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if _is_cjk(ch))
    other = len(text) - cjk
    return cjk + other // 3 + 1


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return 0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF
