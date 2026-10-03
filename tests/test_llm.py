"""LLM 适配层测试。

模型输出永远不可靠，所以这里的重点是**宽容解析**：字段名变体、
类型漂移、多余包裹，都必须被正确吸收而不是抛异常。
所有请求都用 respx 拦掉，**不联网**。
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.errors import LLMError, LLMOutputInvalid
from app.llm.base import (
    clamp_source,
    coerce_float,
    coerce_str,
    coerce_str_list,
    extract_json,
    truncate_middle,
)
from app.llm.ollama_provider import OllamaProvider
from app.notes.prompts import PartialNotes, flatten_schema
from app.schemas import Note


# ---------------------------------------------------------------------------
# extract_json
# ---------------------------------------------------------------------------


def test_extract_json_plain():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_with_whitespace():
    assert extract_json('\n  {"a": 1}  \n') == {"a": 1}


def test_extract_json_fenced():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_fenced_without_language():
    assert extract_json('```\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_with_surrounding_prose():
    raw = '好的，这是结果：\n```json\n{"a": 1}\n```\n希望有帮助！'
    assert extract_json(raw) == {"a": 1}


def test_extract_json_embedded_without_fence():
    assert extract_json('some text {"a": 1} more text') == {"a": 1}


def test_extract_json_nested_braces():
    raw = 'prefix {"a": {"b": [1, 2]}} suffix'
    assert extract_json(raw) == {"a": {"b": [1, 2]}}


@pytest.mark.parametrize("raw", ["", "   ", "not json at all", "{unclosed"])
def test_extract_json_raises_on_garbage(raw):
    with pytest.raises(LLMOutputInvalid):
        extract_json(raw)


def test_extract_json_raises_on_none():
    with pytest.raises(LLMOutputInvalid):
        extract_json(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 类型收敛
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("hello", "hello"),
        (123, "123"),
        (1.5, "1.5"),
        (True, "True"),
        (None, ""),
        (["a", "b"], "a；b"),
    ],
)
def test_coerce_str(value, expected):
    assert coerce_str(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        (83, 83.0),
        (83.5, 83.5),
        ("83", 83.0),
        ("01:23", 83.0),
        ("1:01:01", 3661.0),
        ("", None),
        (None, None),
        ("abc", None),
        (True, None),
        (-5, 0.0),
    ],
)
def test_coerce_float(value, expected):
    result = coerce_float(value)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, []),
        ("", []),
        ("  ", []),
        ("one", ["one"]),
        (["a", "b"], ["a", "b"]),
        (["a", "", "  "], ["a"]),
        ([1, 2], ["1", "2"]),
        ({"k": "v"}, ["k: v"]),
    ],
)
def test_coerce_str_list(value, expected):
    assert coerce_str_list(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("video", "video"),
        ("MODEL", "model"),
        ("mixed", "mixed"),
        ("视频原话", "video"),
        ("模型补充", "model"),
        ("补充说明", "model"),
        ("whatever", "video"),
        (None, "video"),
    ],
)
def test_clamp_source(value, expected):
    assert clamp_source(value) == expected


def test_truncate_middle_short_text_unchanged():
    assert truncate_middle("abc", 100) == "abc"


def test_truncate_middle_keeps_head_and_tail():
    text = "A" * 100 + "B" * 100
    out = truncate_middle(text, 40)
    assert out.startswith("A")
    assert out.endswith("B")
    assert "省略" in out
    assert len(out) < len(text)


# ---------------------------------------------------------------------------
# JSON Schema 展平
# ---------------------------------------------------------------------------


def test_flatten_schema_removes_refs():
    schema = flatten_schema(Note)
    blob = json.dumps(schema)
    assert "$ref" not in blob
    assert "$defs" not in blob


def test_flatten_schema_is_object_rooted():
    schema = flatten_schema(Note)
    assert schema["type"] == "object"
    assert "properties" in schema
    for field in ("title", "one_liner", "summary", "key_points", "follow_ups"):
        assert field in schema["properties"]


def test_flatten_schema_marks_all_fields_required():
    """要求模型给出全部字段，避免它「偷懒」省略小节。"""
    schema = flatten_schema(Note)
    assert set(schema["required"]) == set(schema["properties"])


def test_flatten_schema_forbids_extra_properties():
    schema = flatten_schema(Note)
    assert schema.get("additionalProperties") is False


def test_flatten_schema_nested_objects_are_flattened():
    schema = flatten_schema(Note)
    key_points = schema["properties"]["key_points"]
    assert key_points["type"] == "array"
    item = key_points["items"]
    assert "$ref" not in json.dumps(item)
    assert "title" in item.get("properties", {})


def test_flatten_schema_drops_descriptions_to_save_tokens():
    schema = flatten_schema(Note)
    assert "description" not in schema


def test_flatten_schema_asks_for_string_timestamps():
    """⛔ 回归守卫。

    实测教训：要求模型把 [mm:ss] 换算成秒数时，它会给出 2/3/8/12 这种
    与原文完全对不上的小数字（20 分钟的视频，大纲时间点全落在前 30 秒）。
    「原样抄写标记」才是模型擅长的，所以 schema 里必须是 string。
    """
    schema = flatten_schema(Note)

    outline_ts = schema["properties"]["outline"]["items"]["properties"]["timestamp"]
    assert "string" in json.dumps(outline_ts), outline_ts
    assert "number" not in json.dumps(outline_ts), outline_ts


def test_flatten_schema_timestamp_override_keeps_nullable():
    """仍然允许 null（找不到对应时间点的情况）。"""
    schema = flatten_schema(Note)
    kp_ts = schema["properties"]["key_points"]["items"]["properties"]["timestamp"]
    assert "null" in json.dumps(kp_ts)


def test_flatten_schema_required_excludes_nullable_fields():
    """⛔ 回归守卫。

    曾经把全部字段都设为必填，结果模型对 example / timestamp 这类
    本可留空的字段无话可说，只好复读术语凑数 ——
    产出过「举个例子：UC纪元」这种废话。允许为空反而更诚实。
    """
    schema = flatten_schema(Note)
    required = set(schema["required"])

    # 可空字段不应被强制要求
    assert "example" not in required
    # 但关键小节必须仍然必填，否则模型会整节省略
    for field in ("title", "one_liner", "summary", "outline", "key_points"):
        assert field in required, field


def test_flatten_schema_nested_nullable_fields_not_required():
    schema = flatten_schema(Note)
    kp = schema["properties"]["key_points"]["items"]
    assert "timestamp" not in kp["required"]
    assert "title" in kp["required"]

    concept = schema["properties"]["concepts"]["items"]
    assert "example" not in concept["required"]
    assert "term" in concept["required"]


def test_flatten_schema_other_numeric_fields_untouched():
    """只覆盖 timestamp，别的字段类型不能被动到。"""
    schema = flatten_schema(Note)
    outline = schema["properties"]["outline"]["items"]["properties"]
    assert outline["heading"]["type"] == "string"


def test_flatten_schema_partial_notes():
    schema = flatten_schema(PartialNotes)
    assert schema["type"] == "object"
    assert "key_points" in schema["properties"]


# ---------------------------------------------------------------------------
# Ollama 适配器
# ---------------------------------------------------------------------------

OLLAMA = "http://127.0.0.1:11434"


@respx.mock
async def test_ollama_returns_message_content():
    respx.post(f"{OLLAMA}/api/chat").mock(
        return_value=httpx.Response(200, json={"message": {"content": '{"ok": true}'}})
    )
    p = OllamaProvider(base_url=OLLAMA, model="test-model")
    try:
        out = await p.complete_json(system="s", user="u")
        assert out == '{"ok": true}'
    finally:
        await p.aclose()


@respx.mock
async def test_ollama_sends_json_format_and_schema():
    route = respx.post(f"{OLLAMA}/api/chat").mock(
        return_value=httpx.Response(200, json={"message": {"content": "{}"}})
    )
    p = OllamaProvider(base_url=OLLAMA, model="m")
    try:
        await p.complete_json(system="s", user="u", schema={"type": "object"})
    finally:
        await p.aclose()

    body = json.loads(route.calls[0].request.content)
    assert body["format"] == {"type": "object"}
    assert body["stream"] is False
    assert body["model"] == "m"
    assert body["messages"][0]["role"] == "system"


@respx.mock
async def test_ollama_always_caps_generation_length():
    """⛔ 回归守卫：必须发 num_predict，不能让生成无上限。

    实测教训：不设上限时生成长度只受 num_ctx 约束，小模型一旦陷入重复
    就会一直吐到吃满上下文 —— 表现是「跑了十分钟然后超时」，
    既慢又拿不到任何产出。有了上限，最坏情况变成「很快返回被截断的内容」，
    而截断的 JSON 通常还能解析出部分结果。
    """
    route = respx.post(f"{OLLAMA}/api/chat").mock(
        return_value=httpx.Response(200, json={"message": {"content": "{}"}})
    )
    p = OllamaProvider(base_url=OLLAMA, model="m")
    try:
        await p.complete_json(system="s", user="u")
    finally:
        await p.aclose()

    options = json.loads(route.calls[0].request.content)["options"]
    assert "num_predict" in options, "没有发送 num_predict，生成长度不受限"
    assert options["num_predict"] > 0, "num_predict 必须是个正数上限"
    assert "num_ctx" in options


@respx.mock
async def test_ollama_num_predict_is_configurable():
    route = respx.post(f"{OLLAMA}/api/chat").mock(
        return_value=httpx.Response(200, json={"message": {"content": "{}"}})
    )
    p = OllamaProvider(base_url=OLLAMA, model="m", num_predict=1234)
    try:
        await p.complete_json(system="s", user="u")
    finally:
        await p.aclose()
    assert json.loads(route.calls[0].request.content)["options"]["num_predict"] == 1234


@respx.mock
async def test_ollama_falls_back_to_json_mode_without_schema():
    route = respx.post(f"{OLLAMA}/api/chat").mock(
        return_value=httpx.Response(200, json={"message": {"content": "{}"}})
    )
    p = OllamaProvider(base_url=OLLAMA, model="m")
    try:
        await p.complete_json(system="s", user="u")
    finally:
        await p.aclose()
    assert json.loads(route.calls[0].request.content)["format"] == "json"


@respx.mock
async def test_ollama_missing_model_mentions_pull_command():
    respx.post(f"{OLLAMA}/api/chat").mock(return_value=httpx.Response(404))
    p = OllamaProvider(base_url=OLLAMA, model="qwen3:8b")
    try:
        with pytest.raises(LLMError) as exc:
            await p.complete_json(system="s", user="u")
        assert "ollama pull" in str(exc.value)
    finally:
        await p.aclose()


@respx.mock
async def test_ollama_error_field_in_body():
    respx.post(f"{OLLAMA}/api/chat").mock(
        return_value=httpx.Response(200, json={"error": "model requires more memory"})
    )
    p = OllamaProvider(base_url=OLLAMA, model="m")
    try:
        with pytest.raises(LLMError) as exc:
            await p.complete_json(system="s", user="u")
        assert "memory" in str(exc.value)
    finally:
        await p.aclose()


@respx.mock
async def test_ollama_empty_content_raises():
    respx.post(f"{OLLAMA}/api/chat").mock(
        return_value=httpx.Response(200, json={"message": {"content": "   "}})
    )
    p = OllamaProvider(base_url=OLLAMA, model="m")
    try:
        with pytest.raises(LLMError):
            await p.complete_json(system="s", user="u")
    finally:
        await p.aclose()


@respx.mock
async def test_ollama_connection_error_is_actionable():
    respx.post(f"{OLLAMA}/api/chat").mock(side_effect=httpx.ConnectError("refused"))
    p = OllamaProvider(base_url=OLLAMA, model="m")
    try:
        with pytest.raises(LLMError) as exc:
            await p.complete_json(system="s", user="u")
        assert "Ollama" in str(exc.value)
    finally:
        await p.aclose()


@respx.mock
async def test_ollama_health_reports_missing_model():
    respx.get(f"{OLLAMA}/api/tags").mock(
        return_value=httpx.Response(200, json={"models": [{"name": "llama3:8b"}]})
    )
    p = OllamaProvider(base_url=OLLAMA, model="qwen3:8b")
    try:
        ok, msg = await p.health()
        assert ok is False
        assert "qwen3:8b" in msg
    finally:
        await p.aclose()


@respx.mock
async def test_ollama_health_ok_when_model_present():
    respx.get(f"{OLLAMA}/api/tags").mock(
        return_value=httpx.Response(200, json={"models": [{"name": "qwen3:8b"}]})
    )
    p = OllamaProvider(base_url=OLLAMA, model="qwen3:8b")
    try:
        ok, msg = await p.health()
        assert ok is True
    finally:
        await p.aclose()


@respx.mock
async def test_ollama_health_handles_unreachable():
    respx.get(f"{OLLAMA}/api/tags").mock(side_effect=httpx.ConnectError("refused"))
    p = OllamaProvider(base_url=OLLAMA, model="m")
    try:
        ok, msg = await p.health()
        assert ok is False
        assert "不可达" in msg
    finally:
        await p.aclose()


@respx.mock
async def test_ollama_reports_as_not_remote_on_loopback():
    p = OllamaProvider(base_url=OLLAMA, model="m")
    assert p.is_remote is False
    await p.aclose()
