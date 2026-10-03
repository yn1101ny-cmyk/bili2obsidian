"""异常类型。统一在这里定义，便于测试断言与上层做降级决策。"""

from __future__ import annotations


class MyNoteError(Exception):
    """所有本项目错误的基类。"""


class ConfigError(MyNoteError):
    """配置非法，例如云端 LLM 未获授权却被调用。"""


class InvalidVideoUrl(MyNoteError):
    """用户给的链接无法解析成 B 站视频。"""

    def __init__(self, raw: str, reason: str = "") -> None:
        self.raw = raw
        self.reason = reason
        detail = f"无法解析视频链接: {raw!r}"
        if reason:
            detail += f"（{reason}）"
        super().__init__(detail)


class BiliApiError(MyNoteError):
    """B 站接口返回了非 0 code 或结构不符合预期。"""

    def __init__(self, endpoint: str, code: int | None, message: str) -> None:
        self.endpoint = endpoint
        self.code = code
        self.message = message
        super().__init__(f"B站接口 {endpoint} 失败 (code={code}): {message}")


class NoTranscriptAvailable(MyNoteError):
    """既没有字幕，音频转写也失败/为空。"""


class DownloadError(MyNoteError):
    """音频下载失败。"""


class TranscriptionError(MyNoteError):
    """ASR 转写出错。"""


class LLMError(MyNoteError):
    """本地或云端模型调用失败。"""


class LLMOutputInvalid(LLMError):
    """模型返回的内容不是合法的 JSON / 不符合期望结构。"""


class CloudNotAllowed(ConfigError):
    """配置了云端模型但未显式授权 —— 防止数据在用户不知情时外发。"""

    def __init__(self) -> None:
        super().__init__(
            "检测到云端 LLM 配置，但未授权外发数据。"
            "若确认要将字幕文本发送给第三方服务，请显式设置 ALLOW_CLOUD=true。"
        )
