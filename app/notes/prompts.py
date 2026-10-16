"""提示词与 LLM 输出契约。

## 设计要点

**1. 先想清楚「读者要什么」，再组织笔记。**

实测教训（用户反馈）：直接让模型「总结这个视频」，它会按**时间顺序复述**，
产出一篇什么都没漏、但什么重点都没有的笔记。
用户的原话是「我要的是怎么让 agent 干活、提示词怎么写，但笔记做出来的并不是」。

所以现在分两步：先从标题/简介/标签/章节推断出
「受众目标 + 该抓什么 + 可以忽略什么」（:func:`build_focus_prompt`），
再带着这份判断去组织笔记，让它围绕**读者要的东西**展开。

**2. 不许编造，拿不准就写进 uncertainties。**

**3. 分清来源**：视频里讲过的标 ``video``，模型自己补的标 ``model``。

**4. 时间戳必须原样抄写 ``[mm:ss]`` 标记**，不许自己换算或编造。

**5. 刻意不产出转写全文** —— 复述整段音频没有价值，篇幅应该让给重点。
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from app.notes.chunking import Chunk
from app.schemas import FocusPlan, VideoMeta

# ---------------------------------------------------------------------------
# 系统提示词
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """你是一位极其擅长「把视频里的东西变成读者能直接用上的笔记」的整理者。

你的读者是：**冲着这个视频标题点进来、想拿到具体做法的人。**

你必须遵守的规则：

1. **先问「读者要什么」，再决定写什么。**
   不要按视频的讲述顺序复述。读者要的是「怎么做」，不是「视频讲了什么」。

2. **把可用的东西逐字抓下来。**
   视频里出现过的提示词、命令、代码、参数取值、配置项、操作顺序 ——
   这些是笔记里最值钱的部分，必须原样保留，不要改写成概述。
   （提示词和代码尤其容易在转述中丢失精度。）

3. **只依据给定材料**。不要引入材料里没有的事实、数字、结论。
   拿不准的写进 uncertainties，不要猜。

4. **补充背景要明确标出来**。视频里讲过或可直接推出的标 "video"，
   你自己的补充标 "model"。术语解释只能依据材料，**不要补外部事实**。

5. **时间戳只能原样抄写材料里的 [mm:ss] 标记**，不要换算成秒数、不要编造。

6. **废话是失败的标志**。「介绍了…」「提到了…」「讲解了…」这类复述
   没有任何价值。每一条都要能让读者动手做点事情，或者理解一个具体机制。

7. 转写可能有同音错字（人名、型号、术语尤其容易错）。
   影响理解时在 uncertainties 里说明，并尽量从上下文推断原意。

输出必须是**合法 JSON**，不要任何解释性文字、不要 markdown 代码块包裹。"""


# ---------------------------------------------------------------------------
# 中间产物结构（map 阶段）
# ---------------------------------------------------------------------------


class PartialOutline(BaseModel):
    timestamp: float = 0.0
    heading: str = ""
    summary: str = ""


class PartialKeyPoint(BaseModel):
    title: str = ""
    detail: str = ""
    source: str = "video"
    timestamp: float | None = None


class PartialConcept(BaseModel):
    term: str = ""
    explanation: str = ""
    source: str = "video"
    example: str | None = None
    timestamp: float | None = None


class PartialActionable(BaseModel):
    kind: str = "tip"
    title: str = ""
    content: str = ""
    note: str | None = None
    timestamp: float | None = None


class PartialNotes(BaseModel):
    """单块转写的抽取结果（map 阶段输出）。"""

    outline: list[PartialOutline] = []
    key_points: list[PartialKeyPoint] = []
    actionables: list[PartialActionable] = []
    concepts: list[PartialConcept] = []
    uncertain: list[str] = []


# ---------------------------------------------------------------------------
# $ref 展平（提升 Ollama 结构化输出的可靠性）
# ---------------------------------------------------------------------------

#: 纯注释/元数据键：对生成无帮助，去掉可省 token 也能减少模型分心。
#:
#: ⚠️ 这些键只在 **schema 关键字位置** 剔除。``properties`` 下面是
#: 「属性名 -> 子 schema」的映射，属性名**绝不能过滤** ——
#: 否则一个叫 ``title`` 的字段会被整条删掉（曾经真的踩过这个坑）。
_SCHEMA_ANNOTATION_KEYS = frozenset(
    {"$defs", "$schema", "title", "description", "default", "examples"}
)

#: 字段级 schema 覆盖：让模型用**它擅长的形式**回答，换算交给我们自己做。
#:
#: ``timestamp`` 的实测教训：要求模型把 ``[mm:ss]`` 换算成秒数时，
#: 它会给出 2/3/8/12/15 这种与原文完全对不上的小数字
#: （20 分钟的视频，大纲时间点全落在前 30 秒）。
#: 而「原样抄写这个标记」是模型擅长得多的任务。
_SCHEMA_OVERRIDES: dict[str, dict[str, Any]] = {
    "timestamp": {
        "anyOf": [{"type": "string"}, {"type": "null"}],
        "description": (
            '原样沿用转写里的 [mm:ss] 标记，例如 "12:34"。'
            "不要换算成秒数，不要自己编。找不到对应标记就填 null。"
        ),
    },
}


def _allows_null(schema: Any) -> bool:
    """这个字段是否允许为 null（即本质上是可选的）。"""
    if not isinstance(schema, dict):
        return False
    if schema.get("type") == "null":
        return True
    for sub in schema.get("anyOf") or []:
        if isinstance(sub, dict) and sub.get("type") == "null":
            return True
    return False


def flatten_schema(model: type[BaseModel]) -> dict[str, Any]:
    """把 pydantic 的 ``$defs``/``$ref`` 展平成自包含 schema。

    Ollama 的 ``format`` 参数对 ``$ref`` 支持不稳定，展平后约束更硬。
    同时剔除对生成无用的注释性键，并把必填项收紧到**非空字段**，
    避免模型「偷懒」省略小节。
    """
    raw = model.model_json_schema()
    defs = raw.get("$defs", {})

    def resolve(node: Any, depth: int = 0) -> Any:
        if depth > 20:  # 防御递归 schema
            return {"type": "string"}
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                name = ref.rsplit("/", 1)[-1]
                target = defs.get(name)
                if target is None:
                    return {"type": "string"}
                merged = {k: v for k, v in node.items() if k != "$ref"}
                resolved = resolve(target, depth + 1)
                if isinstance(resolved, dict) and merged:
                    resolved = {**resolved, **merged}
                return resolved

            out: dict[str, Any] = {}
            for key, value in node.items():
                if key in _SCHEMA_ANNOTATION_KEYS:
                    continue
                if key == "properties" and isinstance(value, dict):
                    # 属性名必须原样保留，只对其 schema 递归
                    out["properties"] = {
                        prop: (
                            dict(_SCHEMA_OVERRIDES[prop])
                            if prop in _SCHEMA_OVERRIDES
                            else resolve(sub, depth + 1)
                        )
                        for prop, sub in value.items()
                    }
                else:
                    out[key] = resolve(value, depth + 1)

            # 收紧对象约束，减少模型输出多余字段。
            #
            # ⚠️ 只把**非空字段**设为必填。曾经把全部字段都设为必填，
            # 结果模型对 example / timestamp 这类本来可以留空的字段
            # 无话可说，只好复读术语凑数。
            if out.get("type") == "object" and isinstance(out.get("properties"), dict):
                out.setdefault("additionalProperties", False)
                out["required"] = [
                    prop
                    for prop, sub in out["properties"].items()
                    if not _allows_null(sub)
                ]
            return out

        if isinstance(node, list):
            return [resolve(x, depth) for x in node]
        return node

    result = resolve(raw)
    return result if isinstance(result, dict) else {"type": "object"}


# ---------------------------------------------------------------------------
# 视频元信息（所有提示词共用）
# ---------------------------------------------------------------------------


def build_meta_header(meta: VideoMeta, *, max_desc_chars: int = 1200) -> str:
    """把视频的**非音频信号**整理成提示词头部。

    这些信息全都不是从音频里来的，但对「抓重点」极有价值：

    * **标题** —— 用户最直接的意图来源（用户明确要求用它抓重点）；
    * **简介** —— 教程类视频常把大纲、链接、甚至提示词写在这里；
    * **标签** —— 主题词，帮助判断领域；
    * **章节** —— B站 AI 划分（未登录可得），相当于现成的主干结构。
    """
    lines = [
        f"视频标题：{meta.display_title}",
        f"UP主：{meta.owner_name or '未知'}",
        f"总时长：{_clock(meta.duration)}",
    ]

    if meta.tags:
        lines.append(f"标签：{'、'.join(meta.tags[:15])}")

    if meta.desc.strip():
        desc = meta.desc.strip()
        if len(desc) > max_desc_chars:
            desc = desc[:max_desc_chars] + "…（简介过长已截断）"
        lines.append(f"视频简介：\n{desc}")

    if meta.chapters:
        lines.append("B站已标好的章节（视频主干）：")
        lines.extend(
            f"  [{c.clock()}] {c.title}" for c in meta.chapters[:20]
        )

    return "\n".join(lines)


def build_screen_section(on_screen_text: str, *, max_chars: int = 6000) -> str:
    """把「画面上的文字」整理成提示词片段。

    教程类视频里，提示词、代码、终端输出往往**只出现在画面上**，
    音频里听不到或者听错（实测把 ``git init`` 听成了 ``Gate``）。
    这一段是音频转写之外的补充信息源。
    """
    text = (on_screen_text or "").strip()
    if not text:
        return ""
    if len(text) > max_chars:
        text = text[:max_chars] + "\n…（画面文字过长已截断）"

    return (
        "===== 画面上的文字（从视频画面 OCR 得到，音频里没有）=====\n"
        "⚠️ 这些是**屏幕上显示的内容**，通常是提示词原文、代码、命令、"
        "标题或列表。它们比音频转写准确（不受同音字影响），"
        "抓 actionables 时应优先采用这一部分的原文。\n\n"
        f"{text}\n"
    )


# ---------------------------------------------------------------------------
# 第一步：推断「读者要什么」
# ---------------------------------------------------------------------------

FOCUS_INSTRUCTIONS = """你在帮用户决定：**这个视频的笔记该怎么记。**

现在只有视频的元信息（还没有正文）。请先判断读者到底要什么 ——
后面生成笔记时会带着你的判断去做，所以这一步很关键。

请输出：

- **video_kind**：视频类型。例如：操作教程 / 工具评测 / 知识解说 /
  经验分享 / 产品发布 / 访谈对谈 / 时事评论。

- **audience_goal**：**冲着这个标题点进来的人，最想得到什么？**
  一句话说清，要具体。
  好例子：「学会写能让 agent 稳定干活的提示词」
  坏例子：「了解 agent 相关知识」（太泛，等于没说）

- **focus**：3-6 个关注点，**按重要性排序**。笔记会围绕它们组织。
  每条要具体到一个能落地的方向，而不是一个主题词。
  好例子：「提示词里必须写清角色、任务边界和输出格式」
  坏例子：「提示词技巧」

- **must_extract**：这类视频里**必须逐字抓下来**的东西。
  这是最容易被泛泛总结冲掉的部分，请根据视频类型判断：
  · 操作教程 → 提示词原文、命令、参数取值、操作顺序
  · 工具评测 → 价格、型号、实测数据、优缺点
  · 知识解说 → 因果关系、关键数据、时间线
  · 经验分享 → 具体做法、判断标准、踩过的坑

- **ignore**：可以忽略的内容。开场寒暄、抽奖口播、求三连、
  与主题无关的闲聊 —— 这些不该占笔记篇幅。

只输出 JSON。"""


def build_focus_prompt(meta: VideoMeta) -> str:
    """构造「推断读者目标」的提示词（只用元信息，不看正文）。"""
    return f"{FOCUS_INSTRUCTIONS}\n\n===== 视频元信息 =====\n{build_meta_header(meta)}"


# ---------------------------------------------------------------------------
# map：逐块抽取
# ---------------------------------------------------------------------------

MAP_INSTRUCTIONS = """请从下面这段视频转写中抽取结构化信息。

要求：
- outline：这一段的脉络，2-6 条，每条带 heading（小标题）和 summary（一句话）。
- key_points：值得记下来的要点，1-5 条。detail 要写清楚，不要只写标题。
- **actionables**：**这段里出现的、可以原样拿来用的东西**，0-5 条。
  · kind 取值：prompt / step / command / config / checklist / template / tip
  · content 必须是**能直接复制的原文**（提示词、命令、代码、参数、步骤清单）。
    视频里没出现就留空，**不要自己编**。
  · ⛔ content 里**不要再加标题或「XXX：」这类前缀** ——
    title 字段已经承担了说明作用。读者要的是复制下来就能直接用的原文，
    多一个前缀就得手动删。例如 title 是「验证Git状态」时，
    content 应该就是 `git status`，而不是 `验证Git状态：git status`。
  · 这一段里如果只是**讲解、评价、闲聊**，没有可复制的东西，就留空数组。
- concepts：出现的关键概念/术语，0-4 条。explanation 用大白话，配一个 example。
- uncertain：转写不清、疑似错字、前后矛盾的地方，0-3 条，用一句话说明。

时间戳规则（很重要）：
**原样抄写**转写里的 [mm:ss] 标记，例如 "12:34"。
不要换算成秒数，不要自己编造时间点。找不到对应标记就填 null。
只输出 JSON。"""


def build_map_prompt(
    chunk: Chunk,
    meta: VideoMeta,
    total: int,
    *,
    focus: FocusPlan | None = None,
    screen_text: str = "",
) -> str:
    """构造单块抽取提示词。

    :param focus: 读者目标 —— 否则每块各抽各的，汇总时是一堆碎片。
    :param screen_text: **该时间范围内**的画面文字（由调用方按时间切好）。
        整段画面的文字不能全塞给每一块，那样既浪费又会让模型分不清上下文。
    """
    focus_block = _render_focus(focus)
    screen_block = (
        f"===== 这一段的画面文字（音频里没有，比转写准确）=====\n"
        f"{screen_text.strip()}\n\n"
        if screen_text.strip()
        else ""
    )
    return (
        f"{MAP_INSTRUCTIONS}\n\n"
        f"{build_meta_header(meta, max_desc_chars=400)}\n"
        f"{focus_block}"
        f"这是全片第 {chunk.index + 1}/{total} 段，"
        f"时间范围 {chunk.time_range}。\n\n"
        f"{screen_block}"
        f"===== 转写片段开始 =====\n"
        f"{chunk.render(with_timestamps=True)}\n"
        f"===== 转写片段结束 ====="
    )


# ---------------------------------------------------------------------------
# reduce：汇总成最终笔记
# ---------------------------------------------------------------------------

REDUCE_INSTRUCTIONS = """下面是从同一个视频的各段转写中抽取出的中间结果。
请整合成一份完整的笔记 —— **围绕读者要什么来组织，而不是按视频的讲述顺序复述。**

整合要求：

- **goal**：读完这篇你能做到什么。直接对着读者的目的说，一句话。
  用你自己的话写，**不要照抄下面括号里举例的措辞**。

- **focus_points**：**最重要的一节**，3-6 条，排在最前面。
  冲着这个标题来的人必须带走的几件事。每条都要：
  · title 说到点子上，不要主题词
  · detail 讲透具体怎么做、为什么、有什么前提
  **不要写「视频介绍了…」这类复述，要写读者能直接用的内容。**
  ⚠️ 下面若出现举例，只说明**写法风格**，内容必须来自本视频，不要照抄。

- **one_liner**：一句话说清这个视频最核心的**结论**。
  注意与 goal 区分：goal 是「读者能做到什么」，one_liner 是「这个视频讲了什么结论」。
  两者不能是同一句话。

- **actionables**：**最有价值的一节**。视频里出现过的、可以原样复制的东西：
  提示词原文、命令、代码、参数、配置、操作步骤、检查清单。
  · content 必须是**可以直接复制粘贴的原文**，不要改写成描述
  · ⛔ content 里**不要再加标题或「XXX：」这类前缀** ——
    title 已经承担说明作用，读者要的是复制下来就能用的原文
  · 同一条提示词被多次提到就合并成一条
  · 视频里确实没有这类东西就留空数组，**绝对不要自己编**
  · ⚠️ 评测 / 解说 / 评论类视频**通常就没有可复制的东西** ——
    这时请老实留空数组，把信息放进 focus_points 和 key_points。
    不要为了填满这一节硬塞「价格：398元」这种只是事实、并非「可用」的内容

- outline：全片脉络，3-10 条，时间戳各不相同且递增。
- key_points：视频讲了什么（3-12 条），作为背景理解。
  注意与 focus_points 区分：focus_points 是「要拿走什么」，
  key_points 是「视频讲了什么」。

- concepts：视频里出现的术语解释。**只依据材料**，
  术语全称、缩写含义、作品出处，材料里没有就不要写。

- follow_ups：「读完这篇，你自己还需要搞清楚什么」。
  找出视频**默认你懂、但其实没交代**的点，列成待查问题。
  ⛔ **只提问，绝对不要给答案。** 你没有可靠的外部知识，
  编一个看起来合理的解释会害了读者。2-5 条，每条写清
  question / why / where（搜索关键词，不确定就填 null）。

- uncertainties：存疑、转写错字、前后矛盾。去重，最多 5 条。

时间戳规则（**务必遵守**）：
**原样抄写**中间的 [mm:ss] 标记，不要换算成秒数。
每条时间戳应各不相同且按时间递增；找不到就填 null，不要用 00:00 充数。

只输出 JSON。"""


def build_reduce_prompt(
    partials: list[PartialNotes],
    meta: VideoMeta,
    *,
    transcript_source_label: str = "",
    focus: FocusPlan | None = None,
    screen_text: str = "",
    compact: bool = False,
    max_items: int = 3,
) -> str:
    """构造汇总提示词。

    :param focus: 第一步推断出的读者目标 —— 汇总时最重要的输入。
    :param compact: 中间归并轮次使用。会压缩每条的篇幅、限制条数，
        目的是**控制提示词体量** —— 实测体量超过上下文会导致模型
        陷入异常直到超时（56 分钟的视频就这样失败过）。
        compact 必须真的足够紧凑：如果单条就超预算，
        切批时每条各成一批，归并就完全失效了。
    :param max_items: compact 模式下每类条目最多保留几条。
    """
    detail_chars = 80 if compact else 400
    blocks: list[str] = []

    for i, p in enumerate(partials, 1):
        lines = [f"--- 第 {i} 段中间结果 ---"]
        if p.outline:
            lines.append("大纲：")
            items = p.outline[:max_items] if compact else p.outline
            lines += [
                f"  [{_clock(o.timestamp)}] {o.heading}：{o.summary[:detail_chars]}"
                for o in items
            ]
        if p.actionables:
            lines.append("可用的东西（优先保留）：")
            items = p.actionables[:max_items] if compact else p.actionables
            lines += [
                f"  - [{a.kind}] {a.title}（{_clock(a.timestamp)}）："
                f"{a.content[:detail_chars]}"
                for a in items
            ]
        if p.key_points:
            lines.append("要点：")
            items = p.key_points[:max_items] if compact else p.key_points
            lines += [
                f"  - {k.title}"
                f"（{k.source}，{_clock(k.timestamp)}）：{k.detail[:detail_chars]}"
                for k in items
            ]
        if p.concepts:
            lines.append("概念：")
            items = p.concepts[:max_items] if compact else p.concepts
            lines += [
                f"  - {c.term}（{c.source}）：{c.explanation[:detail_chars]}"
                + (f" 例子：{c.example[:detail_chars]}" if c.example else "")
                for c in items
            ]
        if p.uncertain:
            lines.append("存疑：")
            items = p.uncertain[:3] if compact else p.uncertain
            lines += [f"  - {u[:detail_chars]}" for u in items]
        blocks.append("\n".join(lines))

    header = build_meta_header(meta, max_desc_chars=600)
    if transcript_source_label:
        header += f"\n转写来源：{transcript_source_label}（自动转写可能有错字，请注意甄别）"

    # compact（中间归并）时不给画面文字，控制体量
    screen_block = "" if compact else build_screen_section(screen_text, max_chars=5000)

    return (
        f"{REDUCE_INSTRUCTIONS}\n\n"
        f"===== 视频元信息 =====\n{header}\n"
        f"{_render_focus(focus)}\n"
        f"{screen_block}"
        + "\n\n".join(blocks)
    )


# ---------------------------------------------------------------------------
# 单次生成（短视频，不切块）
# ---------------------------------------------------------------------------

SINGLE_INSTRUCTIONS = """请根据下面的完整视频转写，写出一份让读者**能直接拿走东西**的笔记。

读者的定义：冲着这个视频标题点进来、想拿到具体做法的人。
**不要按视频的讲述顺序复述，要围绕读者要什么来组织。**

要求：

- **goal**：读完这篇你能做到什么。一句话，直接对着读者的目的说。
- **focus_points**：**最重要的一节**，3-6 条。必须带走的几件事。
  title 说到点子上；detail 讲透具体怎么做、为什么、有什么前提。
  不要写「视频介绍了…」这类复述。
- **actionables**：**最有价值的一节**。视频里出现过的、可原样复制的东西：
  提示词原文、命令、代码、参数、操作步骤、检查清单。
  content 必须是能直接复制粘贴的原文，不要改写成描述。
  确实没有就留空数组，**绝对不要编**。
- one_liner：一句话说清这个视频最核心的**结论**。
  注意与 goal 区分：goal 是「读者能做到什么」，one_liner 是「视频的结论」，
  两者不能是同一句话。
- summary：3-5 句，覆盖全片脉络。
- outline：3-10 条，时间戳各不相同且递增。
- key_points：视频讲了什么（3-12 条）。
- concepts：术语解释，**只依据材料**，不要补外部事实。
- follow_ups：「还需要自己搞清楚什么」。
  找出视频**默认你懂、但其实没交代**的点，列成待查问题。
  ⛔ **只提问，不要给答案。** 你没有可靠的外部知识，
  编一个看起来合理的解释会害了读者。2-5 条。
- uncertainties：存疑、错字、矛盾，去重后最多 5 条。

时间戳规则：**原样抄写**转写里的 [mm:ss] 标记，不要换算成秒数，
每条各不相同且递增，找不到就填 null。

只输出 JSON。"""


def build_single_prompt(
    transcript_text: str,
    meta: VideoMeta,
    *,
    transcript_source_label: str = "",
    focus: FocusPlan | None = None,
    screen_text: str = "",
) -> str:
    header = build_meta_header(meta, max_desc_chars=600)
    if transcript_source_label:
        header += f"\n转写来源：{transcript_source_label}"
    screen_block = build_screen_section(screen_text)
    return (
        f"{SINGLE_INSTRUCTIONS}\n\n"
        f"===== 视频元信息 =====\n{header}\n"
        f"{_render_focus(focus)}\n"
        f"{screen_block}"
        f"===== 音频转写开始 =====\n{transcript_text}\n===== 音频转写结束 ====="
    )


# ---------------------------------------------------------------------------
# JSON 修复重试
# ---------------------------------------------------------------------------

REPAIR_PROMPT = """你上一次的输出不是合法 JSON，或者不符合要求的字段结构。

请重新输出**纯 JSON**（不要 markdown 代码块、不要任何解释文字）。
如果某个字段确实没有内容，就给空数组，不要省略字段。

原始任务要求：
{original}

你上次的输出（已截断）：
{bad_output}

请只输出修正后的 JSON。"""


def build_repair_prompt(original: str, bad_output: str) -> str:
    return REPAIR_PROMPT.format(
        original=original[:4000],
        bad_output=bad_output[:2000],
    )


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _render_focus(focus: FocusPlan | None) -> str:
    """把第一步的判断渲染成提示词片段。"""
    if focus is None:
        return ""

    lines = ["===== 本次笔记的读者目标（请围绕它组织）====="]
    if focus.video_kind:
        lines.append(f"视频类型：{focus.video_kind}")
    if focus.audience_goal:
        lines.append(f"读者最想得到：{focus.audience_goal}")
    if focus.focus:
        lines.append("重点关注：")
        lines.extend(f"  {i}. {f}" for i, f in enumerate(focus.focus, 1))
    if focus.must_extract:
        lines.append("必须逐字抓下来的东西：")
        lines.extend(f"  - {m}" for m in focus.must_extract)
    if focus.ignore:
        lines.append(f"可以忽略：{'、'.join(focus.ignore)}")
    lines.append("")
    return "\n".join(lines)


def parse_focus(raw: Any) -> FocusPlan:
    """把模型输出解析成 :class:`FocusPlan`（宽容处理）。"""
    from app.llm.base import coerce_str, coerce_str_list

    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raw = {}
    data = raw if isinstance(raw, dict) else {}

    def pick(*names: str) -> Any:
        lowered = {
            str(k).lower().replace("_", "").replace("-", ""): v
            for k, v in data.items()
        }
        for name in names:
            key = name.lower().replace("_", "").replace("-", "")
            if key in lowered:
                return lowered[key]
        return None

    return FocusPlan(
        video_kind=coerce_str(pick("video_kind", "videoKind", "类型", "kind")),
        audience_goal=coerce_str(
            pick("audience_goal", "audienceGoal", "目标", "goal")
        ),
        focus=coerce_str_list(pick("focus", "关注点", "focus_points")),
        must_extract=coerce_str_list(
            pick("must_extract", "mustExtract", "必须抓取", "extract")
        ),
        ignore=coerce_str_list(pick("ignore", "忽略")),
    )


def _clock(seconds: float | None) -> str:
    if not seconds:
        return "--:--"
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
