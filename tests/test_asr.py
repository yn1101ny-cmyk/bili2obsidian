"""转写引擎测试。

**不加载模型、不联网** —— 只验证设备/精度决策与下载通道配置。
真正的转写由 ``scripts/smoke_transcribe.py`` 用真实音频验证。
"""

from __future__ import annotations

import os

import pytest

from app.asr.whisper_engine import (
    _DLL_DIR_HANDLES,
    apply_hf_env,
    build_hotwords,
    detect_device,
    download_hint,
    resolve_compute_type,
    setup_cuda_dll_paths,
    supported_compute_types,
    to_simplified,
)


# ---------------------------------------------------------------------------
# compute_type 决策 —— Blackwell 上的生死线
# ---------------------------------------------------------------------------


def test_gpu_never_uses_auto():
    """⛔ 硬约束：auto 会选 int8，在 sm_120 上抛 CUBLAS_STATUS_NOT_SUPPORTED。"""
    assert resolve_compute_type("auto", "cuda") == "float16"


def test_gpu_auto_is_float16_regardless_of_availability():
    """即使探测不到可用精度，也不能退回 auto。"""
    assert resolve_compute_type("auto", "cuda") != "auto"


def test_cpu_auto_is_int8():
    assert resolve_compute_type("auto", "cpu") == "int8"


def test_cpu_float16_falls_back_to_int8():
    """CPU 不支持 float16。"""
    assert resolve_compute_type("float16", "cpu") == "int8"


def test_cpu_keeps_explicit_int8():
    assert resolve_compute_type("int8", "cpu") == "int8"


def test_gpu_keeps_explicit_float16():
    assert resolve_compute_type("float16", "cuda") == "float16"


def test_gpu_int8_falls_back_when_unsupported():
    """如果这台机器的 CUDA 不支持 int8，应自动改用 float16 而不是硬上。"""
    available = supported_compute_types("cuda")
    result = resolve_compute_type("int8", "cuda")
    if available and "int8" not in available and "int8_float16" not in available:
        assert result == "float16"
    else:
        assert result in ("int8", "float16")


# ---------------------------------------------------------------------------
# 设备决策
# ---------------------------------------------------------------------------


def test_explicit_cpu_never_probes_gpu():
    assert detect_device("cpu") == ("cpu", "int8")


def test_supported_compute_types_never_raises():
    assert isinstance(supported_compute_types("cuda"), set)
    assert isinstance(supported_compute_types("cpu"), set)


def test_supported_compute_types_cpu_has_int8():
    assert "int8" in supported_compute_types("cpu")


# ---------------------------------------------------------------------------
# 下载通道（HF 镜像）
# ---------------------------------------------------------------------------


def test_apply_hf_env_sets_endpoint(settings, monkeypatch):
    monkeypatch.delenv("HF_ENDPOINT", raising=False)
    settings.hf_endpoint = "https://hf-mirror.com"
    assert apply_hf_env(settings) == "https://hf-mirror.com"
    assert os.environ["HF_ENDPOINT"] == "https://hf-mirror.com"


def test_apply_hf_env_defaults_to_official(settings, monkeypatch):
    monkeypatch.delenv("HF_ENDPOINT", raising=False)
    settings.hf_endpoint = None
    assert apply_hf_env(settings) == "https://huggingface.co"


def test_apply_hf_env_disables_xet_by_default(settings, monkeypatch):
    """实测：开着 Xet 国内下载卡在 0 字节。"""
    monkeypatch.delenv("HF_HUB_DISABLE_XET", raising=False)
    settings.hf_disable_xet = True
    apply_hf_env(settings)
    assert os.environ["HF_HUB_DISABLE_XET"] == "1"


def test_apply_hf_env_respects_existing_xet_setting(settings, monkeypatch):
    """用户显式在外部设了值就不要覆盖。"""
    monkeypatch.setenv("HF_HUB_DISABLE_XET", "0")
    settings.hf_disable_xet = True
    apply_hf_env(settings)
    assert os.environ["HF_HUB_DISABLE_XET"] == "0"


def test_apply_hf_env_can_leave_xet_alone(settings, monkeypatch):
    monkeypatch.delenv("HF_HUB_DISABLE_XET", raising=False)
    settings.hf_disable_xet = False
    apply_hf_env(settings)
    assert "HF_HUB_DISABLE_XET" not in os.environ


def test_apply_hf_env_silences_noisy_warnings(settings, monkeypatch):
    monkeypatch.delenv("HF_HUB_DISABLE_SYMLINKS_WARNING", raising=False)
    apply_hf_env(settings)
    assert os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] == "1"


# ---------------------------------------------------------------------------
# 下载失败提示
# ---------------------------------------------------------------------------


def test_download_hint_mentions_mirror_on_official_endpoint(monkeypatch):
    monkeypatch.setenv("HF_ENDPOINT", "https://huggingface.co")
    hint = download_hint()
    assert "hf-mirror.com" in hint
    assert "HF_ENDPOINT" in hint


def test_download_hint_omits_mirror_advice_when_already_using_one(monkeypatch):
    monkeypatch.setenv("HF_ENDPOINT", "https://hf-mirror.com")
    hint = download_hint()
    assert "hf-mirror.com" in hint
    # 已经在用镜像了，不该再叫他去设镜像
    assert "请在 .env 中设置" not in hint


def test_download_hint_when_no_endpoint_set(monkeypatch):
    monkeypatch.delenv("HF_ENDPOINT", raising=False)
    assert "hf-mirror.com" in download_hint()


# ---------------------------------------------------------------------------
# 繁转简
# ---------------------------------------------------------------------------


def test_to_simplified_converts_traditional():
    """装了 OpenCC 就应该真的把繁体转过来。"""
    result = to_simplified("這個東西很難")
    assert result == "这个东西很难"


def test_to_simplified_leaves_blank_alone():
    assert to_simplified("") == ""


def test_to_simplified_keeps_simplified_unchanged():
    assert to_simplified("这个已经是简体") == "这个已经是简体"


def test_to_simplified_keeps_latin():
    assert to_simplified("hello world 123") == "hello world 123"


# ---------------------------------------------------------------------------
# 热词（专有名词纠错）
# ---------------------------------------------------------------------------


def test_build_hotwords_joins_title_and_tags():
    text = build_hotwords(title="刹帝利 拼装模型", tags=["高达", "MGSD"])
    assert "刹帝利" in text
    assert "高达" in text
    assert "MGSD" in text


def test_build_hotwords_includes_owner_and_part():
    text = build_hotwords(title="标题", part_title="第一讲", owner="某UP")
    assert "标题" in text
    assert "第一讲" in text
    assert "某UP" in text


def test_build_hotwords_deduplicates():
    text = build_hotwords(title="高达", part_title="高达", tags=["高达"])
    assert text == "高达"


def test_build_hotwords_skips_blank_entries():
    text = build_hotwords(title="真标题", part_title="", owner="   ", tags=["", "  "])
    assert text == "真标题"


def test_build_hotwords_respects_limit():
    text = build_hotwords(title="字" * 500, limit=160)
    assert len(text) == 160


def test_build_hotwords_empty_when_nothing_given():
    assert build_hotwords() == ""


def test_build_hotwords_handles_none_tags():
    assert build_hotwords(title="标题", tags=None) == "标题"  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# CUDA DLL 句柄存活（很容易再犯的回归）
# ---------------------------------------------------------------------------


def test_cuda_dll_handles_are_retained():
    """⛔ 回归守卫。

    ``os.add_dll_directory`` 返回的句柄一旦被垃圾回收，
    该目录就会立刻从 DLL 搜索路径移除 —— 等同于什么都没做。
    所以必须把句柄存进模块级列表。
    """
    added = setup_cuda_dll_paths()
    if not added:
        pytest.skip("本机没有额外的 CUDA DLL 目录（非 Windows 或未装 nvidia-* 包）")
    assert len(_DLL_DIR_HANDLES) >= len(added), (
        "add_dll_directory 的返回值没被持有，CUDA 库会加载失败"
    )


def test_setup_cuda_dll_paths_is_idempotent():
    first = setup_cuda_dll_paths()
    second = setup_cuda_dll_paths()
    assert second == first or len(second) <= len(first)
