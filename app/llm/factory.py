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
            num_ctx=settings.llm_num_ctx,
            num_predict=settings.llm_num_predict,
        )

    if settings.llm_provider == "none":
        raise ConfigError(
            "LLM_PROVIDER=none：已关闭笔记生成。"
            "只会产出转写文本。如需生成笔记请设为 ollama。"
        )

    raise ConfigError(f"未知的 LLM_PROVIDER：{settings.llm_provider!r}")


def build_polish_provider(settings: Settings) -> OpenAICompatProvider | None:
    """构造「本地出稿 + 云端润色」用的云端提供方。

    与 :func:`build_provider` 分开，是因为润色**不是**一次完整生成：
    它只拿本地已经成稿的笔记结构去改一遍，不重新读转写。
    返回 ``None`` 表示当前配置不满足润色条件（开关/授权/密钥），
    调用方应保留本地稿。

    ⚠️ 隐私边界：调用它意味着**笔记正文**会发送给第三方 ——
    所以同样要求 ``ALLOW_CLOUD=true`` 显式授权。
    """
    if not settings.polish_ready:
        return None
    return OpenAICompatProvider(
        base_url=settings.polish_base_url or settings.openai_base_url,
        api_key=settings.polish_api_key or settings.openai_api_key,
        model=settings.polish_model or settings.openai_model,
        timeout=settings.polish_timeout,
        consent=settings.allow_cloud,
        max_tokens=settings.polish_max_tokens,
    )
