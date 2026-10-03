"""笔记生成流水线测试。

用假模型驱动，**不联网、不用真模型**。
重点是验证：模型输出的各种不规范形态都能被吸收，
以及「分块 map-reduce」「修复重试」「薄结果重试」三条兜底路径真的生效。
"""

from __future__ import annotations

import json

import pytest

from app.errors import LLMError, LLMOutputInvalid
from app.notes.pipeline import (
    NoteGenerator,
    _note_needs_retry,
    _outline_timestamps_bad,
    to_note,
    to_partial_notes,
)
from app.notes.prompts import (
    REDUCE_INSTRUCTIONS,
    SINGLE_INSTRUCTIONS,
    build_reduce_prompt,
    build_single_prompt,
)
from app.schemas import Transcript, TranscriptSegment, VideoMeta

from .conftest import FakeProvider


@pytest.fixture
def meta() -> VideoMeta:
    return VideoMeta(
        bvid="BV1GJ411x7h7",
        title="向量数据库入门",
        owner_name="某UP主",
        duration=1800,
    )


# ---------------------------------------------------------------------------
# 模型输出 -> Note
# ---------------------------------------------------------------------------


def test_to_note_accepts_camel_case_keys(sample_note_payload):
    note = to_note(sample_note_payload)
    assert note.title == "向量数据库到底解决什么问题"
    assert len(note.key_points) == 1
    assert note.key_points[0].title == "ANN 用准确率换速度"


def test_to_note_accepts_chinese_keys():
    raw = {
        "标题": "中文键名",
        "一句话总结": "结论",
        "摘要": "摘要内容",
        "要点": [{"要点": "点1", "说明": "说明1"}],
        "概念": [{"概念": "ANN", "解释": "近似最近邻"}],
        "存疑": ["不确定的地方"],
        "标签": ["标签1"],
    }
    note = to_note(raw)
    assert note.title == "中文键名"
    assert note.one_liner == "结论"
    assert note.key_points[0].title == "点1"
    assert note.concepts[0].term == "ANN"
    assert note.uncertainties[0].item == "不确定的地方"
    assert note.tags == ["标签1"]


def test_to_note_handles_string_items_in_lists():
    raw = {
        "title": "t",
        "outline": ["第一段", "第二段"],
        "key_points": ["要点一", "要点二"],
        "concepts": ["ANN"],
        "uncertainties": ["存疑一"],
    }
    note = to_note(raw)
    assert len(note.outline) == 2
    assert note.outline[0].heading == "第一段"
    assert len(note.key_points) == 2
    assert note.concepts[0].term == "ANN"
    assert note.uncertainties[0].item == "存疑一"


def test_to_note_skips_empty_items():
    raw = {
        "title": "t",
        "key_points": [{"title": "", "detail": ""}, {"title": "有内容", "detail": "d"}],
        "concepts": [{"term": "", "explanation": "x"}],
    }
    note = to_note(raw)
    assert len(note.key_points) == 1
    assert note.concepts == []


def test_to_note_parses_follow_ups():
    """待查清单只收问题与原因，不再有「答案」字段。"""
    raw = {
        "title": "t",
        "follow_ups": [
            {
                "question": "刹帝利出自哪部作品？",
                "why": "视频默认你熟悉 UC 系谱",
                "where": "机动战士高达UC",
            }
        ],
    }
    note = to_note(raw)
    assert len(note.follow_ups) == 1
    assert note.follow_ups[0].question == "刹帝利出自哪部作品？"
    assert note.follow_ups[0].where == "机动战士高达UC"


def test_to_note_follow_up_without_where():
    """拿不准搜索词时应当留空，而不是硬编一个。"""
    raw = {"title": "t", "follow_ups": [{"question": "q", "why": "w"}]}
    assert to_note(raw).follow_ups[0].where is None


def test_to_note_follow_up_falls_back_to_why_as_question():
    raw = {"title": "t", "follow_ups": [{"why": "视频没解释这个"}]}
    assert to_note(raw).follow_ups[0].question == "视频没解释这个"


def test_to_note_coerces_time_strings():
    raw = {
        "title": "t",
        "key_points": [{"title": "t", "detail": "d", "timestamp": "01:23"}],
    }
    assert to_note(raw).key_points[0].timestamp == pytest.approx(83.0)


def test_to_note_clamps_bad_source_values():
    raw = {"title": "t", "key_points": [{"title": "t", "detail": "d", "source": "???"}]}
    assert to_note(raw).key_points[0].source == "video"


@pytest.mark.parametrize("raw", [None, [], "text", 42, {}])
def test_to_note_never_raises_on_garbage(raw):
    note = to_note(raw)
    assert note is not None
    assert note.title == "" or isinstance(note.title, str)


def test_to_note_handles_single_dict_where_list_expected():
    """模型有时只给一个对象而不是数组。"""
    raw = {"title": "t", "key_points": {"title": "只有一个", "detail": "d"}}
    note = to_note(raw)
    assert len(note.key_points) == 1


def test_to_note_handles_uncertainty_dicts():
    raw = {
        "title": "t",
        "uncertainties": [{"item": "问题", "reason": "原因"}],
    }
    note = to_note(raw)
    assert note.uncertainties[0].item == "问题"
    assert note.uncertainties[0].reason == "原因"


# ---------------------------------------------------------------------------
# 去重与限量（实测模型会凑字数）
# ---------------------------------------------------------------------------


def test_to_note_dedups_concepts():
    raw = {
        "title": "t",
        "concepts": [
            {"term": "MGSD", "explanation": "系列"},
            {"term": "mgsd ", "explanation": "重复"},
            {"term": "MGSD系列", "explanation": "另一个"},
        ],
    }
    note = to_note(raw)
    assert len(note.concepts) == 2  # "MGSD" 与 "mgsd " 视为同一个



def test_to_note_dedups_follow_ups():
    raw = {
        "title": "t",
        "follow_ups": [
            {"question": "刹帝利是什么？", "why": "a"},
            {"question": "刹帝利是什么?", "why": "b"},
        ],
    }
    # 归一化后（去标点）视为同一个问题
    assert len(to_note(raw).follow_ups) == 1


def test_to_note_keeps_distinct_follow_ups():
    raw = {
        "title": "t",
        "follow_ups": [
            {"question": "问题一？", "why": "a"},
            {"question": "问题二？", "why": "b"},
        ],
    }
    assert len(to_note(raw).follow_ups) == 2


def test_to_note_caps_follow_ups():
    raw = {
        "title": "t",
        "follow_ups": [{"question": f"问题{i}？", "why": "w"} for i in range(20)],
    }
    assert len(to_note(raw).follow_ups) <= 5



def test_to_note_caps_uncertainties():
    raw = {
        "title": "t",
        "uncertainties": [{"item": f"问题{i}", "reason": "r"} for i in range(20)],
    }
    assert len(to_note(raw).uncertainties) <= 5


def test_to_note_caps_key_points():
    raw = {
        "title": "t",
        "key_points": [{"title": f"要点{i}", "detail": "d"} for i in range(30)],
    }
    assert len(to_note(raw).key_points) <= 12


def test_to_note_caps_concepts():
    raw = {
        "title": "t",
        "concepts": [{"term": f"术语{i}", "explanation": "e"} for i in range(20)],
    }
    assert len(to_note(raw).concepts) <= 8


def test_to_note_parses_string_timestamps():
    """schema 里 timestamp 是 "mm:ss" 字符串，必须能正确转成秒。"""
    raw = {
        "title": "t",
        "outline": [{"timestamp": "12:34", "heading": "h", "summary": "s"}],
        "key_points": [{"title": "t", "detail": "d", "timestamp": "01:30"}],
    }
    note = to_note(raw)
    assert note.outline[0].timestamp == pytest.approx(754.0)
    assert note.key_points[0].timestamp == pytest.approx(90.0)


def test_to_note_accepts_null_timestamps():
    raw = {
        "title": "t",
        "key_points": [{"title": "t", "detail": "d", "timestamp": None}],
    }
    assert to_note(raw).key_points[0].timestamp is None


# ---------------------------------------------------------------------------
# 提示词里必须写清的约束
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("instructions", [REDUCE_INSTRUCTIONS, SINGLE_INSTRUCTIONS])
def test_prompt_separates_concepts_from_follow_ups(instructions):
    """⛔ 回归守卫。

    实测教训：模型在「解释术语」时会编造外部事实
    （同一个术语的全称三次运行给出三种答案）。
    所以提示词必须把两节职责划清：
    concepts 只依据转写解释，follow_ups 只提问不给答案。
    """
    assert "concepts" in instructions
    assert "follow_ups" in instructions


@pytest.mark.parametrize("instructions", [REDUCE_INSTRUCTIONS, SINGLE_INSTRUCTIONS])
def test_prompt_forbids_answering_follow_ups(instructions):
    """待查清单必须只提问 —— 这是防幻觉的核心设计。"""
    assert "只提问" in instructions
    assert "不要给答案" in instructions


@pytest.mark.parametrize("instructions", [REDUCE_INSTRUCTIONS, SINGLE_INSTRUCTIONS])
def test_prompt_forbids_external_facts_in_concepts(instructions):
    """概念解释也只能用转写里有的内容。"""
    assert "不要补外部事实" in instructions


@pytest.mark.parametrize("instructions", [REDUCE_INSTRUCTIONS, SINGLE_INSTRUCTIONS])
def test_prompt_requires_copying_timestamps(instructions):
    """时间戳必须是「照抄标记」，不是让模型算。"""
    assert "原样抄写" in instructions
    assert "不要换算成秒数" in instructions


@pytest.mark.parametrize("instructions", [REDUCE_INSTRUCTIONS, SINGLE_INSTRUCTIONS])
def test_prompt_warns_against_fabrication(instructions):
    """模型编造事实是实测最危险的失败模式，必须有明确约束。"""
    assert "编" in instructions
    assert "害了读者" in instructions or "没有可靠的外部知识" in instructions


@pytest.mark.parametrize("instructions", [REDUCE_INSTRUCTIONS, SINGLE_INSTRUCTIONS])
def test_prompt_demands_distinct_increasing_timestamps(instructions):
    """⛔ 回归守卫。

    实测症状：模型偷懒时把大纲所有章节都标成 00:00，导致时间戳完全失效
    （而时间戳是「跳回原视频核对」的唯一入口）。提示词必须明确要求
    时间戳各不相同且递增。
    """
    assert "各不相同" in instructions
    assert "递增" in instructions


# ---------------------------------------------------------------------------
# 大纲时间戳退化检测
# ---------------------------------------------------------------------------


def test_outline_degenerate_detects_all_zero():
    note = to_note(
        {
            "title": "t",
            "one_liner": "o",
            "summary": "s",
            "outline": [
                {"timestamp": "00:00", "heading": "a", "summary": "x"},
                {"timestamp": "00:00", "heading": "b", "summary": "x"},
                {"timestamp": "00:00", "heading": "c", "summary": "x"},
            ],
        }
    )
    assert _outline_timestamps_bad(note) is True
    assert "时间戳" in _note_needs_retry(note)


def test_outline_not_degenerate_when_distinct():
    note = to_note(
        {
            "title": "t",
            "one_liner": "o",
            "summary": "s",
            "key_points": [{"title": "k", "detail": "d"}],
            "outline": [
                {"timestamp": "00:00", "heading": "a", "summary": "x"},
                {"timestamp": "02:23", "heading": "b", "summary": "x"},
                {"timestamp": "05:46", "heading": "c", "summary": "x"},
            ],
        }
    )
    assert _outline_timestamps_bad(note) is False
    assert _note_needs_retry(note) == ""


def test_outline_degenerate_ignores_short_outlines():
    """只有一两条大纲时不判定退化（样本太小）。"""
    note = to_note(
        {
            "title": "t",
            "one_liner": "o",
            "summary": "s",
            "outline": [{"timestamp": "00:00", "heading": "a", "summary": "x"}],
        }
    )
    assert _outline_timestamps_bad(note) is False


def test_outline_flags_non_increasing_timestamps():
    """⛔ 回归守卫。

    实测症状：模型给出 [00:00] [04:46] [05:46] [16:13] [11:48] ——
    最后一条比倒数第二条还早，顺序乱了，大纲的导航作用就废了。
    """
    note = to_note(
        {
            "title": "t",
            "one_liner": "o",
            "summary": "s",
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
            "key_points": [{"title": "k", "detail": "d"}],
            "outline": [
                {"timestamp": "00:00", "heading": "a", "summary": "x"},
                {"timestamp": "04:46", "heading": "b", "summary": "x"},
                {"timestamp": "16:13", "heading": "c", "summary": "x"},
            ],
        }
    )
    assert _outline_timestamps_bad(note) is False


def test_needs_retry_flags_thin_note():
    note = to_note({"title": "只有标题", "one_liner": "", "summary": ""})
    assert "过少" in _note_needs_retry(note)


async def test_generator_retries_when_outline_timestamps_bad(
    settings, sample_transcript, sample_note_payload, meta
):
    """大纲时间戳退化时必须自动重试，而不是把坏结果交给用户。"""
    degenerate = {
        "title": "t",
        "one_liner": "一句话",
        "summary": "摘要",
        "key_points": [{"title": "k", "detail": "d"}],
        "outline": [
            {"timestamp": "00:00", "heading": "a", "summary": "x"},
            {"timestamp": "00:00", "heading": "b", "summary": "x"},
            {"timestamp": "00:00", "heading": "c", "summary": "x"},
        ],
    }
    provider = FakeProvider([degenerate, sample_note_payload])
    note = await NoteGenerator(provider, settings).generate(sample_transcript, meta)

    assert len(provider.calls) == 2
    assert "时间戳" in provider.calls[1]
    assert not _outline_timestamps_bad(note)


async def test_generator_keeps_first_result_if_retry_also_bad(
    settings, sample_transcript, meta
):
    """重试也不合格时保留首次结果，不要用更差的结果覆盖。"""
    degenerate = {
        "title": "首次",
        "one_liner": "一句话",
        "summary": "摘要",
        "key_points": [{"title": "k", "detail": "d"}],
        "outline": [
            {"timestamp": "00:00", "heading": "a", "summary": "x"},
            {"timestamp": "00:00", "heading": "b", "summary": "x"},
            {"timestamp": "00:00", "heading": "c", "summary": "x"},
        ],
    }
    provider = FakeProvider([degenerate, degenerate])
    note = await NoteGenerator(provider, settings).generate(sample_transcript, meta)
    assert note.title == "首次"


async def test_generator_uses_temperature_zero(
    settings, sample_transcript, sample_note_payload, meta
):
    """事实整理任务必须用 temperature=0，减少时对时错的不确定性。"""
    captured: list[float] = []

    class RecordingProvider(FakeProvider):
        async def complete_json(self, *, system, user, schema=None, temperature=0.2):
            captured.append(temperature)
            return await super().complete_json(
                system=system, user=user, schema=schema, temperature=temperature
            )

    provider = RecordingProvider([sample_note_payload])
    await NoteGenerator(provider, settings).generate(sample_transcript, meta)
    assert captured and all(t == 0.0 for t in captured)


# ---------------------------------------------------------------------------
# 模型输出 -> PartialNotes
# ---------------------------------------------------------------------------


def test_to_partial_notes(sample_note_payload):
    partial = to_partial_notes(sample_note_payload)
    assert len(partial.key_points) == 1
    assert len(partial.concepts) == 1
    assert partial.outline[0].heading == "开场"


def test_to_partial_notes_collects_uncertain():
    partial = to_partial_notes({"uncertain": ["转写不清"]})
    assert partial.uncertain == ["转写不清"]


@pytest.mark.parametrize("raw", [None, [], "x", {}])
def test_to_partial_notes_never_raises(raw):
    assert to_partial_notes(raw) is not None


# ---------------------------------------------------------------------------
# 生成器：单次生成
# ---------------------------------------------------------------------------


async def test_single_pass_generates_note(settings, sample_transcript, sample_note_payload, meta):
    provider = FakeProvider([sample_note_payload])
    gen = NoteGenerator(provider, settings)

    note = await gen.generate(sample_transcript, meta)

    assert note.title == "向量数据库到底解决什么问题"
    assert note.one_liner
    assert len(note.key_points) == 1
    assert len(provider.calls) == 1


async def test_single_pass_prompt_contains_transcript(settings, sample_transcript, sample_note_payload, meta):
    provider = FakeProvider([sample_note_payload])
    await NoteGenerator(provider, settings).generate(sample_transcript, meta)

    prompt = provider.calls[0]
    assert "向量数据库" in prompt
    assert "向量数据库入门" in prompt  # 标题
    assert "[00:00]" in prompt  # 时间戳


async def test_empty_transcript_raises(settings, meta):
    provider = FakeProvider([])
    with pytest.raises(LLMError):
        await NoteGenerator(provider, settings).generate(Transcript(), meta)


async def test_blank_transcript_raises(settings, meta):
    transcript = Transcript(
        segments=[TranscriptSegment(start=0, end=1, text="   ")]
    )
    with pytest.raises(LLMError):
        await NoteGenerator(FakeProvider([]), settings).generate(transcript, meta)


# ---------------------------------------------------------------------------
# 修复重试
# ---------------------------------------------------------------------------


async def test_repairs_invalid_json(settings, sample_transcript, sample_note_payload, meta):
    provider = FakeProvider(["这根本不是 JSON，抱歉！", sample_note_payload])
    gen = NoteGenerator(provider, settings)

    note = await gen.generate(sample_transcript, meta)

    assert note.one_liner
    assert len(provider.calls) == 2
    assert "修正后的 JSON" in provider.calls[1]


async def test_raises_when_repair_also_fails(settings, sample_transcript, meta):
    provider = FakeProvider(["垃圾", "还是垃圾"])
    with pytest.raises(LLMOutputInvalid):
        await NoteGenerator(provider, settings).generate(sample_transcript, meta)


async def test_repair_handles_fenced_json(settings, sample_transcript, sample_note_payload, meta):
    fenced = f"```json\n{json.dumps(sample_note_payload, ensure_ascii=False)}\n```"
    provider = FakeProvider([fenced])
    note = await NoteGenerator(provider, settings).generate(sample_transcript, meta)
    assert note.title


# ---------------------------------------------------------------------------
# 薄结果重试
# ---------------------------------------------------------------------------


async def test_retries_when_note_is_thin(settings, sample_transcript, sample_note_payload, meta):
    thin = {"title": "只有标题", "one_liner": "", "summary": "", "key_points": []}
    provider = FakeProvider([thin, sample_note_payload])

    note = await NoteGenerator(provider, settings).generate(sample_transcript, meta)

    assert not note.is_thin()
    assert len(provider.calls) == 2
    assert "过少" in provider.calls[1]


# ---------------------------------------------------------------------------
# 分块 map-reduce
# ---------------------------------------------------------------------------


def _long_transcript(n: int = 7, chars: int = 700) -> Transcript:
    """造一段长到必须分块的转写。"""
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


PARTIAL_JSON = {
    "outline": [{"timestamp": 0, "heading": "小节", "summary": "讲了一点东西"}],
    "key_points": [
        {"title": "要点", "detail": "细节说明", "source": "video", "timestamp": 0}
    ],
    "concepts": [{"term": "术语", "explanation": "解释", "source": "video"}],
    "uncertain": ["转写不清的地方"],
}


async def test_long_transcript_uses_map_reduce(settings, sample_note_payload, meta):
    settings.llm_chunk_chars = 1200
    settings.llm_chunk_overlap_chars = 0

    transcript = _long_transcript(7, 700)
    assert transcript.char_count > settings.llm_chunk_chars * 3  # 确保会分块

    # 7 个分段 + 1 次汇总
    provider = FakeProvider([PARTIAL_JSON] * 7 + [sample_note_payload])
    gen = NoteGenerator(provider, settings)

    note = await gen.generate(transcript, meta)

    assert note.title == "向量数据库到底解决什么问题"
    # 至少发生了 8 次调用（7 map + 1 reduce）
    assert len(provider.calls) >= 8


async def test_map_prompts_contain_chunk_timestamps(settings, sample_note_payload, meta):
    settings.llm_chunk_chars = 1200
    settings.llm_chunk_overlap_chars = 0
    transcript = _long_transcript(7, 700)

    provider = FakeProvider([PARTIAL_JSON] * 7 + [sample_note_payload])
    await NoteGenerator(provider, settings).generate(transcript, meta)

    # 前面的 map 调用里应能看到时间戳标记
    assert "[00:00]" in provider.calls[0]
    assert "第 1/7 段" in provider.calls[0] or "1/7" in provider.calls[0]


async def test_reduce_prompt_contains_partial_results(settings, sample_note_payload, meta):
    settings.llm_chunk_chars = 1200
    settings.llm_chunk_overlap_chars = 0
    transcript = _long_transcript(7, 700)

    provider = FakeProvider([PARTIAL_JSON] * 7 + [sample_note_payload])
    await NoteGenerator(provider, settings).generate(transcript, meta)

    reduce_prompt = provider.calls[-1]
    assert "中间结果" in reduce_prompt
    assert "小节" in reduce_prompt  # 来自 PARTIAL_JSON


async def test_map_reduce_collects_uncertainties(settings, sample_note_payload, meta):
    """分段里的存疑点不应在汇总时被丢掉。"""
    settings.llm_chunk_chars = 1200
    settings.llm_chunk_overlap_chars = 0
    transcript = _long_transcript(7, 700)

    provider = FakeProvider([PARTIAL_JSON] * 7 + [sample_note_payload])
    note = await NoteGenerator(provider, settings).generate(transcript, meta)

    items = {u.item for u in note.uncertainties}
    assert "转写不清的地方" in items


async def test_map_reduce_raises_when_nothing_extracted(settings, meta):
    settings.llm_chunk_chars = 1200
    settings.llm_chunk_overlap_chars = 0
    transcript = _long_transcript(7, 700)

    empty_partial = {"outline": [], "key_points": [], "concepts": [], "uncertain": []}
    provider = FakeProvider([empty_partial] * 7)

    with pytest.raises(LLMError) as exc:
        await NoteGenerator(provider, settings).generate(transcript, meta)
    assert "没有抽取" in str(exc.value) or "有效信息" in str(exc.value)


async def test_progress_callback_is_called(settings, sample_transcript, sample_note_payload, meta):
    updates: list[tuple[float, str]] = []
    provider = FakeProvider([sample_note_payload])

    await NoteGenerator(provider, settings).generate(
        sample_transcript, meta, on_progress=lambda f, m: updates.append((f, m))
    )

    assert updates
    assert all(0.0 <= f <= 1.0 for f, _ in updates)
    assert updates[-1][0] == 1.0


async def test_call_count_is_tracked(settings, sample_transcript, sample_note_payload, meta):
    provider = FakeProvider([sample_note_payload])
    gen = NoteGenerator(provider, settings)
    await gen.generate(sample_transcript, meta)
    assert gen.call_count == 1
