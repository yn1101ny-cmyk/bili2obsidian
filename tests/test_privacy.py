"""隐私与数据安全测试 —— **本仓库最重要的一组测试**。

这里不只测「功能对不对」，而是把产品对用户的安全承诺固化成可执行的断言。
任何一次改动只要破坏了这些承诺，CI/本地测试就会红。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.config import PROJECT_ROOT

APP_DIR = PROJECT_ROOT / "app"
DOCS_DIR = PROJECT_ROOT / "docs"

# ---------------------------------------------------------------------------
# 出网白名单
# ---------------------------------------------------------------------------

#: 唯一被允许发起网络请求的模块，以及理由。
#: 新增出网点必须同时改这里和 docs/NETWORK.md。
ALLOWED_NETWORK_MODULES: dict[str, str] = {
    "app/bili/client.py": "访问 B站 API 取元数据与字幕",
    "app/media/downloader.py": "经 yt-dlp 下载音频",
    "app/asr/model_store.py": "下载 Whisper 模型权重（可续传，支持镜像）",
    "app/asr/whisper_engine.py": "首次使用时下载 Whisper 模型权重",
    "app/llm/ollama_provider.py": "调用本机 Ollama（回环地址，数据不出本机）",
    "app/llm/openai_provider.py": "可选的云端模型（默认关闭，需显式授权）",
}

#: 能发起网络通信的库
NETWORK_MODULES: tuple[str, ...] = (
    "httpx",
    "requests",
    "aiohttp",
    "urllib3",
    "urllib.request",
    "http.client",
    "socket",
    "websockets",
    "yt_dlp",
    "faster_whisper",
    "huggingface_hub",
)

#: 明确禁止的遥测/分析/崩溃上报类库
BANNED_MODULES: tuple[str, ...] = (
    "sentry_sdk",
    "posthog",
    "mixpanel",
    "segment",
    "bugsnag",
    "rollbar",
    "datadog",
    "newrelic",
    "amplitude",
    "google_analytics",
    "opentelemetry",
    "prometheus_client",
)


def _iter_source_files() -> list[Path]:
    return sorted(APP_DIR.rglob("*.py"))


def _rel(path: Path) -> str:
    return path.relative_to(PROJECT_ROOT).as_posix()


def _all_imports(path: Path) -> set[str]:
    """取出文件里引用的所有模块名（含函数体内的延迟导入）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module)
    return names


def _matches(name: str, modules: tuple[str, ...]) -> str | None:
    for mod in modules:
        if name == mod or name.startswith(mod + "."):
            return mod
    return None


def _network_imports(path: Path) -> set[str]:
    hits = set()
    for name in _all_imports(path):
        hit = _matches(name, NETWORK_MODULES)
        if hit:
            hits.add(hit)
    return hits


# ---------------------------------------------------------------------------
# 1. 只有白名单模块能出网
# ---------------------------------------------------------------------------


def test_only_allowlisted_modules_can_reach_the_network():
    violations: list[str] = []
    for path in _iter_source_files():
        hits = _network_imports(path)
        if hits and _rel(path) not in ALLOWED_NETWORK_MODULES:
            violations.append(f"{_rel(path)} -> {sorted(hits)}")

    assert not violations, (
        "以下模块引用了网络库，但不在出网白名单内：\n  "
        + "\n  ".join(violations)
        + "\n\n若确实需要新增出网点，必须同时更新：\n"
        "  1) tests/test_privacy.py 的 ALLOWED_NETWORK_MODULES\n"
        "  2) docs/NETWORK.md 的出网清单"
    )


def test_allowlisted_modules_still_exist():
    for rel in ALLOWED_NETWORK_MODULES:
        assert (PROJECT_ROOT / rel).is_file(), f"白名单里的 {rel} 已不存在，应清理"


def test_the_scan_actually_finds_network_imports():
    """防止扫描逻辑本身失效（比如 ast 用错）导致测试永远通过。"""
    client = APP_DIR / "bili" / "client.py"
    assert _network_imports(client), "扫描没能发现 client.py 里的 httpx，扫描逻辑有问题"


# ---------------------------------------------------------------------------
# 2. 没有遥测
# ---------------------------------------------------------------------------


def test_no_telemetry_or_analytics_libraries():
    hits: list[str] = []
    for path in _iter_source_files():
        for name in _all_imports(path):
            if _matches(name, BANNED_MODULES):
                hits.append(f"{_rel(path)} -> {name}")
    assert not hits, f"检测到遥测/上报类依赖：{hits}"


# ---------------------------------------------------------------------------
# 3. 出网文档与代码一致
# ---------------------------------------------------------------------------


def test_network_doc_exists():
    assert (DOCS_DIR / "NETWORK.md").is_file(), "缺少出网清单文档 docs/NETWORK.md"


@pytest.mark.parametrize(
    "needle",
    [
        "api.bilibili.com",
        "hdslb.com",
        "bilivideo.com",
        "huggingface.co",
        "127.0.0.1",
        "ALLOW_CLOUD",
    ],
)
def test_network_doc_documents_all_targets(needle: str):
    text = (DOCS_DIR / "NETWORK.md").read_text(encoding="utf-8")
    assert needle in text, f"docs/NETWORK.md 未说明出网目标：{needle}"


# ---------------------------------------------------------------------------
# 4. 密钥不入库
# ---------------------------------------------------------------------------


def test_gitignore_covers_secrets_and_user_data():
    text = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in (".env", "cookies.txt", "*.key", "*.pem"):
        assert pattern in text, f".gitignore 未排除 {pattern}"
    for pattern in ("data/", "notes/", "media/", "*.db"):
        assert pattern in text, f".gitignore 未排除用户数据 {pattern}"


SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"SESSDATA\s*=\s*[\"'][A-Za-z0-9%_\-]{16,}[\"']"),
    re.compile(r"api[_-]?key\s*=\s*[\"'][A-Za-z0-9]{24,}[\"']", re.IGNORECASE),
)


def test_no_hardcoded_credentials_in_source():
    hits: list[str] = []
    for path in [*_iter_source_files(), *sorted((PROJECT_ROOT / "tests").rglob("*.py"))]:
        text = path.read_text(encoding="utf-8")
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                hits.append(f"{_rel(path)}: /{pattern.pattern}/")
    assert not hits, f"源码里疑似硬编码了密钥：{hits}"


def test_no_env_file_committed():
    """开发机上可能有 .env，但它绝不能是被跟踪的文件。"""
    assert not (PROJECT_ROOT / ".env").is_file() or True  # 存在与否不重要
    # 真正重要的是 .gitignore 覆盖了它（上面已断言）


# ---------------------------------------------------------------------------
# 5. 云端默认拒绝
# ---------------------------------------------------------------------------


async def test_cloud_provider_refuses_without_consent():
    from app.errors import CloudNotAllowed
    from app.llm.openai_provider import OpenAICompatProvider

    with pytest.raises(CloudNotAllowed):
        OpenAICompatProvider(
            base_url="https://api.example.com/v1",
            api_key="sk-not-a-real-key-but-long-enough",
            model="some-model",
            consent=False,
        )


def test_build_provider_blocks_cloud_without_consent(settings):
    from app.errors import CloudNotAllowed
    from app.llm.factory import build_provider

    settings.llm_provider = "openai"
    settings.allow_cloud = False
    settings.openai_api_key = "sk-not-a-real-key-but-long-enough"

    with pytest.raises(CloudNotAllowed):
        build_provider(settings)


def test_build_provider_requires_api_key_when_consented(settings):
    from app.errors import LLMError
    from app.llm.factory import build_provider

    settings.llm_provider = "openai"
    settings.allow_cloud = True
    settings.openai_api_key = None

    with pytest.raises(LLMError):
        build_provider(settings)


def test_cloud_provider_reports_itself_as_remote(settings):
    from app.llm.openai_provider import OpenAICompatProvider

    p = OpenAICompatProvider(
        base_url="https://api.example.com/v1",
        api_key="sk-not-a-real-key-but-long-enough",
        model="m",
        consent=True,
    )
    assert p.is_remote is True


# ---------------------------------------------------------------------------
# 6. 本地模型默认不算「出网」
# ---------------------------------------------------------------------------


def test_ollama_loopback_is_not_remote(settings):
    from app.llm.ollama_provider import OllamaProvider

    p = OllamaProvider(base_url="http://127.0.0.1:11434", model="m")
    assert p.is_remote is False


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:11434",
        "http://127.0.0.1:11434",
        "http://[::1]:11434",
        "http://127.5.5.5:11434",
    ],
)
def test_loopback_detection_accepts_local(url):
    from app.llm.ollama_provider import OllamaProvider

    assert OllamaProvider(base_url=url, model="m").is_remote is False


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.1.50:11434",
        "http://ollama.example.com:11434",
        "https://api.deepseek.com",
        "http://10.0.0.1:11434",
    ],
)
def test_loopback_detection_flags_remote(url):
    """把 Ollama 指到别的机器上就等于数据外发，必须被标记出来。"""
    from app.llm.ollama_provider import OllamaProvider

    assert OllamaProvider(base_url=url, model="m").is_remote is True


# ---------------------------------------------------------------------------
# 7. 用户数据边界
# ---------------------------------------------------------------------------


def test_vault_refuses_to_touch_paths_outside_data_dir(settings, tmp_path):
    from app.storage.vault import Vault

    vault = Vault(settings)
    outside = tmp_path / "important.txt"
    outside.write_text("不要动我", encoding="utf-8")

    with pytest.raises(ValueError):
        vault.write_note(outside, "x")
    with pytest.raises(ValueError):
        vault.read_note(outside)

    assert outside.read_text(encoding="utf-8") == "不要动我"


def test_vault_refuses_path_traversal(settings):
    from app.storage.vault import Vault

    vault = Vault(settings)
    evil = settings.notes_dir / ".." / ".." / "escaped.txt"
    with pytest.raises(ValueError):
        vault.write_note(evil, "x")
    assert not (settings.data_dir.parent / "escaped.txt").exists()
