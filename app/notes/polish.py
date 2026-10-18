"""本地出稿 → 云端单次润色。

分工原则：**本地模型负责「准」，云端模型负责「顺」。**

- 本地（Ollama）照常走完整条流水线：读转写、抽事实、标时间戳，
  产出结构完整的初稿 —— 全程 0 元、数据不出本机；
- 云端只拿**已经成稿的笔记 JSON** 改一遍表达和组织，
  不重新读转写，因此只花一次请求的钱（一篇长视频通常几分钱）。

硬约束（校验不过就放弃润色、保留本地稿）：

1. 不得新增视频里没有的事实、数字、结论、命令、链接；
2. 已有的时间戳一个都不能丢（字段和正文里的 ``[mm:ss]`` 都算）；
3. 代码块、命令、配置、提示词原文逐字不动；
4. 结论不能缩水太狠（正文长度至少保留初稿的 60%）。

之所以把校验写这么死：小模型最怕的不是「写得土」，而是**悄悄改错**。
"""

from __future__ import annotations

import json
import logging
import re

from app.config import Settings
from app.llm.base import LLMProvider, extract_json
from app.notes.chunking import estimate_tokens
from app.notes.pipeline import (
    _note_needs_retry,
    _outline_timestamps_bad,
    to_note,
)
from app.schemas import Note, VideoMeta

logger = logging.getLogger(__name__)

#: 正文里的 [mm:ss] / [h:mm:ss] 标记
_CLOCK_MARK_RE = re.compile(r"\[\d{1,2}:\d{2}(?::\d{2})?\]")

POLISH_SYSTEM = """你是一位中文编辑，专长是把「合格但生硬的初稿」改成「读者一眼看懂、还有作者口吻的终稿」。

你不负责找信息，只负责把**已有信息**改得更好读。你写的每个事实都必须能在初稿里找到出处。"""


def _note_clock_marks(note: Note) -> int:
    """数一下笔记正文里有多少个 ``[mm:ss]`` 标记。"""
    parts: list[str] = []
    for item in [*note.focus_points, *note.key_points, *note.actionables,
                 *note.concepts, *note.outline, *note.follow_ups,
                 *note.uncertainties]:
        for value in item.model_dump().values():
            if isinstance(value, str):
                parts.append(value)
    return len(_CLOCK_MARK_RE.findall("\n".join(parts)))


def _note_timestamps(note: Note) -> set[float]:
    """笔记里所有结构化时间戳（保留一位小数，避免浮点抖动）。"""
    stamps: set[float] = set()
    for item in [*note.focus_points, *note.key_points, *note.actionables,
                 *note.concepts, *note.outline]:
        ts = getattr(item, "timestamp", None)
        if ts is not None:
            stamps.add(round(float(ts), 1))
    return stamps


def _note_char_count(note: Note) -> int:
    total = 0
    for value in note.model_dump(mode="json").values():
        if isinstance(value, str):
            total += len(value)
        elif isinstance(value, list):
            total += len(json.dumps(value, ensure_ascii=False))
    return total


def build_polish_prompt(
    note: Note,
    meta: VideoMeta,
    *,
    instructions: str = "",
    max_chars: int = 40000,
) -> str:
    """把初稿和硬约束拼成润色提示词。"""
    draft = json.dumps(note.model_dump(mode="json"), ensure_ascii=False, indent=1)
    if len(draft) > max_chars:
        draft = draft[:max_chars] + "\n…（初稿过长，已截断）"

    extra = ""
    if instructions.strip():
        extra = (
            "\n## 用户的额外要求（优先级最高）\n"
            f"{instructions.strip()}\n"
        )

    return f"""下面是本地模型根据视频转写整理的**初稿 JSON**。请把它改成终稿。

视频：《{meta.display_title}》  UP主：{meta.owner_name or '未知'}

## 必须改成什么样

- 结构按「读者先想看什么」排：先让人一眼知道这篇能拿走什么，再给重点，最后才是脉络和概念；
- 去掉 AI 味：删掉「首先/其次/总而言之/值得注意的是/让我们/接下来我们」这类套话，
  删掉空洞的排比和过长的破折号串；
- 句子要有主谓、有具体动作。可以有一点作者口吻，但不要玩梗、不要抒情、不要夸张；
- **信息密度只增不减**：初稿里的具体数字、参数、步骤、例子都要留下，只改表达；
  只有同一件事在 summary、key_points、outline 里重复出现时才合并，不要为了顺口删掉细节。

## 绝对不能改的东西（违反任何一条，这次改写就作废）

1. **不得新增**视频里没有的事实、数字、结论、命令、链接、配置项。不确定就保留初稿的措辞；
2. **保留全部时间戳**：JSON 里的 `timestamp` 字段一个都不能少、不能改；
   正文里的 `[mm:ss]` 标记也要原样保留；
3. 代码块、命令、参数、提示词原文**逐字不动**；
4. 标了 `"source": "model"` 的条目是模型补充而非视频内容 —— 保留这个标注；
5. 不要删掉整节内容（可以把条目合并/精简，但不能让某节彻底空掉）；
6. 输出字段与输入**完全一致**：不同结构的 JSON 会被直接拒绝。

## 输出格式

只输出一个完整、合法的 JSON 对象（与输入同结构），不要 Markdown 围栏，不要任何解释。
{extra}
## 初稿

{draft}
"""


def validate_polish(draft: Note, polished: Note) -> str:
    """检查润色结果是否守住了硬约束。

    :returns: 不合格原因；空字符串表示合格。
    """
    reason = _note_needs_retry(polished)
    if reason:
        return f"润色稿不合格：{reason}"
    if _outline_timestamps_bad(polished):
        return "润色稿的大纲时间戳不可用"

    lost = _note_timestamps(draft) - _note_timestamps(polished)
    if lost:
        sample = "、".join(f"{s:.0f}s" for s in sorted(lost)[:3])
        return f"润色稿丢了 {len(lost)} 个时间戳（如 {sample}）"

    draft_marks = _note_clock_marks(draft)
    polished_marks = _note_clock_marks(polished)
    if polished_marks < draft_marks:
        return f"正文里的时间标记少了：初稿 {draft_marks} 个，润色稿 {polished_marks} 个"

    draft_chars = _note_char_count(draft)
    if draft_chars and _note_char_count(polished) < draft_chars * 0.6:
        return "润色稿比初稿短太多（可能删掉了实质内容）"

    return ""


async def polish_note(
    draft: Note,
    meta: VideoMeta,
    *,
    settings: Settings,
    provider: LLMProvider,
) -> Note | None:
    """让云端把本地初稿改一遍。

    **只调用一次**：JSON 不合法或校验不过就直接返回 ``None``，
    由调用方保留本地稿 —— 润色是加分项，不能因为润色失败把成稿弄丢。
    """
    prompt = build_polish_prompt(
        draft, meta, instructions=settings.polish_instructions
    )
    tokens = estimate_tokens(prompt)
    if tokens > settings.polish_max_input_tokens:
        logger.warning(
            "初稿约 %d tokens，超过润色上限 %d，跳过润色",
            tokens,
            settings.polish_max_input_tokens,
        )
        return None

    logger.info("云端润色开始（初稿约 %d tokens，只调用一次）", tokens)
    try:
        raw = await provider.complete_json(
            system=POLISH_SYSTEM,
            user=prompt,
            schema=None,
            temperature=settings.polish_temperature,
        )
        polished = to_note(extract_json(raw))
    except Exception as exc:  # noqa: BLE001 - 润色失败一律退回本地稿
        logger.warning("润色调用失败（%s），保留本地稿", exc)
        return None

    reason = validate_polish(draft, polished)
    if reason:
        logger.warning("润色结果没通过校验（%s），保留本地稿", reason)
        return None

    logger.info(
        "云端润色完成：时间戳 %d 个、时间标记 %d 个均保留",
        len(_note_timestamps(polished)),
        _note_clock_marks(polished),
    )
    return polished


__all__ = [
    "POLISH_SYSTEM",
    "build_polish_prompt",
    "polish_note",
    "validate_polish",
]
