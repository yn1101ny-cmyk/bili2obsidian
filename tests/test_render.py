"""Markdown 渲染测试。

产出就是最终交付物，所以这里既验证结构，也验证几条**产品底线**：

* 「重点」和「能直接用的东西」必须排在最前面、写得最实；
* 来源要标清楚（✅视频原话 / ➕模型补充）；
* **不输出转写全文**（复述音频没有价值，篇幅要让给重点）；
* **不产出任何原始 HTML**（Web 层靠这一点做统一转义）。
"""

from __future__ import annotations

import re

import pytest

from app.notes.render import (
    render_frontmatter,
    render_index_markdown,
    render_note_markdown,
)
from app.schemas import (
    Actionable,
    ActionableKind,
    Concept,
    FocusPoint,
    FollowUp,
    KeyPoint,
    Note,
    NoteRecord,
    OutlineItem,
    Transcript,
    TranscriptSegment,
    TranscriptSource,
    Uncertainty,
    VideoMeta,
)


@pytest.fixture
def meta() -> VideoMeta:
    return VideoMeta(
        bvid="BV1GJ411x7h7",
        title="向量数据库入门",
        owner_name="某UP主",
        duration=1800,
        page=1,
    )


@pytest.fixture
def note() -> Note:
    return Note(
        title="向量数据库到底解决什么问题",
        goal="能判断自己的场景该不该上向量数据库。",
        one_liner="它不是银弹，只解决高维数据的近似最近邻检索。",
        summary="先给结论，再讲 ANN 原理，最后介绍 HNSW 与 IVF。",
        focus_points=[
            FocusPoint(
                title="先确认场景是不是高维相似度检索",
                detail="不是的话传统数据库加索引就够了。",
                timestamp=0.0,
            ),
            FocusPoint(
                title="索引选型先看召回率要求",
                detail="HNSW 召回高但吃内存；IVF 省内存但要调参。",
                timestamp=480.0,
            ),
        ],
        actionables=[
            Actionable(
                kind=ActionableKind.COMMAND,
                title="建 HNSW 索引",
                content="CREATE INDEX ON items USING hnsw (embedding vector_cosine_ops);",
                note="维度必须与建表时一致。",
                timestamp=480.0,
            ),
            Actionable(
                kind=ActionableKind.CHECKLIST,
                title="上线前检查",
                content="1. 召回率达标\n2. 内存余量足够",
            ),
        ],
        outline=[
            OutlineItem(timestamp=0.0, heading="开场", summary="提出主题"),
            OutlineItem(timestamp=480.0, heading="核心原理", summary="讲 ANN 的取舍"),
        ],
        key_points=[
            KeyPoint(
                title="ANN 用准确率换速度",
                detail="精确最近邻在高维上太慢。",
                source="video",
                timestamp=480.0,
            ),
            KeyPoint(
                title="索引结构决定性能",
                detail="不同的索引适合不同的召回率要求。",
                source="model",
                timestamp=None,
            ),
        ],
        concepts=[
            Concept(
                term="ANN",
                explanation="近似最近邻，不保证最优但足够快。",
                source="video",
                example="像在图书馆按大致区域找书。",
                timestamp=912.0,
            )
        ],
        follow_ups=[
            FollowUp(
                question="刹帝利出自哪部作品、什么定位？",
                why="视频默认你熟悉 UC 系谱，却完全没交代背景。",
                where="机动战士高达UC 刹帝利",
            ),
            FollowUp(
                question="MGSD 与 HG / RG / MG 的定位差别？",
                why="视频反复拿它们对比，却没给出各自定义。",
            ),
        ],
        uncertainties=[Uncertainty(item="HNSW 参数", reason="视频里一带而过")],
        tags=["向量数据库"],
    )


@pytest.fixture
def transcript() -> Transcript:
    return Transcript(
        segments=[
            TranscriptSegment(start=0.0, end=3.5, text="大家好，今天聊向量数据库。"),
            TranscriptSegment(start=3.5, end=8.0, text="先说结论：它不是银弹。"),
        ],
        source=TranscriptSource.WHISPER,
        language="zh",
    )


# ---------------------------------------------------------------------------
# frontmatter
# ---------------------------------------------------------------------------


def test_frontmatter_is_valid_yaml_block(note, meta, transcript):
    fm = render_frontmatter(note, meta, transcript)
    assert fm.startswith("---\n")
    assert fm.rstrip().endswith("---")
    for field in ("bvid:", "source_url:", "tags:", "goal:"):
        assert field in fm


def test_frontmatter_contains_goal(note, meta, transcript):
    """目标要写进 frontmatter，方便 Obsidian 等工具做检索。"""
    fm = render_frontmatter(note, meta, transcript)
    assert "能判断自己的场景该不该上向量数据库" in fm


def test_frontmatter_lists_chapters_when_present(note, transcript):
    from app.schemas import Chapter

    meta = VideoMeta(
        bvid="BV1",
        title="t",
        chapters=[Chapter(title="开场", start=0.0), Chapter(title="原理", start=143.0)],
    )
    fm = render_frontmatter(note, meta, transcript)
    assert "chapters:" in fm
    assert "02:23 原理" in fm


def test_frontmatter_escapes_quotes(meta, transcript):
    note = Note(title='带"引号"的标题', goal="g", one_liner="o", summary="s")
    fm = render_frontmatter(note, meta, transcript)
    assert '\\"' in fm


# ---------------------------------------------------------------------------
# 主渲染：结构
# ---------------------------------------------------------------------------


def test_renders_all_major_sections(note, meta, transcript):
    md = render_note_markdown(note, meta, transcript)
    for section in (
        "一句话总结",
        "内容摘要",
        "重点",
        "能直接用的东西",
        "大纲",
        "视频讲了什么",
        "关键概念",
        "你可能还需要搞清楚这些",
        "存疑与待验证",
    ):
        assert section in md, f"缺少小节：{section}"


def test_focus_points_come_before_outline(note, meta, transcript):
    """⛔ 产品底线：重点必须排在「视频讲了什么」之前。

    用户反馈原话：「做出来的笔记不是很重点」。按时间顺序复述就是病根。
    """
    md = render_note_markdown(note, meta, transcript)
    assert md.index("## 📌 重点") < md.index("## 🗺️ 大纲")
    assert md.index("## 📌 重点") < md.index("## 💡 视频讲了什么")


def test_actionables_come_before_outline(note, meta, transcript):
    md = render_note_markdown(note, meta, transcript)
    assert md.index("## 🛠️ 能直接用的东西") < md.index("## 🗺️ 大纲")


def test_goal_is_prominent(note, meta, transcript):
    md = render_note_markdown(note, meta, transcript)
    assert "读完这篇你能做到" in md
    assert "能判断自己的场景该不该上向量数据库" in md


def test_title_is_h1(note, meta, transcript):
    md = render_note_markdown(note, meta, transcript)
    assert md.count("\n# ") == 1


def test_owner_and_duration_present(note, meta, transcript):
    md = render_note_markdown(note, meta, transcript)
    assert "某UP主" in md
    assert "30:00" in md


# ---------------------------------------------------------------------------
# 重点与行动项的内容
# ---------------------------------------------------------------------------


def test_focus_points_are_numbered(note, meta, transcript):
    md = render_note_markdown(note, meta, transcript)
    assert "### 1. 先确认场景是不是高维相似度检索" in md
    assert "### 2. 索引选型先看召回率要求" in md


def test_actionable_content_is_in_code_block(note, meta, transcript):
    """提示词/命令必须放在代码块里，方便一键复制。"""
    md = render_note_markdown(note, meta, transcript)
    assert "CREATE INDEX ON items USING hnsw" in md
    block = md[md.index("CREATE INDEX") :]
    assert "```" in block[:400]


def test_actionables_grouped_by_kind(note, meta, transcript):
    md = render_note_markdown(note, meta, transcript)
    assert "⌨️ 命令 / 代码" in md
    assert "✅ 检查清单" in md


def test_actionable_note_rendered(note, meta, transcript):
    md = render_note_markdown(note, meta, transcript)
    assert "维度必须与建表时一致" in md


def test_actionable_multiline_content_kept(note, meta, transcript):
    md = render_note_markdown(note, meta, transcript)
    assert "1. 召回率达标" in md
    assert "2. 内存余量足够" in md


def test_actionable_timestamp_is_link(note, meta, transcript):
    md = render_note_markdown(note, meta, transcript)
    assert "[08:00](https://www.bilibili.com/video/BV1GJ411x7h7?t=480)" in md


# ---------------------------------------------------------------------------
# 不再输出转写全文
# ---------------------------------------------------------------------------


def test_no_transcript_section(note, meta, transcript):
    """⛔ 用户明确要求去掉全文部分。"""
    md = render_note_markdown(note, meta, transcript)
    assert "转写全文" not in md
    assert "大家好，今天聊向量数据库" not in md


def test_no_segment_level_timestamp_dump(note, meta, transcript):
    """转写那种逐句 `[00:00] 内容` 的形态不该再出现在笔记里。"""
    md = render_note_markdown(note, meta, transcript)
    # 大纲/要点里的链接是 `[08:00](url)` 形态，转写则是 ``[00:00] 文本``
    assert not re.search(r"\[\d{2}:\d{2}\]\s", md)


# ---------------------------------------------------------------------------
# 时间戳链接
# ---------------------------------------------------------------------------


def test_timestamps_become_clickable_links(note, meta, transcript):
    md = render_note_markdown(note, meta, transcript)
    assert "[08:00](https://www.bilibili.com/video/BV1GJ411x7h7?t=480)" in md


def test_multi_page_links_use_ampersand(note, transcript):
    meta = VideoMeta(bvid="BV1GJ411x7h7", title="t", page=2, duration=100)
    md = render_note_markdown(note, meta, transcript)
    assert "?p=2&t=480" in md


def test_rendering_never_emits_bare_ampersand_time_link(note, transcript):
    meta = VideoMeta(bvid="BV1GJ411x7h7", title="t", page=1, duration=100)
    md = render_note_markdown(note, meta, transcript)
    assert "BV1GJ411x7h7&t=" not in md


def test_timestamp_links_are_never_wrapped_in_backticks(note, meta, transcript):
    """回归守卫：``\\`[00:00](url)\\``` 在 Markdown 里是字面量，链接会点不动。"""
    md = render_note_markdown(note, meta, transcript)
    assert "`[0" not in md
    assert "`[1" not in md


# ---------------------------------------------------------------------------
# 来源标注
# ---------------------------------------------------------------------------


def test_video_sourced_items_marked_as_video(note, meta, transcript):
    md = render_note_markdown(note, meta, transcript)
    assert "视频原话" in md


def test_model_sourced_items_marked(note, meta, transcript):
    md = render_note_markdown(note, meta, transcript)
    assert "模型补充" in md


def test_auto_transcript_gets_warning(note, meta, transcript):
    md = render_note_markdown(note, meta, transcript)
    assert "自动生成的字幕" in md or "语音转写" in md


def test_human_subtitle_no_warning(note, meta):
    transcript = Transcript(
        segments=[TranscriptSegment(start=0, end=1, text="x")],
        source=TranscriptSource.BILI_CC,
    )
    md = render_note_markdown(note, meta, transcript)
    assert "可能存在同音字" not in md


def test_follow_ups_render_as_questions(note, meta, transcript):
    md = render_note_markdown(note, meta, transcript)
    assert "刹帝利出自哪部作品" in md
    assert "建议搜" in md


def test_follow_ups_section_states_it_gives_no_answers(note, meta, transcript):
    md = render_note_markdown(note, meta, transcript)
    assert "只给问题，不给答案" in md


def test_model_generated_concepts_carry_verification_warning(note, meta, transcript):
    md = render_note_markdown(note, meta, transcript)
    assert "未经验证" in md
    assert "核实" in md


# ---------------------------------------------------------------------------
# 安全：不产出原始 HTML
# ---------------------------------------------------------------------------


def test_output_contains_no_raw_html(note, meta, transcript):
    """⛔ Web 层靠这条不变量做统一转义。

    任何 <xxx> 形态的标签都会让「先转义再渲染」的策略失效，
    所以渲染器本身必须保证不产出原始 HTML。
    """
    md = render_note_markdown(note, meta, transcript)
    assert not re.search(r"<[a-zA-Z/][^>]*>", md), "渲染器产出了原始 HTML"


def test_dangerous_title_is_not_escaped_by_renderer(meta, transcript):
    """转义是 Web 层的职责，渲染器只管原样输出 Markdown。"""
    note = Note(
        title="<script>alert(1)</script>",
        goal="g",
        one_liner="o",
        summary="s",
    )
    md = render_note_markdown(note, meta, transcript)
    assert "<script>" in md  # 原样保留，由 Web 层转义


# ---------------------------------------------------------------------------
# 边界
# ---------------------------------------------------------------------------


def test_minimal_note_does_not_crash(meta, transcript):
    md = render_note_markdown(
        Note(title="只有标题", goal="", one_liner="", summary=""), meta, transcript
    )
    assert "# 只有标题" in md


def test_note_without_title_falls_back_to_video_title(meta, transcript):
    md = render_note_markdown(
        Note(title="", goal="g", one_liner="o", summary="s"), meta, transcript
    )
    assert "# 向量数据库入门" in md


def test_empty_sections_are_skipped(meta, transcript):
    md = render_note_markdown(
        Note(title="t", goal="", one_liner="", summary=""), meta, transcript
    )
    assert "## 📌 重点" not in md
    assert "## 🛠️ 能直接用的东西" not in md
    assert "## 📚 关键概念" not in md


def test_output_ends_with_newline(note, meta, transcript):
    assert render_note_markdown(note, meta, transcript).endswith("\n")


def test_no_trailing_whitespace_lines(note, meta, transcript):
    md = render_note_markdown(note, meta, transcript)
    for line in md.splitlines():
        assert line == line.rstrip(), f"行尾有多余空白：{line!r}"


# ---------------------------------------------------------------------------
# 索引页
# ---------------------------------------------------------------------------


def test_index_empty():
    md = render_index_markdown([])
    assert "还没有笔记" in md


def test_index_lists_notes():
    records = [
        NoteRecord(
            id="a1",
            bvid="BV1GJ411x7h7",
            title="向量数据库",
            one_liner="一句话",
            markdown_path="data/notes/向量数据库_BV1GJ411x7h7.md",
            transcript_source=TranscriptSource.WHISPER,
            duration=1800,
        )
    ]
    md = render_index_markdown(records)
    assert "向量数据库" in md
    assert "共 1 篇" in md
    assert "(向量数据库_BV1GJ411x7h7.md)" in md


def test_index_escapes_pipes():
    records = [
        NoteRecord(
            id="a1",
            bvid="BV1",
            title="标题|带竖线",
            one_liner="总结|也有",
            markdown_path="x.md",
        )
    ]
    md = render_index_markdown(records)
    assert "\\|" in md
