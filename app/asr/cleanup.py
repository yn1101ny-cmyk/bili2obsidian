"""转写文本清洗 —— 主要处理 Whisper 的**复读幻觉**。

实测症状：56 分钟的装机推荐视频里，多处出现整段重复同一个字
（``替替替替替替…`` / ``宗宗宗宗…`` / ``余帽余帽余帽…``）。
这类片段没有任何信息，却会：
  * 污染笔记内容（模型会把垃圾当成内容去总结）；
  * 浪费上下文窗口；
  * 让「存疑」一节被同一类问题灌满。

成因是 Whisper 在长音频的静音段或低信噪比段落上陷入重复解码。
参数上已经做了 ``condition_on_previous_text=False`` 与
``repetition_penalty``，但仍会零星出现，所以再加一层确定性兜底。
"""

from __future__ import annotations

import logging
import re
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: 同一个字符连续出现这么多次，就判定为复读
DEFAULT_MIN_RUN = 6

#: 清洗后剩下的字数少于原来的这个比例，就整段丢掉
GARBAGE_RATIO = 0.35

#: 周期性重复占比超过这个值，判定为复读垃圾。
#: 实测除了「替替替替」这种单字复读，还有「余帽余帽余帽…」这种整词复读，
#: 单字符正则抓不到，必须用周期检测。
GARBAGE_PERIOD_RATIO = 0.85

#: 周期检测的最大周期长度
MAX_PERIOD = 12


def collapse_repeats(text: str, min_run: int = DEFAULT_MIN_RUN) -> str:
    """把连续重复的字符压缩成一次。

    ``"替替替替替替"`` -> ``"替"``；``"哈哈"`` 这种正常重复不受影响
    （阈值默认 6，只有明显异常的连串才会被压）。
    """
    if not text or min_run < 2:
        return text
    pattern = re.compile(rf"(.)\1{{{min_run - 1},}}", re.DOTALL)
    return pattern.sub(r"\1", text)


def repetition_ratio(text: str, max_period: int = MAX_PERIOD) -> float:
    """文本被某个短单元周期性占据的比例。

    ``1.0`` 表示整段就是同一个短单元的反复。
    用来抓 ``余帽余帽余帽…`` 这类单字符正则处理不了的整词复读。

    正常中文几乎不可能出现高周期占比（周期越长、恰好对上的概率越低），
    所以这个指标很干净。
    """
    s = (text or "").strip()
    if len(s) < DEFAULT_MIN_RUN:
        return 0.0

    best = 0.0
    for period in range(1, min(max_period, len(s) // 2) + 1):
        unit = s[:period]
        matched = sum(1 for i, ch in enumerate(s) if ch == unit[i % period])
        best = max(best, matched / len(s))
        if best >= 1.0:
            break
    return best


def is_mostly_garbage(original: str, collapsed: str) -> bool:
    """整段是否基本是复读垃圾。

    两个判据，命中任一即判定：
      1. 单字符压缩后信息量所剩无几（``替替替替…``）；
      2. 呈现强周期性重复（``余帽余帽余帽…``）。
    """
    original_stripped = original.strip()
    if not original_stripped:
        return True

    if len(collapsed.strip()) < len(original_stripped) * GARBAGE_RATIO:
        return True

    return repetition_ratio(original_stripped) >= GARBAGE_PERIOD_RATIO


def clean_text(text: str, min_run: int = DEFAULT_MIN_RUN) -> tuple[str, bool]:
    """清洗单段文本。

    :returns: ``(清洗后的文本, 是否判定为垃圾)``
    """
    collapsed = collapse_repeats(text, min_run=min_run)
    if is_mostly_garbage(text, collapsed):
        return "", True
    return collapsed, False


def clean_segments(
    segments: list[T],
    *,
    min_run: int = DEFAULT_MIN_RUN,
    text_of=lambda s: s.text,  # type: ignore[assignment]
    with_text=lambda s, t: s.model_copy(update={"text": t}),  # type: ignore[assignment]
) -> tuple[list[T], int, int]:
    """清洗一批转写片段。

    :returns: ``(清洗后的片段, 丢弃的段数, 改写过的段数)``
    """
    kept: list[T] = []
    dropped = 0
    rewritten = 0

    for seg in segments:
        original = text_of(seg)
        cleaned, garbage = clean_text(original, min_run=min_run)
        if garbage:
            dropped += 1
            continue
        if cleaned != original:
            rewritten += 1
            seg = with_text(seg, cleaned)
        kept.append(seg)

    if dropped or rewritten:
        logger.info(
            "转写清洗：丢弃 %d 段复读垃圾，修正 %d 段", dropped, rewritten
        )
    return kept, dropped, rewritten
