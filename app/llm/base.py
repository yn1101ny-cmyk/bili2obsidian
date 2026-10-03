"""LLM 提供方接口与共用的 JSON 处理。

**出网说明**：本包中只有 ``ollama_provider.py``（本机回环）与
``openai_provider.py``（可选云端，默认关闭）会发起网络请求。
``tests/test_privacy.py`` 会静态校验这一点。
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import Any

from app.errors import LLMError, LLMOutputInvalid

#: 匹配 ```json ... ``` 代码块
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


class LLMProvider(ABC):
    """所有模型提供方的统一接口。"""

    #: 展示名，会写进笔记的元信息
    name: str = "unknown"
    #: 是否会把数据发到本机之外 —— 用于隐私提示与审计
    is_remote: bool = False

    @abstractmethod
    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any] | None = None,
        temperature: float = 0.2,
    ) -> str:
        """要求模型返回 JSON 字符串。

        :param schema: 可选的 JSON Schema，用于约束输出结构
        :returns: 模型返回的原始文本
        """

    async def complete_text(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.2,
    ) -> str:
        """纯文本生成（默认复用 JSON 接口）。"""
        return await self.complete_json(
            system=system, user=user, schema=None, temperature=temperature
        )

    @abstractmethod
    async def health(self) -> tuple[bool, str]:
        """返回 ``(是否可用, 说明)``，用于环境自检。"""

    async def aclose(self) -> None:
        """释放连接资源（默认无操作）。"""


def extract_json(text: str) -> Any:
    """从模型输出里稳健地取出 JSON 对象。

    依次尝试：直接解析 → 去 ``` 围栏 → 截取第一个 ``{`` 到最后一个 ``}``。
    """
    if text is None:
        raise LLMOutputInvalid("模型返回为空")

    raw = text.strip()
    if not raw:
        raise LLMOutputInvalid("模型返回为空")

    # 1. 直接就是 JSON
    parsed = _try_parse(raw)
    if parsed is not None:
        return parsed

    # 2. 去掉 markdown 代码围栏
    for m in _FENCE_RE.finditer(raw):
        parsed = _try_parse(m.group(1).strip())
        if parsed is not None:
            return parsed

    # 3. 截取最外层大括号
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        parsed = _try_parse(raw[start : end + 1])
        if parsed is not None:
            return parsed

    raise LLMOutputInvalid(f"无法从模型输出中解析出 JSON：{raw[:200]!r}")


def _try_parse(text: str) -> Any | None:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def coerce_str(value: Any, default: str = "") -> str:
    """把模型可能给出的非字符串值转成字符串。"""
    if value is None:
        return default
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "；".join(coerce_str(v) for v in value if v is not None)
    if isinstance(value, dict):
        return "；".join(f"{k}: {coerce_str(v)}" for k, v in value.items())
    return default


def coerce_float(value: Any) -> float | None:
    """把 ``"01:23"`` / ``83`` / ``83.5`` 都转成秒数。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return max(0.0, float(value))
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        # mm:ss 或 h:mm:ss
        if ":" in s:
            parts = s.split(":")
            try:
                nums = [float(p) for p in parts]
            except ValueError:
                return None
            seconds = 0.0
            for n in nums:
                seconds = seconds * 60 + n
            return max(0.0, seconds)
        try:
            return max(0.0, float(s))
        except ValueError:
            return None
    return None


def coerce_str_list(value: Any) -> list[str]:
    """把模型的任意输出规范成字符串列表。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for v in value:
            s = coerce_str(v).strip()
            if s:
                out.append(s)
        return out
    if isinstance(value, dict):
        return [f"{k}: {coerce_str(v)}" for k, v in value.items()]
    return []


def clamp_source(value: Any) -> str:
    """把 source 字段收敛到 video / model / mixed。"""
    s = coerce_str(value).strip().lower()
    if s in ("video", "model", "mixed"):
        return s
    # 模型可能写中文
    if "视频" in s and "补" not in s:
        return "video"
    if "补" in s or "model" in s or "模型" in s:
        return "model"
    return "video"


def truncate_middle(text: str, limit: int) -> str:
    """超长文本从中间截断，保留首尾（首尾信息密度通常更高）。"""
    if limit <= 0 or len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    return f"{text[:head]}\n……（中间省略 {len(text) - limit} 字）……\n{text[-tail:]}"


__all__ = [
    "LLMProvider",
    "LLMError",
    "LLMOutputInvalid",
    "extract_json",
    "coerce_str",
    "coerce_float",
    "coerce_str_list",
    "clamp_source",
    "truncate_middle",
]
