"""可选云端 LLM 适配器（OpenAI 兼容接口，如 DeepSeek / 通义 / OpenAI）。

⚠️ **隐私边界**：使用本适配器意味着**视频字幕全文会离开你的电脑**，
发送给第三方服务商。因此这里做了三重默认拒绝：

1. 配置层：必须 ``LLM_PROVIDER=openai`` **且** ``ALLOW_CLOUD=true`` 才会被构造；
2. 构造层：不给 ``consent=True`` 直接抛 :class:`CloudNotAllowed`；
3. 展示层：UI 与笔记 frontmatter 会标注「本笔记由云端模型生成」。

不做沉默的数据外发。
"""

from __future__ import annotations

from typing import Any

import httpx

from app.errors import CloudNotAllowed, LLMError
from app.llm.base import LLMProvider


class OpenAICompatProvider(LLMProvider):
    """OpenAI 兼容的 ``/chat/completions`` 客户端。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        model: str,
        timeout: float = 300.0,
        consent: bool = False,
        max_tokens: int | None = None,
    ) -> None:
        if not consent:
            raise CloudNotAllowed()
        if not api_key or not api_key.strip():
            raise LLMError(
                "选择了云端模型但没有配置 API Key。"
                "请在 .env 中设置 OPENAI_API_KEY。"
            )
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip()
        self.model = model
        self.timeout = timeout
        #: 单次输出上限。None 表示不限制（交给服务端默认值）。
        self.max_tokens = max_tokens
        self.name = f"云端/{model}"
        self.is_remote = True
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout, connect=15.0),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    # ------------------------------------------------------------------
    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any] | None = None,
        temperature: float = 0.2,
    ) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            # 大多数兼容服务都支持 json_object；结构由提示词 + pydantic 校验兜底
            "response_format": {"type": "json_object"},
        }
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens

        client = self._get_client()
        try:
            resp = await client.post("/chat/completions", json=payload)
        except httpx.ConnectError as exc:
            raise LLMError(f"连不上云端接口 {self.base_url}：{exc}") from exc
        except httpx.TimeoutException as exc:
            raise LLMError(f"云端接口超时（{self.timeout}s）：{exc}") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"请求云端接口失败：{exc}") from exc

        if resp.status_code == 401:
            raise LLMError("云端接口返回 401：API Key 无效或已过期。")
        if resp.status_code == 429:
            raise LLMError("云端接口返回 429：触发速率限制，请稍后重试或降低频率。")
        if resp.status_code >= 400:
            raise LLMError(f"云端接口返回 HTTP {resp.status_code}：{resp.text[:300]}")

        try:
            data = resp.json()
        except ValueError as exc:
            raise LLMError(f"云端接口返回的不是 JSON：{resp.text[:300]}") from exc

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"云端返回结构异常：{str(data)[:300]}") from exc

        if not isinstance(content, str) or not content.strip():
            raise LLMError("云端返回了空内容")
        return content

    # ------------------------------------------------------------------
    async def health(self) -> tuple[bool, str]:
        """只检查鉴权与连通性，不发送任何视频内容。"""
        client = self._get_client()
        try:
            resp = await client.get("/models", timeout=httpx.Timeout(10.0))
        except httpx.HTTPError as exc:
            return False, f"云端接口不可达：{exc}"
        if resp.status_code == 401:
            return False, "云端 API Key 无效（401）"
        if resp.status_code >= 400:
            return False, f"云端接口返回 HTTP {resp.status_code}"
        return True, f"云端接口连通，模型 {self.model}（⚠️ 字幕将发送到第三方）"
