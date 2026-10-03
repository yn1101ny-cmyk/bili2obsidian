"""自检与脚本测试。

doctor 里的联网项用 ``--offline`` 路径覆盖，**本文件不发起任何网络请求**。
"""

from __future__ import annotations

import json
from dataclasses import asdict

import pytest

from scripts.doctor import (
    FAIL,
    OK,
    WARN,
    Check,
    Report,
    check_asr_deps,
    check_cuda,
    check_dirs,
    check_disk,
    check_ffmpeg,
    check_llm_config,
    check_opencc,
    check_python,
    check_vault,
    check_ytdlp,
    render_text,
    run_checks,
)


# ---------------------------------------------------------------------------
# Check / Report
# ---------------------------------------------------------------------------


def test_check_mark_by_status():
    assert Check("n", OK, "d").mark == "✅"
    assert Check("n", FAIL, "d").mark == "❌"
    assert Check("n", WARN, "d").mark.startswith("⚠")


def test_report_worst_status():
    r = Report()
    assert r.worst == OK
    r.add(Check("a", OK, ""))
    assert r.worst == OK
    r.add(Check("b", WARN, ""))
    assert r.worst == WARN
    r.add(Check("c", FAIL, ""))
    assert r.worst == FAIL


def test_report_collects_failures_and_warnings():
    r = Report()
    r.add(Check("a", OK, ""))
    r.add(Check("b", WARN, ""))
    r.add(Check("c", FAIL, ""))
    assert [c.name for c in r.failed] == ["c"]
    assert [c.name for c in r.warnings] == ["b"]


def test_check_is_json_serialisable():
    """doctor --json 依赖这一点。"""
    assert json.dumps(asdict(Check("n", OK, "d", "h"))) is not None


# ---------------------------------------------------------------------------
# 单项检查
# ---------------------------------------------------------------------------


def test_check_python_passes_on_supported_version():
    c = check_python(None)  # type: ignore[arg-type]
    assert c.status == OK


def test_check_dirs_creates_and_is_writable(settings):
    c = check_dirs(settings)
    assert c.status == OK
    assert not (settings.data_dir / ".write_probe").exists()  # 探针要清理掉


def test_check_disk(settings):
    c = check_disk(settings)
    assert c.status in (OK, WARN, FAIL)


def test_check_asr_deps_reports_ctranslate2_version():
    c = check_asr_deps()
    assert c.status in (OK, WARN)
    assert "ctranslate2" in c.detail


def test_check_cuda_never_raises(settings):
    c = check_cuda(settings)
    assert c.status in (OK, WARN)
    assert c.detail


def test_check_opencc():
    c = check_opencc()
    assert c.status in (OK, WARN)


def test_check_ytdlp_installed():
    c = check_ytdlp()
    assert c.status == OK
    assert "yt-dlp" in c.detail


def test_check_ffmpeg_never_fails_the_run():
    """ffmpeg 在主路径里用不到，缺了最多是警告。"""
    c = check_ffmpeg()
    assert c.status in (OK, WARN)


def test_check_vault(settings):
    c = check_vault(settings)
    assert c.status == OK


# ---------------------------------------------------------------------------
# 模型配置检查 —— 数据安全相关
# ---------------------------------------------------------------------------


def test_local_ollama_reports_data_stays_local(settings):
    settings.llm_provider = "ollama"
    settings.ollama_base_url = "http://127.0.0.1:11434"
    c = check_llm_config(settings)
    assert c.status == OK
    assert "不出本机" in c.detail


def test_remote_ollama_is_flagged_as_warning(settings):
    """把 Ollama 指到别的机器就等于数据外发，必须警告。"""
    settings.llm_provider = "ollama"
    settings.ollama_base_url = "http://192.168.1.50:11434"
    c = check_llm_config(settings)
    assert c.status == WARN
    assert "离开本机" in c.hint


def test_cloud_without_consent_is_flagged(settings):
    settings.llm_provider = "openai"
    settings.allow_cloud = False
    c = check_llm_config(settings)
    assert c.status == WARN
    assert "ALLOW_CLOUD" in c.hint


def test_cloud_with_consent_warns_about_egress(settings):
    settings.llm_provider = "openai"
    settings.allow_cloud = True
    settings.openai_api_key = "sk-not-real-but-long-enough-value"
    c = check_llm_config(settings)
    assert c.status == WARN
    assert "第三方" in c.hint


def test_llm_disabled_is_flagged(settings):
    settings.llm_provider = "none"
    c = check_llm_config(settings)
    assert c.status == WARN


# ---------------------------------------------------------------------------
# 离线整体运行
# ---------------------------------------------------------------------------


async def test_offline_report_contains_core_checks(settings):
    report = await run_checks(settings, offline=True)
    names = [c.name for c in report.checks]
    for expected in ("Python 版本", "数据目录", "磁盘空间", "语音转写依赖", "GPU 加速"):
        assert expected in names


async def test_offline_report_has_no_failures(settings):
    """离线模式下不该出现阻塞项（依赖都装好了）。"""
    report = await run_checks(settings, offline=True)
    assert not report.failed, [c.name for c in report.failed]


async def test_offline_report_notes_skipped_network(settings):
    report = await run_checks(settings, offline=True)
    skipped = [c for c in report.checks if "跳过" in c.detail]
    assert skipped


async def test_offline_report_does_not_touch_the_network(settings, monkeypatch):
    """守住「--offline 真的不联网」这件事。"""
    import scripts.doctor as doctor

    async def boom(*args, **kwargs):
        raise AssertionError("--offline 模式下不应发起网络请求")

    monkeypatch.setattr(doctor, "check_bilibili", boom)
    monkeypatch.setattr(doctor, "check_ollama", boom)
    monkeypatch.setattr(doctor, "check_bili_login", boom)

    report = await run_checks(settings, offline=True)
    assert report is not None


# ---------------------------------------------------------------------------
# 文本渲染
# ---------------------------------------------------------------------------


def test_render_text_lists_every_check(settings):
    r = Report()
    r.add(Check("甲", OK, "正常"))
    r.add(Check("乙", WARN, "注意", "这样做"))
    text = render_text(settings, r)
    assert "甲" in text
    assert "乙" in text
    assert "这样做" in text


def test_render_text_redacts_secrets(settings):
    settings.bili_sessdata = "SUPER-SECRET-VALUE"
    settings.openai_api_key = "sk-another-secret-value-here"
    r = Report()
    r.add(Check("x", OK, "y"))

    text = render_text(settings, r)
    assert "SUPER-SECRET-VALUE" not in text
    assert "sk-another-secret-value-here" not in text
    assert "***已设置***" in text


@pytest.mark.parametrize(
    "status,expect",
    [(OK, "全部就绪"), (WARN, "基本可用"), (FAIL, "存在阻塞项")],
)
def test_render_text_summary_by_status(settings, status, expect):
    r = Report()
    r.add(Check("x", status, "d", "hint"))
    assert expect in render_text(settings, r)


def test_render_text_lists_blocking_hints(settings):
    r = Report()
    r.add(Check("坏掉的东西", FAIL, "坏了", "这样修"))
    text = render_text(settings, r)
    assert "坏掉的东西" in text
    assert "这样修" in text


# ---------------------------------------------------------------------------
# 控制台编码
# ---------------------------------------------------------------------------


def test_enable_utf8_output_is_idempotent():
    from app.console import enable_utf8_output

    enable_utf8_output()
    enable_utf8_output()


def test_console_module_imports():
    from app.console import enable_utf8_output

    assert callable(enable_utf8_output)
