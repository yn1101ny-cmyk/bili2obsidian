"""按配置构造 LLM 提供方。"""

from __future__ import annotations

from app.config import Settings
from app.errors import ConfigError
from app.llm.base import LLMProvider
from app.llm.ollama_provider import OllamaProvider
from app.llm.openai_provider import OpenAICompatProvider


def build_provider(settings: Settings) -> LLMProvider:
    """根据配置返回 LLM 提供方。

    * ``ollama``：默认，本地推理，数据不出本机；
    * ``openai``：需 ``ALLOW_CLOUD=true`` 显式授权，否则抛 :class:`CloudNotAllowed`；
    * ``none``：不生成笔记（仍可拿到转写），用于纯转写/调试场景。
    """
    if settings.llm_provider == "openai":
        # consent 传入前已由 settings.cloud_enabled 把关
        return OpenAICompatProvider(
            base_url=settings.openai_base_url,
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            consent=settings.cloud_enabled,
        )

    if settings.llm_provider == "ollama":
        return OllamaProvider(
            base_url=settings.ollama_base_url,
            model=settings.ollama_model,
            timeout=settings.ollama_timeout,
        )

    if settings.llm_provider == "none":
        raise ConfigError(
            "LLM_PROVIDER=none：已关闭笔记生成。"
            "只会产出转写文本。如需生成笔记请设为 ollama。"
        )

    raise ConfigError(f"未知的 LLM_PROVIDER：{settings.llm_provider!r}")
