"""笔记生成流水线测试。

用假模型驱动，**不联网、不用真模型**。

假模型按 **schema 自动挑选**返回内容 —— 生成过程中有三类调用
（推断读者目标 / 分段抽取 / 最终汇总），按 schema 区分比按调用顺序猜稳得多。
"""

from __future__ import annotations

import json

import pytest

from app.errors import LLMError, LLMOutputInvalid
from app.llm.base import LLMProvider
from app.notes.pipeline import (
    NoteGenerator,
    _note_needs_retry,
    _outline_timestamps_bad,
    coerce_actionable_kind,
    to_note,
    to_partial_notes,
)
from app.notes.prompts import (
    REDUCE_INSTRUCTIONS,
    SINGLE_INSTRUCTIONS,
    build_meta_header,
    build_reduce_prompt,
    build_single_prompt,
    parse_focus,
)
from app.schemas import ActionableKind, Transcript, TranscriptSegment, VideoMeta

from .conftest import FakeProvider


@pytest.fixture
def meta() -> VideoMeta:
    return VideoMeta(
        bvid="BV1GJ411x7h7",
        title="向量数据库入门",
        owner_name="某UP主",
        duration=1800,
    )


FOCUS_JSON = {
    "video_kind": "操作教程",
    "audience_goal": "学会判断该不该上向量数据库，以及选哪类索引",
    "focus": ["先确认场景", "索引选型看召回率"],
    "must_extract": ["建索引的命令", "参数取值"],
    "ignore": ["开场寒暄", "求三连"],
}

PARTIAL_JSON = {
    "outline": [{"timestamp": 0, "heading": "小节", "summary": "讲了一点东西"}],
    "key_points": [
        {"title": "要点", "detail": "细节说明", "source": "video", "timestamp": 0}
    ],
    "actionables": [
        {
            "kind": "command",
            "title": "建索引",
            "content": "CREATE INDEX ...",
            "timestamp": 0,
        }
    ],
    "concepts": [{"term": "术语", "explanation": "解释", "source": "video"}],
    "uncertain": ["转写不清的地方"],
}


class SchemaAwareProvider(LLMProvider):
    """按 schema 自动挑返回内容的假模型。"""

    name = "fake/schema-aware"
    is_remote = False

    def __init__(
        self,
        *,
        note: dict | None = None,
        partial: dict | None = None,
        focus: dict | None = None,
        raw_note: str | None = None,
    ) -> None:
        self._note = note if note is not None else {}
        self._partial = partial if partial is not None else PARTIAL_JSON
        self._focus = focus if focus is not None else FOCUS_JSON
        self._raw_note = raw_note

        self.calls: list[str] = []
        self.temperatures: list[float] = []
        self._note_responses: list[str] = []

    def queue_note_response(self, payload: dict | str) -> None:
        """把下一次「最终汇总」的返回换成指定内容（用于测试重试）。"""
        self._note_responses.append(
            payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        )

    async def complete_json(
        self, *, system: str, user: str, schema: dict | None = None, temperature: float = 0.2
    ) -> str:
        self.calls.append(user)
        self.temperatures.append(temperature)
        keys = set((schema or {}).get("properties", {}))

        if "audience_goal" in keys:
            return json.dumps(self._focus, ensure_ascii=False)
        if "follow_ups" in keys:
            if self._note_responses:
                return self._note_responses.pop(0)
            if self._raw_note is not None:
                return self._raw_note
            return json.dumps(self._note, ensure_ascii=False)
        return json.dumps(self._partial, ensure_ascii=False)

    async def health(self) -> tuple[bool, str]:
        return True, "假模型就绪"

    @property
    def note_calls(self) -> list[str]:
        """只看「最终汇总」那几次调用的用户消息。"""
        return self.calls


# ---------------------------------------------------------------------------
# 模型输出 -> Note
# ---------------------------------------------------------------------------


def test_to_note_accepts_camel_case_keys(sample_note_payload):
    note = to_note(sample_note_payload)
    assert note.title == "向量数据库到底解决什么问题"
    assert note.goal.startswith("能判断自己的场景")
    assert len(note.focus_points) == 2
    assert len(note.actionables) == 2


def test_to_note_parses_actionable_kinds(sample_note_payload):
    note = to_note(sample_note_payload)
    kinds = [a.kind for a in note.actionables]
    assert ActionableKind.COMMAND in kinds
    assert ActionableKind.CHECKLIST in kinds


def test_to_note_keeps_actionable_content_verbatim(sample_note_payload):
    """提示词/命令必须原样保留，不能改写 —— 这是最有价值的部分。"""
    note = to_note(sample_note_payload)
    command = next(a for a in note.actionables if a.kind is ActionableKind.COMMAND)
    assert command.content == (
        "CREATE INDEX ON items USING hnsw (embedding vector_cosine_ops);"
    )


def test_to_note_actionable_note_and_timestamp(sample_note_payload):
    note = to_note(sample_note_payload)
    command = next(a for a in note.actionables if a.kind is ActionableKind.COMMAND)
    assert command.note == "向量维度必须与建表时一致。"
    assert command.timestamp == pytest.approx(480.0)


def test_to_note_accepts_chinese_keys():
    raw = {
        "标题": "中文键名",
        "目标": "能做到某事",
        "一句话总结": "结论",
        "摘要": "摘要内容",
        "重点": [{"重点": "重点1", "说明": "怎么做"}],
        "要点": [{"要点": "点1", "说明": "说明1"}],
        "概念": [{"概念": "ANN", "解释": "近似最近邻"}],
        "存疑": ["不确定的地方"],
        "标签": ["标签1"],
    }
    note = to_note(raw)
    assert note.title == "中文键名"
    assert note.goal == "能做到某事"
    assert note.focus_points[0].title == "重点1"
    assert note.key_points[0].title == "点1"
    assert note.concepts[0].term == "ANN"
    assert note.uncertainties[0].item == "不确定的地方"


def test_to_note_handles_strings_in_lists():
    raw = {
        "title": "t",
        "focus_points": ["重点一", "重点二"],
        "actionables": ["直接给一句话"],
        "outline": ["第一段"],
        "uncertainties": ["存疑一"],
    }
    note = to_note(raw)
    assert len(note.focus_points) == 2
    assert note.focus_points[0].title == "重点一"
    assert len(note.actionables) == 1
    assert note.actionables[0].content == "直接给一句话"
    assert note.outline[0].heading == "第一段"


@pytest.mark.parametrize("raw", [None, [], "text", 42, {}])
def test_to_note_never_raises_on_garbage(raw):
    note = to_note(raw)
    assert note is not None
    assert isinstance(note.title, str)


def test_to_note_skips_empty_items():
    raw = {
        "title": "t",
        "focus_points": [{"title": "", "detail": ""}, {"title": "有内容", "detail": "d"}],
        "actionables": [{"title": "", "content": ""}],
    }
    note = to_note(raw)
    assert len(note.focus_points) == 1
    assert note.actionables == []


def test_to_note_actionable_falls_back_to_content_as_title():
    raw = {"title": "t", "actionables": [{"content": "只有内容没有标题"}]}
    note = to_note(raw)
    assert note.actionables[0].title == "只有内容没有标题"


def test_to_note_coerces_time_strings():
    raw = {
        "title": "t",
        "focus_points": [{"title": "t", "detail": "d", "timestamp": "01:23"}],
    }
    assert to_note(raw).focus_points[0].timestamp == pytest.approx(83.0)


def test_to_note_accepts_null_timestamps():
    raw = {"title": "t", "actionables": [{"title": "t", "content": "c"}]}
    assert to_note(raw).actionables[0].timestamp is None


# ---------------------------------------------------------------------------
# kind 收敛
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("prompt", ActionableKind.PROMPT),
        ("提示词", ActionableKind.PROMPT),
        ("PROMPT", ActionableKind.PROMPT),
        ("step", ActionableKind.STEP),
        ("步骤", ActionableKind.STEP),
        ("代码", ActionableKind.COMMAND),
        ("配置", ActionableKind.CONFIG),
        ("检查清单", ActionableKind.CHECKLIST),
        ("模板", ActionableKind.TEMPLATE),
        ("技巧", ActionableKind.TIP),
        ("完全看不懂的东西", ActionableKind.TIP),
        (None, ActionableKind.TIP),
    ],
)
def test_coerce_actionable_kind(value, expected):
    assert coerce_actionable_kind(value) is expected


# ---------------------------------------------------------------------------
# 去重与限量
# ---------------------------------------------------------------------------


def test_to_note_dedups_focus_points():
    raw = {
        "title": "t",
        "focus_points": [
            {"title": "同一个重点", "detail": "a"},
            {"title": "同一个重点 ", "detail": "b"},
        ],
    }
    assert len(to_note(raw).focus_points) == 1


def test_to_note_caps_focus_points():
    raw = {
        "title": "t",
        "focus_points": [{"title": f"重点{i}", "detail": "d"} for i in range(30)],
    }
    assert len(to_note(raw).focus_points) <= 8


def test_to_note_caps_actionables():
    raw = {
        "title": "t",
        "actionables": [{"title": f"项{i}", "content": "c"} for i in range(40)],
    }
    assert len(to_note(raw).actionables) <= 10


def test_to_note_caps_uncertainties():
    raw = {
        "title": "t",
        "uncertainties": [{"item": f"问题{i}", "reason": "r"} for i in range(20)],
    }
    assert len(to_note(raw).uncertainties) <= 5


def test_to_note_caps_outline():
    raw = {
        "title": "t",
        "outline": [
            {"timestamp": i, "heading": f"h{i}", "summary": "s"} for i in range(30)
        ],
    }
    assert len(to_note(raw).outline) <= 12


def test_to_note_dedups_concepts():
    raw = {
        "title": "t",
        "concepts": [
            {"term": "MGSD", "explanation": "系列"},
            {"term": "mgsd ", "explanation": "重复"},
        ],
    }
    assert len(to_note(raw).concepts) == 1


def test_to_note_dedups_follow_ups():
    raw = {
        "title": "t",
        "follow_ups": [
            {"question": "刹帝利是什么？", "why": "a"},
            {"question": "刹帝利是什么?", "why": "b"},
        ],
    }
    assert len(to_note(raw).follow_ups) == 1


# ---------------------------------------------------------------------------
# PartialNotes
# ---------------------------------------------------------------------------


def test_to_partial_notes_extracts_actionables():
    partial = to_partial_notes(PARTIAL_JSON)
    assert len(partial.actionables) == 1
    assert partial.actionables[0].kind == "command"
    assert partial.actionables[0].content == "CREATE INDEX ..."


@pytest.mark.parametrize("raw", [None, [], "x", {}])
def test_to_partial_notes_never_raises(raw):
    assert to_partial_notes(raw) is not None


# ---------------------------------------------------------------------------
# FocusPlan 解析
# ---------------------------------------------------------------------------


def test_parse_focus_full():
    plan = parse_focus(FOCUS_JSON)
    assert plan.video_kind == "操作教程"
    assert "向量数据库" in plan.audience_goal
    assert len(plan.focus) == 2
    assert plan.must_extract
    assert plan.ignore


def test_parse_focus_chinese_keys():
    plan = parse_focus({"类型": "教程", "目标": "学会写提示词", "关注点": ["a", "b"]})
    assert plan.video_kind == "教程"
    assert plan.audience_goal == "学会写提示词"
    assert plan.focus == ["a", "b"]


@pytest.mark.parametrize("raw", [None, [], "垃圾", {}])
def test_parse_focus_never_raises(raw):
    plan = parse_focus(raw)
    assert plan is not None
    assert isinstance(plan.focus, list)


# ---------------------------------------------------------------------------
# 元信息头部：非音频信号必须进提示词
# ---------------------------------------------------------------------------


def test_meta_header_includes_desc_and_tags():
    meta = VideoMeta(
        bvid="BV1",
        title="如何让 agent 干活",
        desc="本期讲提示词怎么写，包含三个模板。",
        tags=["AI", "提示词"],
    )
    header = build_meta_header(meta)
    assert "如何让 agent 干活" in header
    assert "本期讲提示词怎么写" in header
    assert "AI" in header and "提示词" in header


def test_meta_header_includes_chapters():
    from app.schemas import Chapter

    meta = VideoMeta(
        bvid="BV1",
        title="t",
        chapters=[Chapter(title="开场", start=0.0), Chapter(title="提示词写法", start=143.0)],
    )
    header = build_meta_header(meta)
    assert "B站已标好的章节" in header
    assert "02:23" in header
    assert "提示词写法" in header


def test_meta_header_truncates_long_desc():
    meta = VideoMeta(bvid="BV1", title="t", desc="字" * 5000)
    header = build_meta_header(meta, max_desc_chars=100)
    assert "已截断" in header
    assert len(header) < 2000


# ---------------------------------------------------------------------------
# 提示词里必须写清的约束
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("instructions", [REDUCE_INSTRUCTIONS, SINGLE_INSTRUCTIONS])
def test_prompt_demands_focus_points_and_actionables(instructions):
    """⛔ 回归守卫：这是本轮修改的核心。

    用户反馈「笔记不是很重点，我要的是怎么让 agent 干活、提示词怎么写」。
    提示词必须把「重点」和「能直接用的东西」列为最重要的一节。
    """
    assert "focus_points" in instructions
    assert "actionables" in instructions
    assert "最重要的一节" in instructions
    assert "最有价值的一节" in instructions


@pytest.mark.parametrize("instructions", [REDUCE_INSTRUCTIONS, SINGLE_INSTRUCTIONS])
def test_prompt_forbids_chronological_recap(instructions):
    """必须明确禁止「按视频讲述顺序复述」。"""
    assert "复述" in instructions or "讲述顺序" in instructions


@pytest.mark.parametrize("instructions", [REDUCE_INSTRUCTIONS, SINGLE_INSTRUCTIONS])
def test_prompt_requires_verbatim_copy(instructions):
    """提示词/命令要原样复制，不能改写成描述。"""
    assert "原样" in instructions or "直接复制" in instructions


@pytest.mark.parametrize("instructions", [REDUCE_INSTRUCTIONS, SINGLE_INSTRUCTIONS])
def test_prompt_forbids_answering_follow_ups(instructions):
    assert "只提问" in instructions
    assert "不要给答案" in instructions


@pytest.mark.parametrize("instructions", [REDUCE_INSTRUCTIONS, SINGLE_INSTRUCTIONS])
def test_prompt_requires_copying_timestamps(instructions):
    assert "原样抄写" in instructions
    assert "不要换算成秒数" in instructions


@pytest.mark.parametrize("instructions", [REDUCE_INSTRUCTIONS, SINGLE_INSTRUCTIONS])
def test_prompt_warns_against_fabrication(instructions):
    assert "编" in instructions
    assert "害了读者" in instructions or "没有可靠的外部知识" in instructions


def test_focus_prompt_asks_for_audience_goal():
    from app.notes.prompts import build_focus_prompt

    prompt = build_focus_prompt(VideoMeta(bvid="BV1", title="如何让 agent 干活"))
    assert "audience_goal" in prompt
    assert "must_extract" in prompt
    assert "如何让 agent 干活" in prompt


# ---------------------------------------------------------------------------
# 大纲时间戳质量检查
# ---------------------------------------------------------------------------


def test_outline_degenerate_detects_all_zero():
    note = to_note(
        {
            "title": "t",
            "one_liner": "o",
            "summary": "s",
            "focus_points": [{"title": "f", "detail": "d"}],
            "outline": [
                {"timestamp": "00:00", "heading": "a", "summary": "x"},
                {"timestamp": "00:00", "heading": "b", "summary": "x"},
                {"timestamp": "00:00", "heading": "c", "summary": "x"},
            ],
        }
    )
    assert _outline_timestamps_bad(note) is True
    assert "时间戳" in _note_needs_retry(note)


def test_outline_flags_non_increasing_timestamps():
    note = to_note(
        {
            "title": "t",
            "one_liner": "o",
            "summary": "s",
            "focus_points": [{"title": "f", "detail": "d"}],
            "outline": [
                {"timestamp": "00:00", "heading": "a", "summary": "x"},
                {"timestamp": "04:46", "heading": "b", "summary": "x"},
                {"timestamp": "16:13", "heading": "c", "summary": "x"},
                {"timestamp": "11:48", "heading": "d", "summary": "x"},
            ],
        }
    )
    assert _outline_timestamps_bad(note) is True
    assert "递增" in _note_needs_retry(note)


def test_outline_accepts_monotonic_timestamps():
    note = to_note(
        {
            "title": "t",
            "one_liner": "o",
            "summary": "s",
            "focus_points": [{"title": "f", "detail": "d"}],
            "outline": [
                {"timestamp": "00:00", "heading": "a", "summary": "x"},
                {"timestamp": "04:46", "heading": "b", "summary": "x"},
                {"timestamp": "16:13", "heading": "c", "summary": "x"},
            ],
        }
    )
    assert _outline_timestamps_bad(note) is False


def test_outline_degenerate_ignores_short_outlines():
    note = to_note(
        {
            "title": "t",
            "outline": [{"timestamp": "00:00", "heading": "a", "summary": "x"}],
        }
    )
    assert _outline_timestamps_bad(note) is False


# ---------------------------------------------------------------------------
# 质量门槛
# ---------------------------------------------------------------------------


def test_needs_retry_flags_thin_note():
    note = to_note({"title": "只有标题", "one_liner": "", "summary": ""})
    assert "过少" in _note_needs_retry(note)


def test_needs_retry_flags_missing_focus_and_actionables():
    """⛔ 回归守卫：光有要点但没有重点/行动项，正是用户抱怨的那种笔记。"""
    note = to_note(
        {
            "title": "t",
            "one_liner": "o",
            "summary": "s",
            "key_points": [{"title": "视频介绍了X", "detail": "..."}],
        }
    )
    reason = _note_needs_retry(note)
    assert "focus_points" in reason
    assert "actionables" in reason


def test_needs_retry_reports_all_problems_at_once():
    """把问题一次列全，重试才有意义。"""
    note = to_note(
        {
            "title": "t",
            "one_liner": "",
            "summary": "",
            "outline": [
                {"timestamp": "00:00", "heading": "a", "summary": "x"},
                {"timestamp": "00:00", "heading": "b", "summary": "x"},
                {"timestamp": "00:00", "heading": "c", "summary": "x"},
            ],
        }
    )
    reason = _note_needs_retry(note)
    assert "过少" in reason
    assert "focus_points" in reason
    assert "时间戳" in reason


def test_note_with_focus_passes_quality_gate():
    note = to_note(
        {
            "title": "t",
            "one_liner": "o",
            "summary": "s",
            "focus_points": [{"title": "重点", "detail": "怎么做"}],
            "key_points": [{"title": "k", "detail": "d"}],
            "outline": [
                {"timestamp": "00:00", "heading": "a", "summary": "x"},
                {"timestamp": "04:46", "heading": "b", "summary": "x"},
            ],
        }
    )
    assert _note_needs_retry(note) == ""


# ---------------------------------------------------------------------------
# 生成器：单次生成
# ---------------------------------------------------------------------------


async def test_single_pass_generates_note(
    settings, sample_transcript, sample_note_payload, meta
):
    provider = SchemaAwareProvider(note=sample_note_payload)
    note = await NoteGenerator(provider, settings).generate(sample_transcript, meta)

    assert note.title == "向量数据库到底解决什么问题"
    assert note.focus_points
    assert note.actionables


async def test_generator_derives_focus_first(
    settings, sample_transcript, sample_note_payload, meta
):
    """第一步必须先用元信息推断读者目标，且这一步的结果会进入后续提示词。"""
    provider = SchemaAwareProvider(note=sample_note_payload)
    await NoteGenerator(provider, settings).generate(sample_transcript, meta)

    # 第一次调用是推断读者目标
    assert "audience_goal" in provider.calls[0]
    # 后续的笔记生成提示词里带上了推断结果
    assert any("学会判断该不该上向量数据库" in c for c in provider.calls[1:])


async def test_focus_failure_does_not_break_generation(
    settings, sample_transcript, sample_note_payload, meta
):
    """推断失败不该让整个生成失败，只是少一层引导。"""

    class BrokenFocusProvider(SchemaAwareProvider):
        async def complete_json(self, *, system, user, schema=None, temperature=0.2):
            keys = set((schema or {}).get("properties", {}))
            if "audience_goal" in keys:
                raise LLMError("模拟推断失败")
            return await super().complete_json(
                system=system, user=user, schema=schema, temperature=temperature
            )

    provider = BrokenFocusProvider(note=sample_note_payload)
    note = await NoteGenerator(provider, settings).generate(sample_transcript, meta)
    assert note.title


async def test_meta_header_reaches_the_prompt(
    settings, sample_note_payload, meta, sample_transcript
):
    """标题/简介/标签/章节都要进提示词 —— 这是「抓重点」的输入。"""
    from app.schemas import Chapter

    meta.desc = "本期讲三个提示词模板"
    meta.tags = ["AI", "提示词"]
    meta.chapters = [Chapter(title="提示词写法", start=143.0)]

    provider = SchemaAwareProvider(note=sample_note_payload)
    await NoteGenerator(provider, settings).generate(sample_transcript, meta)

    focus_prompt = provider.calls[0]
    assert "本期讲三个提示词模板" in focus_prompt
    assert "提示词写法" in focus_prompt


async def test_empty_transcript_raises(settings, meta):
    with pytest.raises(LLMError):
        await NoteGenerator(SchemaAwareProvider(), settings).generate(
            Transcript(), meta
        )


async def test_generator_uses_temperature_zero(
    settings, sample_transcript, sample_note_payload, meta
):
    provider = SchemaAwareProvider(note=sample_note_payload)
    await NoteGenerator(provider, settings).generate(sample_transcript, meta)
    assert provider.temperatures and all(t == 0.0 for t in provider.temperatures)


# ---------------------------------------------------------------------------
# 生成器：修复重试
# ---------------------------------------------------------------------------


async def test_repairs_invalid_json(settings, sample_transcript, meta):
    provider = SchemaAwareProvider(raw_note="这根本不是 JSON，抱歉！")
    provider.queue_note_response(sample_note_payload_stub())
    note = await NoteGenerator(provider, settings).generate(sample_transcript, meta)
    assert note.title


def sample_note_payload_stub() -> dict:
    return {
        "title": "修好了",
        "goal": "g",
        "one_liner": "o",
        "summary": "s",
        "focus_points": [{"title": "重点", "detail": "d"}],
        "key_points": [{"title": "k", "detail": "d"}],
        "outline": [
            {"timestamp": "00:00", "heading": "a", "summary": "x"},
            {"timestamp": "04:46", "heading": "b", "summary": "x"},
        ],
    }


async def test_raises_when_repair_also_fails(settings, sample_transcript, meta):
    provider = SchemaAwareProvider(raw_note="垃圾")
    with pytest.raises(LLMOutputInvalid):
        await NoteGenerator(provider, settings).generate(sample_transcript, meta)


async def test_retries_when_note_lacks_substance(
    settings, sample_transcript, meta
):
    """只有复述式要点、没有重点/行动项时应当重试。"""
    thin = {
        "title": "只有复述",
        "goal": "g",
        "one_liner": "o",
        "summary": "s",
        "key_points": [{"title": "视频介绍了X", "detail": "..."}],
    }
    provider = SchemaAwareProvider(note=thin)
    provider.queue_note_response(thin)
    provider.queue_note_response(sample_note_payload_stub())

    note = await NoteGenerator(provider, settings).generate(sample_transcript, meta)
    assert note.focus_points


async def test_keeps_first_result_if_retry_also_bad(settings, sample_transcript, meta):
    bad = {
        "title": "首次",
        "goal": "g",
        "one_liner": "o",
        "summary": "s",
        "key_points": [{"title": "k", "detail": "d"}],
    }
    provider = SchemaAwareProvider(note=bad)
    provider.queue_note_response(bad)
    note = await NoteGenerator(provider, settings).generate(sample_transcript, meta)
    assert note.title == "首次"


async def test_progress_callback_is_called(
    settings, sample_transcript, sample_note_payload, meta
):
    updates: list[tuple[float, str]] = []
    provider = SchemaAwareProvider(note=sample_note_payload)
    await NoteGenerator(provider, settings).generate(
        sample_transcript, meta, on_progress=lambda f, m: updates.append((f, m))
    )
    assert updates
    assert all(0.0 <= f <= 1.0 for f, _ in updates)
    assert updates[-1][0] == 1.0


# ---------------------------------------------------------------------------
# 生成器：map-reduce
# ---------------------------------------------------------------------------


def _long_transcript(n: int = 7, chars: int = 700) -> Transcript:
    return Transcript(
        segments=[
            TranscriptSegment(
                start=float(i * 60),
                end=float(i * 60 + 55),
                text=f"第{i}段： " + "内容" * (chars // 2),
            )
            for i in range(n)
        ]
    )


async def test_long_transcript_uses_map_reduce(
    settings, sample_note_payload, meta
):
    settings.llm_chunk_chars = 1200
    settings.llm_chunk_overlap_chars = 0
    transcript = _long_transcript(7, 700)

    provider = SchemaAwareProvider(note=sample_note_payload)
    note = await NoteGenerator(provider, settings).generate(transcript, meta)

    assert note.title
    # 1 次推断 + 7 次 map + 1 次汇总
    assert len(provider.calls) >= 9


async def test_map_prompts_carry_focus(settings, sample_note_payload, meta):
    """每一块都要带上「读者要什么」，否则各块各抽各的。"""
    settings.llm_chunk_chars = 1200
    settings.llm_chunk_overlap_chars = 0
    transcript = _long_transcript(7, 700)

    provider = SchemaAwareProvider(note=sample_note_payload)
    await NoteGenerator(provider, settings).generate(transcript, meta)

    map_prompts = [c for c in provider.calls if "转写片段开始" in c]
    assert map_prompts
    for p in map_prompts:
        assert "读者目标" in p
        assert "学会判断该不该上向量数据库" in p


async def test_map_prompts_contain_chunk_timestamps(
    settings, sample_note_payload, meta
):
    settings.llm_chunk_chars = 1200
    settings.llm_chunk_overlap_chars = 0
    transcript = _long_transcript(7, 700)

    provider = SchemaAwareProvider(note=sample_note_payload)
    await NoteGenerator(provider, settings).generate(transcript, meta)

    map_prompts = [c for c in provider.calls if "转写片段开始" in c]
    assert "[00:00]" in map_prompts[0]


async def test_reduce_prompt_contains_partial_results(
    settings, sample_note_payload, meta
):
    settings.llm_chunk_chars = 1200
    settings.llm_chunk_overlap_chars = 0
    transcript = _long_transcript(7, 700)

    provider = SchemaAwareProvider(note=sample_note_payload)
    await NoteGenerator(provider, settings).generate(transcript, meta)

    reduce_prompts = [c for c in provider.calls if "中间结果" in c]
    assert reduce_prompts
    assert "小节" in reduce_prompts[-1]
    assert "可用的东西" in reduce_prompts[-1]  # actionables 被优先保留


async def test_map_reduce_collects_uncertainties(
    settings, sample_note_payload, meta
):
    settings.llm_chunk_chars = 1200
    settings.llm_chunk_overlap_chars = 0
    transcript = _long_transcript(7, 700)

    provider = SchemaAwareProvider(note=sample_note_payload)
    note = await NoteGenerator(provider, settings).generate(transcript, meta)
    items = {u.item for u in note.uncertainties}
    assert "转写不清的地方" in items
    assert len(note.uncertainties) <= 5


async def test_map_reduce_raises_when_nothing_extracted(settings, meta):
    settings.llm_chunk_chars = 1200
    settings.llm_chunk_overlap_chars = 0
    transcript = _long_transcript(7, 700)

    empty = {"outline": [], "key_points": [], "concepts": [], "uncertain": []}
    provider = SchemaAwareProvider(partial=empty)

    with pytest.raises(LLMError):
        await NoteGenerator(provider, settings).generate(transcript, meta)


# ---------------------------------------------------------------------------
# 汇总提示词的体量控制
# ---------------------------------------------------------------------------


def _fat_partial() -> dict:
    return {
        "outline": [
            {"timestamp": 0, "heading": f"章节{j}", "summary": "描" * 120}
            for j in range(6)
        ],
        "key_points": [
            {"title": f"要点{j}", "detail": "细" * 120, "source": "video"}
            for j in range(6)
        ],
        "concepts": [
            {"term": f"术语{j}", "explanation": "解" * 120} for j in range(4)
        ],
        "uncertain": ["疑" * 100 for _ in range(3)],
    }


def test_split_by_budget_keeps_each_batch_small(settings, meta):
    """⛔ 回归守卫：56 分钟的视频曾因汇总提示词超出上下文而超时失败。"""
    from app.notes.chunking import estimate_tokens

    gen = NoteGenerator(FakeProvider([]), settings)
    partials = [to_partial_notes(_fat_partial()) for _ in range(8)]
    groups = gen._split_by_budget(partials, gen._reduce_budget, meta, "")

    assert len(groups) > 1
    for group in groups:
        prompt = build_reduce_prompt(
            group, meta, transcript_source_label="", compact=True
        )
        assert estimate_tokens(prompt) <= gen._reduce_budget * 1.2


def test_split_by_budget_preserves_all_items(settings, meta):
    gen = NoteGenerator(FakeProvider([]), settings)
    partials = [to_partial_notes(_fat_partial()) for _ in range(8)]
    groups = gen._split_by_budget(partials, gen._reduce_budget, meta, "")
    assert sum(len(g) for g in groups) == len(partials)


def test_split_by_budget_single_oversized_item_is_its_own_group(settings, meta):
    gen = NoteGenerator(FakeProvider([]), settings)
    huge = [to_partial_notes(_fat_partial())]
    groups = gen._split_by_budget(huge, 10, meta, "")
    assert groups == [huge]


def test_reduce_budget_leaves_room_for_output(settings):
    gen = NoteGenerator(FakeProvider([]), settings)
    assert gen._reduce_budget < settings.llm_num_ctx
