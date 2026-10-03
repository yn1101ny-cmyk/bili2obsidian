"""配置层测试 —— 重点是**数据安全的硬约束**。

这些用例的意义不只是「代码对不对」，而是把安全承诺固化下来：
以后任何人改动配置层，只要破坏了下述任一条，测试都会红。
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.config import PROJECT_ROOT, Settings


def make(**kwargs) -> Settings:
    """构造配置，屏蔽磁盘上的 .env 以保证可重复。"""
    return Settings(_env_file=None, **kwargs)


# ---------------------------------------------------------------------------
# 服务暴露面：只允许回环
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_hosts_allowed(host):
    assert make(host=host).host == host


@pytest.mark.parametrize(
    "host",
    [
        "0.0.0.0",          # 所有网卡 —— 会把笔记暴露给局域网
        "192.168.1.10",
        "10.0.0.5",
        "example.com",
        "::",               # IPv6 通配
    ],
)
def test_non_loopback_hosts_rejected(host):
    with pytest.raises(ValidationError) as exc:
        make(host=host)
    assert "回环" in str(exc.value) or "host" in str(exc.value)


def test_default_host_is_loopback():
    assert make().host == "127.0.0.1"


# ---------------------------------------------------------------------------
# 云端默认关闭
# ---------------------------------------------------------------------------


def test_cloud_requires_both_switch_and_consent():
    """选了云端但没授权 -> 不算启用。"""
    assert make(llm_provider="openai", allow_cloud=False).cloud_enabled is False


def test_cloud_requires_provider_choice():
    """授权了但用的是本地模型 -> 也不算启用。"""
    assert make(llm_provider="ollama", allow_cloud=True).cloud_enabled is False


def test_cloud_enabled_only_when_both():
    s = make(
        llm_provider="openai",
        allow_cloud=True,
        openai_api_key="sk-test",
    )
    assert s.cloud_enabled is True


def test_allow_cloud_defaults_to_false():
    assert make().allow_cloud is False


# ---------------------------------------------------------------------------
# 密钥不外泄
# ---------------------------------------------------------------------------


def test_redacted_hides_sessdata_and_api_key():
    secret = "SUPER-SECRET-SESSDATA-VALUE"
    key = "sk-SUPER-SECRET-KEY"
    s = make(bili_sessdata=secret, openai_api_key=key)

    blob = json.dumps(s.redacted(), ensure_ascii=False, default=str)
    assert secret not in blob
    assert key not in blob
    assert "SUPER-SECRET" not in blob


def test_redacted_marks_presence_without_value():
    s = make(bili_sessdata="abc", openai_api_key="sk-x")
    r = s.redacted()
    assert r["bili_sessdata"] == "***已设置***"
    assert r["openai_api_key"] == "***已设置***"


def test_redacted_reports_absent_secrets():
    r = make().redacted()
    assert r["bili_sessdata"] is None
    assert r["openai_api_key"] is None


def test_repr_does_not_leak_secrets():
    """pydantic 的 repr 会带字段值，确认我们没有在任何地方打印它。"""
    s = make(bili_sessdata="LEAKME")
    # 这条测试是在提醒：不要 log settings 本身，要 log settings.redacted()
    assert "LEAKME" in repr(s)  # 事实如此，所以下面的约定必须遵守
    assert "LEAKME" not in json.dumps(s.redacted(), default=str)


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------


def test_derived_paths(settings: Settings):
    assert settings.notes_dir == settings.data_dir / "notes"
    assert settings.media_dir == settings.data_dir / "media"
    assert settings.cache_dir == settings.data_dir / "cache"
    assert settings.models_dir == settings.data_dir / "models"
    assert settings.log_dir == settings.data_dir / "logs"
    assert settings.db_path == settings.data_dir / "mynote.db"


def test_relative_data_dir_is_anchored_to_project():
    s = make(data_dir="some/sub")
    assert s.data_dir.is_absolute()
    assert s.data_dir == (PROJECT_ROOT / "some" / "sub").resolve()


def test_absolute_data_dir_kept(tmp_path):
    target = tmp_path / "custom"
    assert make(data_dir=target).data_dir == target.resolve()


def test_ensure_dirs_is_idempotent(tmp_path):
    s = make(data_dir=tmp_path / "d")
    s.ensure_dirs()
    s.ensure_dirs()  # 再来一次不应报错
    for d in (
        s.data_dir,
        s.notes_dir,
        s.media_dir,
        s.cache_dir,
        s.models_dir,
        s.log_dir,
    ):
        assert d.is_dir()


# ---------------------------------------------------------------------------
# 登录态判断
# ---------------------------------------------------------------------------


def test_has_bili_login_false_when_unset():
    assert make().has_bili_login is False


def test_has_bili_login_false_when_blank():
    assert make(bili_sessdata="   ").has_bili_login is False
    assert make(bili_sessdata="").has_bili_login is False


def test_has_bili_login_true_when_set():
    assert make(bili_sessdata="abc123").has_bili_login is True


# ---------------------------------------------------------------------------
# 面向 Blackwell 的安全默认值
# ---------------------------------------------------------------------------


def test_whisper_compute_type_is_never_auto():
    """⛔ 这是硬约束。

    ``auto`` 会让 CTranslate2 选 int8，而 Blackwell(sm_120) 上 int8 路径
    会抛 CUBLAS_STATUS_NOT_SUPPORTED。默认值必须是显式的 float16。
    """
    assert make().whisper_compute_type != "auto"
    assert make().whisper_compute_type == "float16"


def test_whisper_language_defaults_to_chinese():
    assert make().whisper_language == "zh"


def test_initial_prompt_is_a_complete_sentence():
    """实测结论：单词/短语形式的 initial_prompt 完全无效，必须是完整句子。"""
    prompt = make().whisper_initial_prompt
    assert len(prompt) >= 8
    assert prompt.endswith(("。", "！", "？", "."))


def test_whisper_defaults_favour_simplified_chinese():
    s = make()
    assert s.whisper_simplify_chinese is True


# ---------------------------------------------------------------------------
# 校验边界
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("port", [0, 70000, -1])
def test_invalid_port_rejected(port):
    with pytest.raises(ValidationError):
        make(port=port)


def test_invalid_llm_provider_rejected():
    with pytest.raises(ValidationError):
        make(llm_provider="chatgpt")
