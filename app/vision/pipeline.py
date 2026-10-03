"""把「画面上的字」变成可以喂给笔记模型的结构化文本。

流程::

    抽帧 → 画面去重 → OCR → 剔除字幕 → 剔除水印 → 合并连续相同 → 带时间戳的文本块

## 两个关键设计

**1. 用 ASR 结果剔除字幕 —— 这是本项目的独有优势。**

教程视频的画面底下通常挂着字幕，内容和音频转写**完全重复**。
如果直接塞进提示词，等于把同一段话喂两遍，白白吃掉上下文。

通用 OCR 方案只能靠「字幕总在画面底部某个位置」这种启发式，
对花字、弹幕式字幕就失效了。而我们**已经有音频转写** ——
把 OCR 出的每一行和转写文本做归一化比对，命中就丢掉。
这比位置规则可靠得多。

**2. 水印/台标靠「几乎每帧都出现」来识别。**

固定角标、UP主 logo、频道的固定标题会在大多数帧里出现。
只保留出现率低的行，避免把真正的板书/PPT 内容误删。
（护栏：总帧数太少时不敢判定，宁可多留。）
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from app.vision.dedup import dedup_frames
from app.vision.frames import Frame, extract_frames, probe_duration
from app.vision.ocr import OcrEngine

logger = logging.getLogger(__name__)

#: 归一化时要去掉的字符（标点、空白、常见装饰）
_NOISE_RE = re.compile(r"[\s\u3000·、，,。.:：;；!！?？\-—_()（）\[\]【】\"'“”‘’|/\\~]+")

#: 一行文本与转写的相似度超过这个值，就认为它是字幕
SUBTITLE_SIMILARITY = 0.75

#: 一行的最短有效长度（太短的多半是噪点或 logo 残片）
MIN_LINE_LENGTH = 3

#: 出现率高于这个值的行，判定为水印/台标
WATERMARK_RATIO = 0.7

#: 总帧数少于这个值时不敢判水印（样本太小，容易误杀）
WATERMARK_MIN_FRAMES = 6


@dataclass(slots=True)
class OnScreenText:
    """某一时刻画面上的文字。"""

    timestamp: float
    lines: list[str]

    def clock(self) -> str:
        from app.schemas import format_clock

        return format_clock(self.timestamp)

    def render(self, *, max_lines: int = 12) -> str:
        body = "\n".join(f"  {ln}" for ln in self.lines[:max_lines])
        if len(self.lines) > max_lines:
            body += f"\n  …（还有 {len(self.lines) - max_lines} 行）"
        return f"[{self.clock()}] 画面内容：\n{body}"


def normalize(text: str) -> str:
    """归一化文本，用于相似度比较。"""
    return _NOISE_RE.sub("", (text or "").lower())


def is_subtitle_like(
    line: str,
    transcript_norm: str,
    *,
    threshold: float = SUBTITLE_SIMILARITY,
) -> bool:
    """这一行是不是已经在音频转写里了。

    两级判断：先看子串包含（快且准），再用相似度兜住轻微差异。
    """
    norm = normalize(line)
    if len(norm) < MIN_LINE_LENGTH:
        return True  # 太短的没有信息量，直接当噪音丢掉
    if not transcript_norm:
        return False

    if norm in transcript_norm:
        return True

    # 子串没命中时退一步算相似度。
    # 为了避免长文本下的 O(n²)，只跟长度接近的片段比 —— 这里用滑窗近似。
    window = len(norm)
    if window < 6:
        return False
    step = max(1, window // 2)
    for start in range(0, max(1, len(transcript_norm) - window + 1), step):
        chunk = transcript_norm[start : start + window]
        if SequenceMatcher(None, norm, chunk).ratio() >= threshold:
            return True
    return False


def drop_subtitles(
    lines_by_frame: list[list[str]],
    transcript_text: str,
    *,
    threshold: float = SUBTITLE_SIMILARITY,
) -> list[list[str]]:
    """逐行剔除与音频转写重复的字幕。"""
    transcript_norm = normalize(transcript_text)
    if not transcript_norm:
        return lines_by_frame
    return [
        [ln for ln in lines if not is_subtitle_like(ln, transcript_norm, threshold=threshold)]
        for lines in lines_by_frame
    ]


def find_persistent_lines(
    lines_by_frame: list[list[str]],
    *,
    min_frames: int = WATERMARK_MIN_FRAMES,
    ratio: float = WATERMARK_RATIO,
) -> set[str]:
    """找出「几乎每帧都出现」的行（水印、台标、固定标题）。

    ⚠️ 护栏：帧数太少时直接返回空集 —— 曾经的教训是把整屏正文
    当成水印全删了。
    """
    total = len(lines_by_frame)
    if total < min_frames:
        return set()

    counts: dict[str, int] = {}
    for lines in lines_by_frame:
        for norm in {normalize(ln) for ln in lines}:
            if len(norm) >= MIN_LINE_LENGTH:
                counts[norm] = counts.get(norm, 0) + 1

    persistent = {k for k, c in counts.items() if c / total >= ratio}
    if persistent:
        logger.info("识别出 %d 行固定内容（水印/台标），将剔除", len(persistent))
    return persistent


def drop_persistent(
    lines_by_frame: list[list[str]], persistent: set[str]
) -> list[list[str]]:
    if not persistent:
        return lines_by_frame
    return [
        [ln for ln in lines if normalize(ln) not in persistent]
        for lines in lines_by_frame
    ]


def merge_consecutive(
    frames: list[Frame], lines_by_frame: list[list[str]]
) -> list[OnScreenText]:
    """把连续相同的画面内容合并成一段，减少重复。

    时间戳取**第一次出现**的时刻 —— 这样跳转能落到内容刚刚出现的位置。
    """
    merged: list[OnScreenText] = []
    previous_key: tuple[str, ...] | None = None

    for frame, lines in zip(frames, lines_by_frame):
        cleaned = [ln for ln in lines if ln.strip()]
        if not cleaned:
            continue
        key = tuple(normalize(ln) for ln in cleaned)
        if key == previous_key:
            continue  # 与上一段内容完全一样，跳过
        merged.append(OnScreenText(timestamp=frame.timestamp, lines=cleaned))
        previous_key = key

    return merged


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def extract_on_screen_text(
    video: str | Path,
    *,
    work_dir: Path,
    transcript_text: str = "",
    interval: float = 15.0,
    max_frames: int = 240,
    engine: OcrEngine | None = None,
    on_progress: Callable[[float, str], None] | None = None,
) -> list[OnScreenText]:
    """从视频里提取画面文字。

    :param transcript_text: 音频转写全文 —— 用来剔除重复的字幕
    :param on_progress: ``(0~1, 消息)``
    """
    def report(frac: float, msg: str) -> None:
        if on_progress:
            on_progress(max(0.0, min(1.0, frac)), msg)

    report(0.0, "正在探测视频…")
    duration = probe_duration(video)
    if duration <= 0:
        logger.warning("读不出视频时长，画面识别效果会受限")

    report(0.05, "正在抽取关键帧…")
    frames = extract_frames(
        video,
        interval=interval,
        max_frames=max_frames,
        work_dir=work_dir / "frames",
        on_progress=lambda f, m: report(0.05 + 0.25 * f, m),
    )
    if not frames:
        logger.warning("没有抽到任何帧，跳过画面识别")
        return []

    # ---- 去重：在 OCR 之前砍掉大量重复画面 ----
    report(0.30, "正在筛掉重复画面…")
    frames = _dedup(frames)
    if not frames:
        return []

    # ---- OCR ----
    report(0.35, f"正在识别画面文字（{len(frames)} 帧）…")
    ocr = engine or OcrEngine()
    lines_by_frame: list[list[str]] = []
    for i, frame in enumerate(frames):
        lines_by_frame.append(ocr.recognize(frame.path))
        report(
            0.35 + 0.60 * (i + 1) / len(frames),
            f"识别画面文字 {i + 1}/{len(frames)}",
        )

    # ---- 清洗 ----
    report(0.96, "正在剔除字幕与固定水印…")
    lines_by_frame = drop_subtitles(lines_by_frame, transcript_text)
    persistent = find_persistent_lines(lines_by_frame)
    lines_by_frame = drop_persistent(lines_by_frame, persistent)

    merged = merge_consecutive(frames, lines_by_frame)
    total_lines = sum(len(m.lines) for m in merged)
    logger.info(
        "画面文字提取完成：%d 段、%d 行（原始 %d 帧）",
        len(merged),
        total_lines,
        len(frames),
    )
    report(1.0, f"画面文字：{len(merged)} 段")
    return merged


def _dedup(frames: list[Frame]) -> list[Frame]:
    """按画面内容去重。读图失败时退回不去重。"""
    try:
        import cv2
    except ImportError:
        logger.debug("没有 cv2，跳过画面去重")
        return frames

    def load(frame: Frame):
        try:
            img = cv2.imread(str(frame.path), cv2.IMREAD_GRAYSCALE)
        except Exception:  # noqa: BLE001
            return None
        if img is None:
            return None
        # 缩到小尺寸算签名，够用且更快
        return cv2.resize(img, (64, 36), interpolation=cv2.INTER_AREA)

    return dedup_frames(frames, load)
