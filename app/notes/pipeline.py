"""笔记生成流水线：单次生成 / 分块 map-reduce + 结构修复重试。

模型输出永远不完全可靠，所以这一层做了三件事：
  1. **宽容解析**：兼容字段名变体（``keyPoints`` / ``key_points`` / ``要点``）、
     类型漂移（时间戳写成 ``"01:23"``）、多余包裹；
  2. **修复重试**：解析失败时把错误输出回喂给模型要求重出（默认 1 次）；
  3. **薄笔记自检**：结果为空壳时换更严格的提示词重试。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

from app.config import Settings
from app.errors import LLMError, LLMOutputInvalid
from app.llm.base import (
    LLMProvider,
    clamp_source,
    coerce_float,
    coerce_str,
    coerce_str_list,
    extract_json,
)
from app.notes.chunking import Chunk, chunk_segments, estimate_tokens, transcript_to_text
from app.notes.prompts import (
    SYSTEM_PROMPT,
    PartialActionable,
    PartialConcept,
    PartialKeyPoint,
    PartialNotes,
    PartialOutline,
    build_focus_prompt,
    build_map_prompt,
    build_reduce_prompt,
    build_repair_prompt,
    build_single_prompt,
    flatten_schema,
    parse_focus,
)
from app.schemas import (
    Actionable,
    ActionableKind,
    Concept,
    FocusPlan,
    FocusPoint,
    FollowUp,
    KeyPoint,
    Note,
    OutlineItem,
    Transcript,
    Uncertainty,
    VideoMeta,
)

logger = logging.getLogger(__name__)

ProgressCb = Callable[[float, str], None] | None


# ---------------------------------------------------------------------------
# 宽容取值
# ---------------------------------------------------------------------------


def _pick(d: dict[str, Any], *names: str) -> Any:
    """从字典里按多个候选键名取值（大小写/下划线不敏感）。"""
    if not isinstance(d, dict):
        return None
    lowered = {str(k).lower().replace("_", "").replace("-", ""): v for k, v in d.items()}
    for name in names:
        key = name.lower().replace("_", "").replace("-", "")
        if key in lowered:
            return lowered[key]
    return None


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    return []


# ---------------------------------------------------------------------------
# 原始 dict -> 模型对象
# ---------------------------------------------------------------------------


def to_partial_notes(raw: Any) -> PartialNotes:
    """把模型输出规范成 :class:`PartialNotes`。"""
    d = _as_dict(raw)
    return PartialNotes(
        outline=[
            PartialOutline(
                timestamp=coerce_float(_pick(o, "timestamp", "time", "时间")) or 0.0,
                heading=coerce_str(_pick(o, "heading", "title", "标题")),
                summary=coerce_str(_pick(o, "summary", "detail", "描述", "内容")),
            )
            for o in _as_list(_pick(d, "outline", "大纲", "sections"))
            if isinstance(o, (dict, str))
        ],
        key_points=[
            PartialKeyPoint(
                title=coerce_str(_pick(k, "title", "point", "要点", "标题")),
                detail=coerce_str(_pick(k, "detail", "explanation", "说明", "详情")),
                source=clamp_source(_pick(k, "source", "来源")),
                timestamp=coerce_float(_pick(k, "timestamp", "time", "时间")),
            )
            for k in _as_list(_pick(d, "key_points", "keyPoints", "points", "要点"))
            if isinstance(k, dict)
        ],
        actionables=[
            PartialActionable(
                kind=coerce_str(_pick(a, "kind", "type", "类型")) or "tip",
                title=coerce_str(_pick(a, "title", "name", "标题")),
                content=coerce_str(
                    _pick(a, "content", "body", "text", "内容", "原文")
                ),
                note=coerce_str(_pick(a, "note", "说明", "备注")) or None,
                timestamp=coerce_float(_pick(a, "timestamp", "time", "时间")),
            )
            for a in _as_list(
                _pick(d, "actionables", "可用的东西", "actionable", "行动项")
            )
            if isinstance(a, dict)
        ],
        concepts=[
            PartialConcept(
                term=coerce_str(_pick(c, "term", "name", "概念", "名词")),
                explanation=coerce_str(
                    _pick(c, "explanation", "detail", "解释", "说明")
                ),
                source=clamp_source(_pick(c, "source", "来源")),
                example=coerce_str(_pick(c, "example", "例子", "举例")) or None,
                timestamp=coerce_float(_pick(c, "timestamp", "time", "时间")),
            )
            for c in _as_list(_pick(d, "concepts", "概念", "terms"))
            if isinstance(c, dict)
        ],
        uncertain=coerce_str_list(
            _pick(d, "uncertain", "uncertainties", "存疑", "doubts")
        ),
    )


#: 各节的条数上限。
#: 实测：模型在「存疑」上会写出 12 条，其中多条是同一件事的不同说法；
#: 在小节上话多不等于信息多，反而稀释重点，所以要有硬约束。
MAX_OUTLINE = 12
MAX_KEY_POINTS = 12
MAX_FOCUS_POINTS = 8
MAX_ACTIONABLES = 10
MAX_CONCEPTS = 8
MAX_FOLLOW_UPS = 5
MAX_UNCERTAINTIES = 5

#: 模型可能用这些词表示「这是提示词/命令/步骤」，统一收敛到枚举
_ACTIONABLE_ALIASES: dict[str, ActionableKind] = {
    "prompt": ActionableKind.PROMPT,
    "提示词": ActionableKind.PROMPT,
    "instruction": ActionableKind.PROMPT,
    "step": ActionableKind.STEP,
    "步骤": ActionableKind.STEP,
    "steps": ActionableKind.STEP,
    "operation": ActionableKind.STEP,
    "command": ActionableKind.COMMAND,
    "命令": ActionableKind.COMMAND,
    "code": ActionableKind.COMMAND,
    "代码": ActionableKind.COMMAND,
    "shell": ActionableKind.COMMAND,
    "config": ActionableKind.CONFIG,
    "配置": ActionableKind.CONFIG,
    "setting": ActionableKind.CONFIG,
    "checklist": ActionableKind.CHECKLIST,
    "清单": ActionableKind.CHECKLIST,
    "检查清单": ActionableKind.CHECKLIST,
    "template": ActionableKind.TEMPLATE,
    "模板": ActionableKind.TEMPLATE,
    "format": ActionableKind.TEMPLATE,
    "tip": ActionableKind.TIP,
    "技巧": ActionableKind.TIP,
    "建议": ActionableKind.TIP,
    "note": ActionableKind.TIP,
}


def coerce_actionable_kind(value: Any) -> ActionableKind:
    """把模型给的 kind 收敛到枚举。认不出来就当作「技巧」。"""
    key = coerce_str(value).strip().lower()
    return _ACTIONABLE_ALIASES.get(key, ActionableKind.TIP)


#: 可能出现在「标题：内容」之间的分隔符
_LABEL_SEPARATORS = ("：", ":", " - ", "—", "－")


def strip_label_prefix(content: str, title: str) -> str:
    """去掉 content 开头那个与 title 重复的「标题：」前缀。

    模型（尤其是 8B）非常爱把可复制内容写成这样::

        验证Git状态：git status

    读者复制下来还得手动删前缀。实测**在提示词里反复强调也改不掉**，
    所以这里做确定性的后处理。

    ⚠️ 只在**前缀确实等于标题**时才剥离。不能见冒号就切 ——
    提示词正文里本来就常有冒号（比如「请按以下格式输出：…」），
    一刀切会把有用的内容切掉。
    """
    if not content or not title:
        return content

    body = content.strip()
    for separator in _LABEL_SEPARATORS:
        prefix = title.strip() + separator
        if body.startswith(prefix):
            stripped = body[len(prefix) :].strip()
            # 前缀后面还得有东西才敢剥，否则会把内容剥成空
            if stripped:
                return stripped

    # 归一化后再比一次，覆盖「Git状态」vs「git 状态」这类细微差异
    for separator in _LABEL_SEPARATORS:
        head, found, tail = body.partition(separator)
        if not found:
            continue
        if len(head) > 40:  # 太长的「标题」其实是正文
            break
        if _norm(head) == _norm(title) and tail.strip():
            return tail.strip()
    return content

_PUNCT_RE = re.compile(r"[\s·、，,。.:：;；!！?？\-—_()（）\[\]【】\"'“”‘’/\\|]+")


def _norm(text: str) -> str:
    """归一化去重用的键：去标点空白、转小写。"""
    return _PUNCT_RE.sub("", (text or "").lower())


def _dedup(items: list[Any], key_fn: Callable[[Any], str], limit: int) -> list[Any]:
    """按归一化键去重，并截断到上限。保留首次出现的顺序。"""
    seen: set[str] = set()
    out: list[Any] = []
    for item in items:
        key = key_fn(item)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= limit:
            break
    return out


def _outline_timestamps_bad(note: Note) -> bool:
    """大纲时间戳是否不可用。

    两种实测到的症状：

    1. **全部相同** —— 模型偷懒时把每个章节都标成 00:00；
    2. **没有递增** —— 出现过 ``[16:13]`` 排在 ``[11:48]`` 之后。
       （有时它又能准确抄出 02:23 / 05:46 这类真实位置，不稳定。）

    时间戳是「跳回原视频核对」的唯一入口，坏了整篇笔记的可核对性就没了，
    所以发现异常就重试一次。
    """
    stamps = [round(o.timestamp, 1) for o in note.outline]
    if len(stamps) < 3:
        return False
    if len(set(stamps)) == 1:
        return True
    return any(later < earlier for earlier, later in zip(stamps, stamps[1:]))


#: 画面文字块：``(时间戳秒, 文本)``。
#: 用朴素元组而不是 vision 的类型，避免 notes 层反向依赖 vision 层。
ScreenBlock = tuple[float, str]


def slice_screen(
    blocks: list[ScreenBlock],
    start: float,
    end: float,
    *,
    max_chars: int = 3000,
) -> str:
    """取出指定时间范围内的画面文字。

    为什么要切片：整段视频的画面文字可能有上万字，
    给每一块都塞全文既浪费上下文，又会让模型分不清「这块讲了什么」。
    """
    picked = [text for ts, text in blocks if start <= ts <= end]
    if not picked:
        return ""
    joined = "\n\n".join(picked)
    if len(joined) > max_chars:
        joined = joined[:max_chars] + "\n…（该段画面文字过长已截断）"
    return joined


def render_screen_all(blocks: list[ScreenBlock], *, max_chars: int = 6000) -> str:
    """把全部画面文字渲染成一段文本（单次生成 / 汇总时用）。"""
    if not blocks:
        return ""
    joined = "\n\n".join(text for _ts, text in blocks)
    if len(joined) > max_chars:
        joined = joined[:max_chars] + "\n…（画面文字过长已截断）"
    return joined


def _note_needs_retry(note: Note) -> str:
    """笔记是否需要重试，返回原因（空串表示不用）。

    **把所有问题一次性列全**，而不是发现一个就返回 ——
    重试提示词里告诉模型的问题越完整，一次重试就越可能合格。
    """
    reasons: list[str] = []

    if note.is_thin():
        reasons.append("内容过少（缺少 summary 和 key_points）")

    if not note.has_substance():
        reasons.append(
            "没有产出「重点」（focus_points）和「能直接用的东西」（actionables）。"
            "这两节是读者真正要的东西 —— 请把视频里的具体做法、"
            "提示词原文、命令、步骤抓出来，不要只写「视频介绍了什么」"
        )

    if _outline_timestamps_bad(note):
        reasons.append(
            "大纲时间戳不可用（全部相同，或者没有按时间递增）—— "
            "你没有按要求去转写里找 [mm:ss] 标记。"
            "请逐条重新定位：每条时间戳应各不相同，且按时间从早到晚排列"
        )

    return "；".join(reasons)


def to_note(raw: Any) -> Note:
    """把模型输出规范成 :class:`Note`。"""
    d = _as_dict(raw)

    outline: list[OutlineItem] = []
    for o in _as_list(_pick(d, "outline", "大纲", "sections")):
        if isinstance(o, str):
            outline.append(OutlineItem( timestamp=0.0, heading=o, summary=""))
            continue
        if not isinstance(o, dict):
            continue
        heading = coerce_str(_pick(o, "heading", "title", "标题"))
        summary = coerce_str(_pick(o, "summary", "detail", "描述"))
        if not heading and not summary:
            continue
        outline.append(
            OutlineItem(
                timestamp=coerce_float(_pick(o, "timestamp", "time", "时间")) or 0.0,
                heading=heading or summary[:30],
                summary=summary,
            )
        )

    key_points: list[KeyPoint] = []
    for k in _as_list(_pick(d, "key_points", "keyPoints", "points", "要点")):
        if isinstance(k, str):
            key_points.append(KeyPoint(title=k, detail="", source="video"))
            continue
        if not isinstance(k, dict):
            continue
        title = coerce_str(_pick(k, "title", "point", "要点", "标题"))
        detail = coerce_str(_pick(k, "detail", "explanation", "说明", "详情"))
        if not title and not detail:
            continue
        key_points.append(
            KeyPoint(
                title=title or detail[:40],
                detail=detail,
                source=clamp_source(_pick(k, "source", "来源")),
                timestamp=coerce_float(_pick(k, "timestamp", "time", "时间")),
            )
        )

    concepts: list[Concept] = []
    for c in _as_list(_pick(d, "concepts", "概念", "terms")):
        if isinstance(c, str):
            concepts.append(Concept(term=c, explanation="", source="video"))
            continue
        if not isinstance(c, dict):
            continue
        term = coerce_str(_pick(c, "term", "name", "概念", "名词"))
        if not term:
            continue
        concepts.append(
            Concept(
                term=term,
                explanation=coerce_str(
                    _pick(c, "explanation", "detail", "解释", "说明")
                ),
                source=clamp_source(_pick(c, "source", "来源")),
                example=coerce_str(_pick(c, "example", "例子", "举例")) or None,
                timestamp=coerce_float(_pick(c, "timestamp", "time", "时间")),
            )
        )

    # ---- 重点：冲着标题来的人必须带走的几件事（排在最前面）----
    focus_points: list[FocusPoint] = []
    for f in _as_list(
        _pick(d, "focus_points", "focusPoints", "重点", "focus", "应重点抓取")
    ):
        if isinstance(f, str):
            focus_points.append(FocusPoint(title=f, detail=""))
            continue
        if not isinstance(f, dict):
            continue
        title = coerce_str(_pick(f, "title", "point", "重点", "标题"))
        detail = coerce_str(
            _pick(f, "detail", "explanation", "说明", "详情", "内容")
        )
        if not title and not detail:
            continue
        focus_points.append(
            FocusPoint(
                title=title or detail[:40],
                detail=detail,
                timestamp=coerce_float(_pick(f, "timestamp", "time", "时间")),
            )
        )

    # ---- 行动项：提示词、命令、步骤这些能直接复制的东西（最有价值）----
    actionables: list[Actionable] = []
    for a in _as_list(
        _pick(d, "actionables", "actionable", "可用的东西", "行动项", "能直接用的")
    ):
        if isinstance(a, str):
            actionables.append(
                Actionable(kind=ActionableKind.TIP, title=a, content=a)
            )
            continue
        if not isinstance(a, dict):
            continue
        title = coerce_str(_pick(a, "title", "name", "标题"))
        content = coerce_str(
            _pick(a, "content", "body", "text", "内容", "原文", "prompt")
        )
        if not title and not content:
            continue
        # 模型爱把 content 写成「标题：内容」，把重复的前缀剥掉
        content = strip_label_prefix(content, title)
        actionables.append(
            Actionable(
                kind=coerce_actionable_kind(_pick(a, "kind", "type", "类型")),
                title=title or content[:40],
                content=content or title,
                note=coerce_str(_pick(a, "note", "说明", "备注")) or None,
                timestamp=coerce_float(_pick(a, "timestamp", "time", "时间")),
            )
        )

    follow_ups: list[FollowUp] = []
    for s in _as_list(
        _pick(
            d,
            "follow_ups",
            "followUps",
            "待查",
            "待查清单",
            "补充",
            "补充背景",
            "supplements",
            "background",
        )
    ):
        if isinstance(s, str):
            follow_ups.append(FollowUp(question=s, why=""))
            continue
        if not isinstance(s, dict):
            continue
        question = coerce_str(
            _pick(s, "question", "topic", "title", "问题", "主题", "标题")
        )
        why = coerce_str(_pick(s, "why", "reason", "为什么", "说明"))
        where = (
            coerce_str(_pick(s, "where", "search", "keyword", "查询", "去哪查"))
            or None
        )
        if not question and not why:
            continue
        follow_ups.append(
            FollowUp(
                question=question or why[:40],
                why=why,
                where=where,
            )
        )

    uncertainties: list[Uncertainty] = []
    raw_unc = _pick(d, "uncertainties", "uncertain", "存疑", "doubts")
    for u in _as_list(raw_unc):
        if isinstance(u, str):
            uncertainties.append(Uncertainty(item=u, reason=""))
        elif isinstance(u, dict):
            item = coerce_str(_pick(u, "item", "topic", "内容", "问题"))
            reason = coerce_str(_pick(u, "reason", "why", "原因", "说明"))
            if item or reason:
                uncertainties.append(
                    Uncertainty(item=item or reason[:40], reason=reason)
                )

    # ---- 去重与限量 ----
    # 提示词里写了「3-10 条」，但实测 56 分钟的视频模型给出了 22 条大纲，
    # 条数失控会稀释重点，所以这里做硬约束。
    outline = _dedup(outline, lambda o: _norm(o.heading), MAX_OUTLINE)
    focus_points = _dedup(focus_points, lambda f: _norm(f.title), MAX_FOCUS_POINTS)
    actionables = _dedup(actionables, lambda a: _norm(a.title), MAX_ACTIONABLES)
    key_points = _dedup(key_points, lambda k: _norm(k.title), MAX_KEY_POINTS)
    concepts = _dedup(concepts, lambda c: _norm(c.term), MAX_CONCEPTS)
    follow_ups = _dedup(follow_ups, lambda f: _norm(f.question), MAX_FOLLOW_UPS)
    uncertainties = _dedup(
        uncertainties, lambda u: _norm(u.item), MAX_UNCERTAINTIES
    )

    return Note(
        title=coerce_str(_pick(d, "title", "标题")),
        goal=coerce_str(_pick(d, "goal", "目标", "读者目标")),
        one_liner=coerce_str(_pick(d, "one_liner", "oneLiner", "总结", "一句话总结")),
        summary=coerce_str(_pick(d, "summary", "摘要", "概述")),
        focus_points=focus_points,
        actionables=actionables,
        outline=outline,
        key_points=key_points,
        concepts=concepts,
        follow_ups=follow_ups,
        uncertainties=uncertainties,
        tags=coerce_str_list(_pick(d, "tags", "标签")),
    )


# ---------------------------------------------------------------------------
# 生成器
# ---------------------------------------------------------------------------


class NoteGenerator:
    """把转写变成结构化笔记。"""

    def __init__(self, provider: LLMProvider, settings: Settings) -> None:
        self.provider = provider
        self.settings = settings
        #: 记录本次生成实际用了几次模型调用，便于性能评估
        self.call_count = 0

    # ------------------------------------------------------------------
    async def _ask(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any] | None,
        temperature: float = 0.2,
    ) -> str:
        self.call_count += 1
        return await self.provider.complete_json(
            system=system, user=user, schema=schema, temperature=temperature
        )

    async def _call_json(
        self,
        *,
        user: str,
        schema: dict[str, Any] | None,
        temperature: float = 0.2,
        repair_attempts: int = 1,
    ) -> Any:
        """调用模型并解析 JSON，失败则回喂错误重试。"""
        system = SYSTEM_PROMPT
        raw = await self._ask(
            system=system, user=user, schema=schema, temperature=temperature
        )
        try:
            return extract_json(raw)
        except LLMOutputInvalid as first_err:
            if repair_attempts <= 0:
                raise
            logger.warning("模型输出不是合法 JSON，尝试修复重试：%s", first_err)
            repair_user = build_repair_prompt(user, raw)
            fixed = await self._ask(
                system=system,
                user=repair_user,
                schema=schema,
                temperature=0.0,
            )
            try:
                return extract_json(fixed)
            except LLMOutputInvalid as second_err:
                raise LLMOutputInvalid(
                    f"模型两次都没能给出合法 JSON。首次：{first_err}；"
                    f"重试：{second_err}"
                ) from second_err

    # ------------------------------------------------------------------
    @property
    def _single_pass_char_budget(self) -> int:
        """单次生成能塞进多少字。取分块上限的 3 倍，超过就走 map-reduce。"""
        return max(self.settings.llm_chunk_chars * 3, 3000)

    @property
    def _reduce_budget(self) -> int:
        """汇总提示词的 token 预算。

        留出一半上下文给**输出**：一份完整笔记（大纲 + 要点 + 概念 +
        待查 + 存疑）轻易上千 token，把窗口塞满就没地方生成了。
        """
        return max(1500, int(self.settings.llm_num_ctx * 0.45))

    def _split_by_budget(
        self,
        partials: list[PartialNotes],
        budget: int,
        meta: VideoMeta,
        source_label: str,
        focus: FocusPlan | None = None,
    ) -> list[list[PartialNotes]]:
        """把中间结果切成若干批，使每批的提示词都在预算之内。

        单条就超预算时也会自成一批 —— 否则会切不动、陷入死循环。
        """
        groups: list[list[PartialNotes]] = []
        current: list[PartialNotes] = []

        for item in partials:
            candidate = [*current, item]
            size = estimate_tokens(
                build_reduce_prompt(
                    candidate,
                    meta,
                    transcript_source_label=source_label,
                    focus=focus,
                    compact=True,
                )
            )
            if current and size > budget:
                groups.append(current)
                current = [item]
            else:
                current = candidate

        if current:
            groups.append(current)
        return groups

    async def _derive_focus(self, meta: VideoMeta) -> FocusPlan:
        """第一步：推断「读者要什么、该抓什么」。

        只用视频元信息（标题/简介/标签/章节），**不看正文** —— 所以很快，
        而且不受转写质量影响。用户明确要求「通过视频标题来获取需要抓取的重点」，
        这个方法的产出会贯穿后续所有环节（map 每块、reduce 汇总）。

        失败不致命：拿不到就返回空计划，笔记照常生成，
        只是少了这层「围绕读者目标」的引导。
        """
        try:
            raw = await self._call_json(
                user=build_focus_prompt(meta),
                schema=flatten_schema(FocusPlan),
                temperature=0.0,
            )
            # ⚠️ parse_focus 必须在 try 里面 —— 它也可能因模型输出畸形而抛异常
            plan = parse_focus(raw)
        except Exception as exc:  # noqa: BLE001 - 锦上添花的一步
            logger.warning("推断读者目标失败（不影响生成，只是少一层引导）：%s", exc)
            return FocusPlan()
        if plan.focus or plan.audience_goal:
            logger.info(
                "读者目标：%s ｜ 关注点 %d 条 ｜ 需逐字抓取 %d 项",
                plan.audience_goal or "（未给出）",
                len(plan.focus),
                len(plan.must_extract),
            )
        return plan

    async def generate(
        self,
        transcript: Transcript,
        meta: VideoMeta,
        *,
        on_screen: list[ScreenBlock] | None = None,
        on_progress: ProgressCb = None,
    ) -> Note:
        """生成笔记。短视频一次搞定，长视频走 map-reduce。

        :param on_screen: 画面上的文字（OCR 得到），音频里没有的信息。
            教程类视频的提示词/代码/命令往往只在这里，是抓 actionables 的关键。
        """
        if transcript.is_empty():
            raise LLMError("转写内容为空，无法生成笔记")

        screen = list(on_screen or [])
        text = transcript_to_text(transcript.segments, with_timestamps=True)
        source_label = transcript.source.label

        def progress(frac: float, msg: str) -> None:
            if on_progress:
                on_progress(max(0.0, min(1.0, frac)), msg)

        # ---- 第 0 步：先想清楚读者要什么 ----
        # 这一步决定了笔记是「按读者目标抓重点」还是「按时间顺序复述」。
        # 不做这一步时，模型会写出一篇什么都没漏、但什么都没重点的笔记。
        progress(0.05, "正在从标题和简介判断该抓什么重点…")
        focus = await self._derive_focus(meta)

        if screen:
            logger.info("本次生成会带上 %d 段画面文字", len(screen))

        # ---- 短视频：一次生成 ----
        if len(text) <= self._single_pass_char_budget:
            progress(0.25, "正在生成笔记…")
            note = await self._single_pass(
                text, meta, source_label, focus, render_screen_all(screen)
            )
            progress(1.0, "笔记生成完成")
            return note

        # ---- 长视频：map-reduce ----
        return await self._map_reduce(
            text, transcript, meta, source_label, progress, focus, screen
        )

    async def _single_pass(
        self,
        text: str,
        meta: VideoMeta,
        source_label: str,
        focus: FocusPlan,
        screen_text: str = "",
    ) -> Note:
        schema = flatten_schema(Note)
        user = build_single_prompt(
            text,
            meta,
            transcript_source_label=source_label,
            focus=focus,
            screen_text=screen_text,
        )

        # temperature=0：笔记是事实整理任务，不需要创造性。
        # 实测 0.2 时同一提示词两次产出差异明显 —— 幻觉时有时无、
        # 时间戳时对时错，这对「把知识记准」是致命的。
        raw = await self._call_json(user=user, schema=schema, temperature=0.0)
        note = to_note(raw)

        reason = _note_needs_retry(note)
        if reason:
            logger.warning("单次生成结果不合格（%s），重试一次", reason)
            raw = await self._call_json(
                user=(
                    f"{user}\n\n⚠️ 上一次的输出有以下问题：{reason}\n"
                    "请重新输出完整且合格的 JSON。"
                ),
                schema=schema,
                temperature=0.0,
            )
            retried = to_note(raw)
            if not _note_needs_retry(retried):
                note = retried
            else:
                logger.warning("重试后仍不合格，保留首次结果")
        return note

    async def _map_reduce(
        self,
        text: str,
        transcript: Transcript,
        meta: VideoMeta,
        source_label: str,
        progress: Callable[[float, str], None],
        focus: FocusPlan,
        screen: list[ScreenBlock] | None = None,
    ) -> Note:
        chunks: list[Chunk] = chunk_segments(
            transcript.segments,
            max_chars=self.settings.llm_chunk_chars,
            overlap_chars=self.settings.llm_chunk_overlap_chars,
        )
        if not chunks:
            raise LLMError("分块后没有可用的转写内容")

        total = len(chunks)
        logger.info(
            "转写共 %d 字，切成 %d 块（单块上限 %d 字）",
            len(text),
            total,
            self.settings.llm_chunk_chars,
        )

        # ---- map ----
        partial_schema = flatten_schema(PartialNotes)
        partials: list[PartialNotes] = []
        for i, chunk in enumerate(chunks):
            progress(
                0.10 + 0.60 * (i / total),
                f"正在理解第 {i + 1}/{total} 段内容…",
            )
            raw = await self._call_json(
                # 每块都带上「读者要什么」—— 否则各块各抽各的，
                # 汇总时会得到一堆与读者目标无关的碎片。
                # 画面文字按时间切片，只给这一段范围内的。
                user=build_map_prompt(
                    chunk,
                    meta,
                    total,
                    focus=focus,
                    screen_text=slice_screen(screen or [], chunk.start, chunk.end),
                ),
                schema=partial_schema,
                temperature=0.0,
            )
            partials.append(to_partial_notes(raw))

        usable = [
            p
            for p in partials
            if p.key_points or p.outline or p.concepts or p.actionables
        ]
        if not usable:
            raise LLMError(
                f"{total} 段内容都没有抽取出有效信息，可能是转写质量太差或模型能力不足。"
                "建议换更大的模型（如 qwen3:14b）后重试。"
            )

        # ---- 中间归并：按**体量**判断，而不是按条数 ----
        #
        # ⚠️ 实测教训：只按条数设上限（比如 12 条）远远不够 ——
        # 8 个分段的中间结果拼起来就可能超过上下文窗口，
        # 模型随即陷入异常并一直跑到超时（56 分钟的视频就这样失败过）。
        # 所以改成估算提示词 token 数，超预算就先归并一轮。
        budget = min(self.settings.llm_num_ctx // 2, max(1500, self._reduce_budget))
        merge_round = 0

        while len(usable) > 1:
            size = estimate_tokens(
                build_reduce_prompt(
                    usable,
                    meta,
                    transcript_source_label=source_label,
                    focus=focus,
                )
            )
            if size <= budget:
                break

            merge_round += 1
            if merge_round > 6:
                logger.warning("归并轮次过多，停止归并以免死循环")
                break

            progress(
                0.7,
                f"中间结果较长（约 {size} tokens），先归并第 {merge_round} 轮…",
            )
            logger.info(
                "中间结果约 %d tokens 超出预算 %d，触发第 %d 轮归并",
                size,
                budget,
                merge_round,
            )

            groups = self._split_by_budget(
                usable, budget, meta, source_label, focus
            )
            merged: list[PartialNotes] = []
            for batch in groups:
                if len(batch) == 1:
                    merged.append(batch[0])
                    continue
                raw = await self._call_json(
                    user=build_reduce_prompt(
                        batch,
                        meta,
                        transcript_source_label=source_label,
                        focus=focus,
                        compact=True,
                    ),
                    schema=partial_schema,
                    temperature=0.0,
                )
                merged.append(to_partial_notes(raw))

            if len(merged) >= len(usable):
                logger.warning("归并没能减少条目数，停止归并")
                break
            usable = merged

        # ---- reduce ----
        progress(0.85, "正在整合成完整笔记…")
        schema = flatten_schema(Note)
        user = build_reduce_prompt(
            usable,
            meta,
            transcript_source_label=source_label,
            focus=focus,
            screen_text=render_screen_all(screen or []),
        )
        raw = await self._call_json(user=user, schema=schema, temperature=0.0)
        note = to_note(raw)

        reason = _note_needs_retry(note)
        if reason:
            logger.warning("汇总结果不合格（%s），重试一次", reason)
            raw = await self._call_json(
                user=(
                    f"{user}\n\n⚠️ 上一次的输出有以下问题：{reason}\n"
                    "请重新输出完整且合格的 JSON。"
                ),
                schema=schema,
                temperature=0.0,
            )
            retried = to_note(raw)
            if not _note_needs_retry(retried):
                note = retried
            else:
                logger.warning("重试后仍不合格，保留首次结果")

        # 把各段存疑点合并进来，避免汇总时丢信息。
        # ⚠️ 必须**合并后重新限量** —— 之前漏了这一步，
        # 结果 8 个分段各塞 2 条，最终产出 19 条存疑，把上限绕过去了。
        seen = {_norm(u.item) for u in note.uncertainties}
        for p in usable:
            for u in p.uncertain[:2]:
                key = _norm(u)
                if u and key and key not in seen:
                    note.uncertainties.append(
                        Uncertainty(item=u, reason="来自分段理解")
                    )
                    seen.add(key)
        note.uncertainties = note.uncertainties[:MAX_UNCERTAINTIES]

        progress(1.0, "笔记生成完成")
        return note
