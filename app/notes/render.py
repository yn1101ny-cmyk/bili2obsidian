"""Markdown 渲染 —— 纯函数，重点覆盖测试。

## 结构顺序 = 阅读优先级

笔记**不再是「视频讲了什么」的流水账**，而是按读者要什么来排：

1. ``goal`` —— 读完你能做到什么
2. ``focus_points`` —— **重点**，冲着标题来的人必须带走的几件事
3. ``actionables`` —— **能直接用的东西**：提示词、命令、步骤
4. 然后才是大纲、要点这些「视频讲了什么」的背景
5. 最后是概念、待查、存疑

**刻意不输出转写全文** —— 复述整段音频没有价值，篇幅应该让给重点。
转写另有纯文本文件（``Vault.write_transcript``），需要核对时再翻。

渲染上还有一条硬约束：**不产出任何原始 HTML**。
Web 层依赖这一点对整份文档做统一转义，从而杜绝注入。
"""

from __future__ import annotations

from datetime import datetime

from app.schemas import (
    ActionableKind,
    Note,
    Transcript,
    TranscriptSource,
    VideoMeta,
    format_clock,
)

SOURCE_BADGE = {
    "video": "✅ 视频原话",
    "model": "➕ 模型补充",
    "mixed": "🔀 视频+补充",
}

#: frontmatter 里需要转义的字符
_YAML_ESCAPE = str.maketrans({'"': '\\"', "\\": "\\\\", "\n": " "})


def _yaml_str(value: str) -> str:
    return '"' + str(value).translate(_YAML_ESCAPE) + '"'


def _ts_link(meta: VideoMeta, seconds: float | None, label: str) -> str:
    """生成可跳转的时间戳链接；拿不到秒数时退化成纯文本。"""
    if seconds is None:
        return label
    return f"[{label}]({meta.timestamped_url(seconds)})"


def render_frontmatter(
    note: Note,
    meta: VideoMeta,
    transcript: Transcript,
) -> str:
    """生成 YAML frontmatter。"""
    tags = list(dict.fromkeys([*note.tags, "bilibili", "视频笔记"]))
    lines = [
        "---",
        f"title: {_yaml_str(note.title or meta.display_title)}",
        f"goal: {_yaml_str(note.goal)}",
        f"bvid: {_yaml_str(meta.bvid)}",
        f"source_url: {_yaml_str(meta.url)}",
        f"up: {_yaml_str(meta.owner_name)}",
        f"duration: {_yaml_str(format_clock(meta.duration or transcript.duration))}",
        f"transcript_source: {_yaml_str(transcript.source.label)}",
        f"transcript_confidence: {_yaml_str(transcript.source.confidence)}",
    ]
    if meta.chapters:
        lines.append("chapters:")
        lines.extend(
            f"  - {_yaml_str(f'{c.clock()} {c.title}')}" for c in meta.chapters
        )
    lines.append(f"created: {_yaml_str(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))}")
    lines.append("tags:")
    lines.extend(f"  - {_yaml_str(t)}" for t in tags)
    lines.append("---")
    return "\n".join(lines)


def render_note_markdown(
    note: Note,
    meta: VideoMeta,
    transcript: Transcript,
) -> str:
    """把笔记渲染成完整 Markdown。"""
    parts: list[str] = [
        render_frontmatter(note, meta, transcript),
        "",
        f"# {note.title or meta.display_title}",
        "",
    ]

    # ---- 元信息条 ----
    parts.append(
        f"> **UP主**：{meta.owner_name or '未知'} ｜ "
        f"**时长**：{format_clock(meta.duration or transcript.duration)} ｜ "
        f"**转写来源**：{transcript.source.label}"
        f"（可信度 {transcript.source.confidence}）"
    )
    parts.append(">")
    parts.append(f"> 🔗 [在原视频中打开]({meta.url})")
    parts.append("")

    # ---- 目标：读者读完能做到什么 ----
    if note.goal.strip():
        parts += [f"> 🎯 **读完这篇你能做到**：{note.goal.strip()}", ""]

    if transcript.source is not TranscriptSource.BILI_CC:
        parts.append(
            "> ⚠️ 本文基于**自动生成的字幕/语音转写**，可能存在同音字、"
            "专有名词识别错误。关键结论建议对照原视频确认。"
        )
        parts.append("")

    # 关于模型生成内容的总体警示。
    # 实测模型会自信地编造术语全称/作品出处，而且每次编的还不一样。
    # 这类错误一旦进了笔记就会变成「自己记错的知识」，必须提前说清。
    parts.append("> 🧠 **「关键概念」一节由本地模型生成，未经验证。**")
    parts.append(">")
    parts.append(
        "> 模型可能给出错误的术语解释 —— 实测它会编造术语全称、作品出处。"
        "请对照视频原文核实：带 `✅ 视频原话` 标记的表示视频里讲过。"
    )
    parts.append("")

    # ---- 一句话总结 / 摘要 ----
    if note.one_liner.strip():
        parts += ["## 🎯 一句话总结", "", f"**{note.one_liner.strip()}**", ""]

    if note.summary.strip():
        parts += ["## 📄 内容摘要", "", note.summary.strip(), ""]

    # ---- 重点：放在最前面，写得最实 ----
    if note.focus_points:
        parts += ["## 📌 重点（这个视频最该拿走的）", ""]
        for i, fp in enumerate(note.focus_points, 1):
            ts = _ts_link(meta, fp.timestamp, fp.clock()) if fp.timestamp is not None else ""
            head = f"### {i}. {fp.title.strip()}"
            if ts:
                head += f"  · {ts}"
            parts += [head, ""]
            if fp.detail.strip():
                parts += [fp.detail.strip(), ""]

    # ---- 能直接用的东西 ----
    if note.actionables:
        parts += [
            "## 🛠️ 能直接用的东西",
            "",
            "*以下内容摘自视频，可原样复制使用。*",
            "",
        ]
        by_kind: dict[ActionableKind, list] = {}
        for item in note.actionables:
            by_kind.setdefault(item.kind, []).append(item)

        for kind, items in by_kind.items():
            parts += [f"### {kind.icon} {kind.label}", ""]
            for item in items:
                ts = (
                    _ts_link(meta, item.timestamp, item.clock())
                    if item.timestamp is not None
                    else ""
                )
                head = f"**{item.title.strip()}**"
                if ts:
                    head += f"  · {ts}"
                parts += [head, ""]
                if item.content.strip():
                    parts += ["```text", item.content.strip(), "```", ""]
                if item.note and item.note.strip():
                    parts += [f"*{item.note.strip()}*", ""]

    # ---- 大纲 ----
    if note.outline:
        parts += ["## 🗺️ 大纲（可跳转）", ""]
        for item in note.outline:
            link = _ts_link(meta, item.timestamp, item.clock())
            parts.append(f"- **{link}** — {item.heading}")
            if item.summary.strip():
                parts.append(f"  - {item.summary.strip()}")
        parts.append("")

    # ---- 核心要点 ----
    if note.key_points:
        parts += ["## 💡 视频讲了什么", ""]
        for i, kp in enumerate(note.key_points, 1):
            badge = SOURCE_BADGE.get(kp.source, "")
            ts = _ts_link(meta, kp.timestamp, kp.clock()) if kp.timestamp is not None else ""
            head = f"### {i}. {kp.title.strip()}"
            if ts:
                head += f"  · {ts}"
            parts += [head, ""]
            parts += [f"{kp.detail.strip()}", ""]
            if badge:
                parts.append(f"*{badge}*")
                parts.append("")

    # ---- 关键概念 ----
    if note.concepts:
        parts += ["## 📚 关键概念", ""]
        for c in note.concepts:
            badge = SOURCE_BADGE.get(c.source, "")
            ts = _ts_link(meta, c.timestamp, c.clock()) if c.timestamp is not None else ""
            head = f"### {c.term.strip()}"
            if ts:
                head += f"  · {ts}"
            parts += [head, "", c.explanation.strip(), ""]
            if c.example and c.example.strip():
                parts += [f"> **举个例子**：{c.example.strip()}", ""]
            if badge:
                parts.append(f"*{badge}*")
                parts.append("")

    # ---- 待查清单 ----
    if note.follow_ups:
        parts += [
            "## 🧩 读完这篇，你可能还需要搞清楚这些",
            "",
            "> 视频默认你懂、但没有解释的点。**这里只给问题，不给答案** ——",
            "> 本地模型没有可靠的外部知识，实测它会在这个位置编造术语全称、",
            "> 作品出处（同一个词三次运行给出三种不同「全称」）。",
            "> 与其给你一个可能错的解释，不如把该查什么列清楚。",
            "",
        ]
        for item in note.follow_ups:
            parts += [f"- **{item.question.strip()}**", ""]
            if item.why.strip():
                parts.append(f"  {item.why.strip()}")
                parts.append("")
            if item.where and item.where.strip():
                lines = item.where.strip().splitlines()
                parts.append(f"  *🔍 建议搜：{' '.join(lines)}*")
                parts.append("")

    # ---- 存疑 ----
    if note.uncertainties:
        parts += ["## ❓ 存疑与待验证", ""]
        for u in note.uncertainties:
            parts.append(f"- **{u.item.strip()}** — {u.reason.strip()}")
        parts.append("")

    return "\n".join(parts).rstrip() + "\n"


def render_index_markdown(records: list) -> str:
    """生成笔记索引页（按时间倒序）。"""
    lines = ["# 📒 我的视频笔记", "", f"共 {len(records)} 篇。", ""]
    if not records:
        lines.append("还没有笔记。把 B站链接贴进应用就能生成第一篇。")
        return "\n".join(lines) + "\n"

    lines += ["| 笔记 | 一句话总结 | 来源 | 时长 | 生成时间 |", "|---|---|---|---|---|"]
    for r in records:
        name = (r.markdown_path or "").replace("\\", "/").split("/")[-1]
        title = (r.title or r.bvid).replace("|", "\\|")
        one = (r.one_liner or "").replace("|", "\\|").replace("\n", " ")
        title_cell = f"[{title}]({name})" if name else title
        created = (
            r.created_at.strftime("%Y-%m-%d %H:%M")
            if isinstance(r.created_at, datetime)
            else str(r.created_at)
        )
        lines.append(
            f"| {title_cell} | {one} | {r.transcript_source.label} | "
            f"{format_clock(r.duration)} | {created} |"
        )
    return "\n".join(lines) + "\n"
