"""Markdown 渲染测试。

产出就是最终交付物，所以这里既验证结构，也验证
**「视频原话」与「模型补充」有没有被清楚地区分开** ——
这是产品可信度的底线。
"""

from __future__ import annotations

import pytest

from app.notes.render import (
    render_frontmatter,
    render_index_markdown,
    render_note_markdown,
)
from app.schemas import (
    Concept,
    KeyPoint,
    Note,
    NoteRecord,
    OutlineItem,
    Supplement,
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
        one_liner="它不是银弹，只解决高维数据的近似最近邻检索。",
        summary="先给结论，再讲 ANN 原理，最后介绍 HNSW 与 IVF。",
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
        supplements=[
            Supplement(
                topic="维度灾难",
                why="不理解它就看不懂为什么要用近似算法。",
                content="维度越高数据越稀疏，树形分割退化成线性扫描。",
            )
        ],
        uncertainties=[
            Uncertainty(item="HNSW 参数", reason="视频里一带而过")
        ],
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
    assert "bvid:" in fm
    assert "source_url:" in fm
    assert "tags:" in fm


def test_frontmatter_contains_bvid_and_url(note, meta, transcript):
    fm = render_frontmatter(note, meta, transcript)
    assert "BV1GJ411x7h7" in fm
    assert "https://www.bilibili.com/video/BV1GJ411x7h7" in fm


def test_frontmatter_records_transcript_confidence(note, meta, transcript):
    fm = render_frontmatter(note, meta, transcript)
    assert "本地Whisper转写" in fm
    assert "可信度" not in fm or "中" in fm


def test_frontmatter_escapes_quotes(meta, transcript):
    note = Note(title='带"引号"的标题', one_liner="o", summary="s")
    fm = render_frontmatter(note, meta, transcript)
    assert '\\"' in fm


# ---------------------------------------------------------------------------
# 主渲染
# ---------------------------------------------------------------------------


def test_renders_all_major_sections(note, meta, transcript):
    md = render_note_markdown(note, meta, transcript)
    for section in (
        "一句话总结",
        "内容摘要",
        "大纲",
        "核心要点",
        "关键概念",
        "补充背景",
        "存疑与待验证",
        "转写全文",
    ):
        assert section in md, f"缺少小节：{section}"


def test_title_is_h1(note, meta, transcript):
    md = render_note_markdown(note, meta, transcript)
    assert md.count("\n# ") == 1


def test_one_liner_is_bold(note, meta, transcript):
    md = render_note_markdown(note, meta, transcript)
    assert "**它不是银弹，只解决高维数据的近似最近邻检索。**" in md


def test_owner_and_duration_present(note, meta, transcript):
    md = render_note_markdown(note, meta, transcript)
    assert "某UP主" in md
    assert "30:00" in md


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
    """单P视频不能出现 ``...BVxxx&t=`` 这种非法链接。"""
    meta = VideoMeta(bvid="BV1GJ411x7h7", title="t", page=1, duration=100)
    md = render_note_markdown(note, meta, transcript)
    assert "BV1GJ411x7h7&t=" not in md


def test_key_point_without_timestamp_has_no_link(note, meta, transcript):
    md = render_note_markdown(note, meta, transcript)
    # 第二条要点 timestamp=None，不应生成链接
    assert "索引结构决定性能" in md


# ---------------------------------------------------------------------------
# 来源标注 —— 可信度底线
# ---------------------------------------------------------------------------


def test_video_sourced_items_marked_as_video(note, meta, transcript):
    md = render_note_markdown(note, meta, transcript)
    assert "视频原话" in md


def test_supplements_marked_as_model_generated(note, meta, transcript):
    md = render_note_markdown(note, meta, transcript)
    assert "由模型补充" in md
    assert "模型补充" in md


def test_supplements_carry_an_explicit_verification_warning(note, meta, transcript):
    """⛔ 回归守卫。

    实测发现模型会在补充背景里编造事实（把 MGSD 全称写成
    Master Grade Sword）。这一节是用户最想要、也最不可信的，
    必须在笔记正文里就把「需自行核实」写清楚，不能只靠文档说明。
    """
    md = render_note_markdown(note, meta, transcript)
    assert "未经验证" in md
    assert "核实" in md


def test_auto_transcript_gets_warning(note, meta, transcript):
    md = render_note_markdown(note, meta, transcript)
    assert "自动生成的字幕" in md or "语音转写" in md


def test_human_subtitle_no_warning(note, meta):
    """人工字幕可信度高，不应出现转录错误的警告。"""
    transcript = Transcript(
        segments=[TranscriptSegment(start=0, end=1, text="x")],
        source=TranscriptSource.BILI_CC,
    )
    md = render_note_markdown(note, meta, transcript)
    assert "可能存在同音字" not in md


# ---------------------------------------------------------------------------
# 转写全文
# ---------------------------------------------------------------------------


def test_transcript_section_can_be_omitted(note, meta, transcript):
    md = render_note_markdown(note, meta, transcript, include_transcript=False)
    assert "转写全文" not in md
    assert "大家好" not in md


def test_transcript_lines_have_clickable_timestamps(note, meta, transcript):
    md = render_note_markdown(note, meta, transcript)
    assert "[00:00](https://www.bilibili.com/video/BV1GJ411x7h7?t=0)" in md
    assert "[00:03](https://www.bilibili.com/video/BV1GJ411x7h7?t=3)" in md


def test_timestamp_links_are_never_wrapped_in_backticks(note, meta, transcript):
    """回归守卫：``\\`[00:00](url)\\``` 在 Markdown 里是字面量，链接会点不动。

    这正好废掉「跳回原视频核对」这个核心功能，所以专门盯住。
    """
    md = render_note_markdown(note, meta, transcript)
    assert "`[0" not in md
    assert "`[1" not in md


def test_transcript_truncation(note, meta, transcript):
    md = render_note_markdown(note, meta, transcript, transcript_max_chars=5)
    assert "转写过长" in md


# ---------------------------------------------------------------------------
# 边界情况
# ---------------------------------------------------------------------------


def test_minimal_note_does_not_crash(meta, transcript):
    md = render_note_markdown(
        Note(title="只有标题", one_liner="", summary=""), meta, transcript
    )
    assert "# 只有标题" in md


def test_note_without_title_falls_back_to_video_title(meta, transcript):
    md = render_note_markdown(Note(title="", one_liner="o", summary="s"), meta, transcript)
    assert "# 向量数据库入门" in md


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
    # 链接应指向同目录下的文件名
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
    # 竖线必须转义，否则表格会错列
    assert "\\|" in md
