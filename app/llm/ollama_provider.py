"""Ollama 本地模型适配器。

只访问 ``OLLAMA_BASE_URL``（默认 ``http://127.0.0.1:11434``）—— **数据不出本机**。
若该地址被改成远程主机，``is_remote`` 会变 True，UI 上会给出明确警告。
"""

from __future__ import annotations

import ipaddress
from typing import Any
from urllib.parse import urlparse

import httpx

from app.errors import LLMError
from app.llm.base import LLMProvider


def _is_loopback(url: str) -> bool:
    """判断 base_url 是否指向本机。"""
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return False
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class OllamaProvider(LLMProvider):
    """通过 Ollama HTTP API 调用本地模型。"""

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:11434",
        model: str = "qwen3:8b",
        timeout: float = 600.0,
        num_ctx: int = 8192,
        num_predict: int = 4096,
        think: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.num_ctx = num_ctx
        self.num_predict = num_predict
        self.think = think
        self.name = f"Ollama/{model}"
        self.is_remote = not _is_loopback(self.base_url)
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout, connect=10.0),
                # Ollama 在本机（或内网），不该走系统代理。
                #
                # 坑：httpx 默认 trust_env=True，会用 urllib.getproxies() 读代理，
                # 而它在 Windows 上读的是注册表里的「系统代理」。只要装过
                # Clash/V2Ray 之类并开过系统代理，连 http://127.0.0.1:11434
                # 也会被塞进 http://127.0.0.1:7897 走一遭，代理不接受本地
                # 目标就回一个空 body 的 502 —— 表现是「Ollama 明明在跑，
                # 却一直报 Ollama 返回 HTTP 502」。关掉 env 代理直连才对。
                trust_env=False,
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
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_ctx": self.num_ctx,
                # ⚠️ 必须设上限。不设的话生成长度只受 num_ctx 约束，
                # 小模型一旦陷入重复就会一直吐到吃满上下文，
                # 表现就是「跑了十分钟然后超时」——既慢又没有任何产出。
                # 有了它，最坏情况变成「很快返回一段被截断的内容」，
                # 而截断的内容至少还能被解析出部分结果。
                "num_predict": self.num_predict,
            },
        }
        # format 传 JSON Schema 可让 Ollama 用语法约束解码，输出结构更可靠
        payload["format"] = schema if schema else "json"
        if self.think:
            payload["think"] = True

        client = self._get_client()
        try:
            resp = await client.post("/api/chat", json=payload)
        except httpx.ConnectError as exc:
            raise LLMError(
                f"连不上 Ollama（{self.base_url}）。请确认 Ollama 正在运行；"
                f"否则把 LLM_PROVIDER 设为 none，或改用云端模型。原始错误：{exc}"
            ) from exc
        except httpx.TimeoutException as exc:
            raise LLMError(
                f"Ollama 响应超时（{self.timeout}s）。\n"
                f"常见原因与对策：\n"
                f"  1) 提示词超过了上下文窗口（当前 num_ctx={self.num_ctx}）—— "
                f"长视频的汇总最容易触发。可调大 LLM_NUM_CTX，"
                f"或调小 LLM_CHUNK_CHARS 让中间结果更短；\n"
                f"  2) 模型太大、显存不足在跑 CPU。可用 ollama ps 查看；\n"
                f"  3) 单纯就是慢。可调大 OLLAMA_TIMEOUT，或换更小的模型。\n"
                f"原始错误：{exc}"
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"请求 Ollama 失败：{exc}") from exc

        if resp.status_code == 404:
            raise LLMError(
                f"Ollama 里没有模型 {self.model!r}。"
                f"请先执行：ollama pull {self.model}"
            )
        if resp.status_code >= 400:
            raise LLMError(
                f"Ollama 返回 HTTP {resp.status_code}：{resp.text[:300]}"
            )

        try:
            data = resp.json()
        except ValueError as exc:
            raise LLMError(f"Ollama 返回的不是 JSON：{resp.text[:300]}") from exc

        if isinstance(data, dict) and data.get("error"):
            raise LLMError(f"Ollama 报错：{data['error']}")

        content = ""
        if isinstance(data, dict):
            msg = data.get("message")
            if isinstance(msg, dict):
                content = msg.get("content") or ""
            if not content:
                content = data.get("response") or ""

        if not content.strip():
            raise LLMError("Ollama 返回了空内容")
        return content

    # ------------------------------------------------------------------
    async def health(self) -> tuple[bool, str]:
        """检查服务可达 + 目标模型是否已拉取。"""
        client = self._get_client()
        try:
            resp = await client.get("/api/tags", timeout=httpx.Timeout(5.0))
        except httpx.HTTPError as exc:
            return False, f"Ollama 不可达（{self.base_url}）：{exc}"

        if resp.status_code != 200:
            return False, f"Ollama 返回 HTTP {resp.status_code}"

        try:
            data = resp.json()
        except ValueError:
            return False, "Ollama /api/tags 返回内容异常"

        models = [
            m.get("name", "")
            for m in (data.get("models") or [])
            if isinstance(m, dict)
        ]
        if not models:
            return False, "Ollama 已运行，但一个模型都没有。请执行 ollama pull <模型名>"

        # 允许 "qwen3:8b" 匹配 "qwen3:8b" 或带 tag 的完整名
        base = self.model.split(":")[0]
        if self.model in models or any(m.split(":")[0] == base for m in models):
            return True, f"Ollama 正常，模型 {self.model} 可用"

        return (
            False,
            f"Ollama 已运行，但没有模型 {self.model!r}。"
            f"现有：{', '.join(models[:8])}。请执行 ollama pull {self.model}",
        )
