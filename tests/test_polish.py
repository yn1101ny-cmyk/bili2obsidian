"""「本地出稿 + 云端单次润色」的测试。

原则：绝不联网。云端提供方用假实现，提示词与校验用纯函数验证。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.config import Settings
from app.llm.factory import build_polish_provider
from app.notes.pipeline import to_note
from app.notes.polish import (
    build_polish_prompt,
    polish_note,
    validate_polish,
)
from app.pipeline import NotePipeline
from app.schemas import Note, VideoMeta
from tests.conftest import FakeProvider


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _meta() -> VideoMeta:
    return VideoMeta(
        bvid="BV1", title="向量数据库到底解决什么问题", owner_name="某人", duration=600
    )


def _draft(payload: dict[str, Any]) -> Note:
    return to_note(payload)


def _reworded(note: Note) -> Note:
    """在初稿基础上改几句表达，保留全部时间戳与 ``[mm:ss]`` 标记。"""
    out = note.model_copy(deep=True)
    out.summary = "先给结论：向量数据库不是银弹，它只做高维相似度检索。"
    if out.focus_points:
        out.focus_points[0].detail = (
            "如果你的问题不是「找最像的那几条」，传统库加索引就够了，"
            "上向量库只是多一份运维负担。"
        )
    if out.key_points:
        out.key_points[0].detail = (
            "精确最近邻在高维上太慢，ANN 允许少量误差，换来的是数量级提速。"
        )
    return out


def _as_payload(note: Note) -> dict[str, Any]:
    return note.model_dump(mode="json")


def _settings(tmp_path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "data_dir": tmp_path / "data",
        "bili_min_interval": 0.0,
        "llm_provider": "ollama",
        "allow_cloud": True,
        "polish_enabled": True,
        "openai_api_key": "sk-test",
    }
    values.update(overrides)
    s = Settings(**values)
    s.ensure_dirs()
    return s


class _RemoteProvider(FakeProvider):
    name = "云端/假"
    is_remote = True


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------


def test_prompt_states_hard_constraints(sample_note_payload):
    prompt = build_polish_prompt(
        _draft(sample_note_payload), _meta(), instructions="短句为主"
    )

    assert "不得新增" in prompt
    assert "时间戳" in prompt
    assert "[mm:ss]" in prompt
    assert "代码块、命令、参数、提示词原文" in prompt
    assert "短句为主" in prompt
    # 初稿本身必须带进去，否则云端无从改起
    assert sample_note_payload["one_liner"] in prompt


def test_prompt_omits_extra_section_without_instructions(sample_note_payload):
    prompt = build_polish_prompt(_draft(sample_note_payload), _meta())
    assert "用户的额外要求" not in prompt


# ---------------------------------------------------------------------------
# 校验：守住「准」这条底线
# ---------------------------------------------------------------------------


def test_identity_note_passes_validation(sample_note_payload):
    note = _draft(sample_note_payload)
    assert validate_polish(note, note) == ""


def test_losing_a_timestamp_is_rejected(sample_note_payload):
    draft = _draft(sample_note_payload)
    polished = draft.model_copy(deep=True)
    # 15.2s 只出现在 concepts 里，丢掉它就能被查出来
    polished.concepts = [
        c.model_copy(update={"timestamp": None}) for c in polished.concepts
    ]
    assert "时间戳" in validate_polish(draft, polished)


def test_losing_clock_marks_is_rejected(sample_note_payload):
    """正文里的 ``[08:00]`` 标记也算时间戳，丢了同样拒绝。"""
    draft = _draft(sample_note_payload)
    draft.focus_points[0].detail += "（[08:00] 这里）"
    polished = _draft(sample_note_payload)  # 原稿没有这个标记
    assert "时间标记" in validate_polish(draft, polished)


def test_collapsing_the_note_is_rejected(sample_note_payload):
    draft = _draft(sample_note_payload)
    thin = Note(title="x", goal="", one_liner="", summary="太短了")
    assert validate_polish(draft, thin)


# ---------------------------------------------------------------------------
# 单次调用
# ---------------------------------------------------------------------------


async def test_polish_calls_cloud_exactly_once(sample_note_payload, tmp_path):
    draft = _draft(sample_note_payload)
    provider = FakeProvider([_as_payload(_reworded(draft))])

    polished = await polish_note(
        draft, _meta(), settings=_settings(tmp_path), provider=provider
    )

    assert polished is not None
    assert len(provider.calls) == 1
    assert "向量数据库不是银弹" in polished.summary


async def test_polish_rejects_invalid_json(sample_note_payload, tmp_path):
    provider = FakeProvider(["这不是 JSON"])

    polished = await polish_note(
        _draft(sample_note_payload),
        _meta(),
        settings=_settings(tmp_path),
        provider=provider,
    )

    assert polished is None
    assert len(provider.calls) == 1


async def test_polish_rejects_dropped_timestamps(sample_note_payload, tmp_path):
    draft = _draft(sample_note_payload)
    bad = _reworded(draft)
    bad.concepts = [c.model_copy(update={"timestamp": None}) for c in bad.concepts]
    provider = FakeProvider([_as_payload(bad)])

    polished = await polish_note(
        draft,
        _meta(),
        settings=_settings(tmp_path),
        provider=provider,
    )

    assert polished is None


async def test_polish_skips_oversized_draft_without_calling(
    sample_note_payload, tmp_path
):
    draft = _draft(sample_note_payload)
    draft.summary = "字" * 6000  # 远超 1000 tokens
    provider = FakeProvider(["{}"])

    polished = await polish_note(
        draft,
        _meta(),
        settings=_settings(tmp_path, polish_max_input_tokens=1000),
        provider=provider,
    )

    assert polished is None
    assert provider.calls == []


# ---------------------------------------------------------------------------
# 提供方构造
# ---------------------------------------------------------------------------


def test_polish_provider_requires_explicit_consent(tmp_path):
    settings = _settings(tmp_path, allow_cloud=False)
    assert build_polish_provider(settings) is None


def test_polish_provider_requires_api_key(tmp_path):
    settings = _settings(tmp_path, openai_api_key=None, polish_api_key=None)
    assert build_polish_provider(settings) is None


def test_polish_provider_disabled_by_default(tmp_path):
    settings = _settings(tmp_path, polish_enabled=False)
    assert build_polish_provider(settings) is None


def test_polish_provider_is_skipped_when_cloud_generates_note(tmp_path):
    """整篇已由云端生成时，再润色一次纯属白花钱。"""
    settings = _settings(tmp_path, llm_provider="openai")
    assert build_polish_provider(settings) is None


def test_polish_provider_reuses_openai_config(tmp_path):
    settings = _settings(tmp_path)
    provider = build_polish_provider(settings)
    assert provider is not None
    assert provider.model == settings.openai_model
    assert provider.max_tokens == settings.polish_max_tokens


def test_polish_provider_prefers_dedicated_settings(tmp_path):
    settings = _settings(
        tmp_path,
        polish_model="cheap-model",
        polish_api_key="sk-dedicated",
        polish_base_url="https://example.invalid/v1",
    )
    provider = build_polish_provider(settings)
    assert provider is not None
    assert provider.model == "cheap-model"
    assert provider.api_key == "sk-dedicated"


# ---------------------------------------------------------------------------
# 流水线接线
# ---------------------------------------------------------------------------


def _report_sink() -> tuple[list[tuple[str, float, str]], Any]:
    events: list[tuple[str, float, str]] = []

    def report(stage, progress, message):
        events.append((str(stage), progress, message))

    return events, report


async def test_pipeline_skips_polish_when_disabled(sample_note_payload, tmp_path):
    settings = _settings(tmp_path, polish_enabled=False)
    pipeline = NotePipeline(settings)
    note = _draft(sample_note_payload)
    events, report = _report_sink()
    notices: list[str] = []

    result = await pipeline._maybe_polish(
        note, _meta(), FakeProvider([]), report, notices=notices
    )

    assert result is note
    assert notices == []
    assert events == []


async def test_pipeline_warns_without_allow_cloud(sample_note_payload, tmp_path):
    settings = _settings(tmp_path, allow_cloud=False)
    pipeline = NotePipeline(settings)
    note = _draft(sample_note_payload)
    events, report = _report_sink()
    notices: list[str] = []

    result = await pipeline._maybe_polish(
        note, _meta(), FakeProvider([]), report, notices=notices
    )

    assert result is note
    assert any("ALLOW_CLOUD" in n for n in notices)


async def test_pipeline_skips_polish_when_provider_is_remote(
    sample_note_payload, tmp_path
):
    pipeline = NotePipeline(_settings(tmp_path))
    note = _draft(sample_note_payload)
    events, report = _report_sink()
    notices: list[str] = []

    result = await pipeline._maybe_polish(
        note, _meta(), _RemoteProvider([]), report, notices=notices
    )

    assert result is note
    assert notices == []


async def test_pipeline_replaces_note_with_polished_version(
    sample_note_payload, tmp_path, monkeypatch
):
    pipeline = NotePipeline(_settings(tmp_path))
    draft = _draft(sample_note_payload)
    polished = _reworded(draft)
    events, report = _report_sink()
    notices: list[str] = []

    async def fake_polish(note, meta, *, settings, provider):
        return polished

    monkeypatch.setattr("app.pipeline.polish_note", fake_polish)

    result = await pipeline._maybe_polish(
        draft, _meta(), FakeProvider([]), report, notices=notices
    )

    assert result is polished
    assert any("润色" in n for n in notices)
    assert any("1 次调用" in n for n in notices)


async def test_pipeline_keeps_draft_when_polish_rejected(
    sample_note_payload, tmp_path, monkeypatch
):
    pipeline = NotePipeline(_settings(tmp_path))
    draft = _draft(sample_note_payload)
    events, report = _report_sink()
    notices: list[str] = []

    async def fake_polish(note, meta, *, settings, provider):
        return None

    monkeypatch.setattr("app.pipeline.polish_note", fake_polish)

    result = await pipeline._maybe_polish(
        draft, _meta(), FakeProvider([]), report, notices=notices
    )

    assert result is draft
    assert any("保留本地稿" in n for n in notices)
