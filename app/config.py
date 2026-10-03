"""集中配置。

原则：
  1. **密钥只从环境变量 / .env 读**，代码里不出现任何默认密钥，也不会写日志。
  2. **云端默认关闭**：即使填了 API Key，没有 ``ALLOW_CLOUD=true`` 也不会外发数据。
  3. **服务只绑 127.0.0.1**：配置层就拦住，避免误绑 0.0.0.0 把笔记暴露到局域网。
  4. **数据目录固定在项目下的 data/**：用户数据不被 git 跟踪（见 .gitignore）。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录 = 本文件的上上级（app/config.py -> app/ -> 根）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: 允许绑定到的主机名 —— 仅回环地址，杜绝局域网/公网暴露
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

#: B站网页端 UA，不带它极易触发 -412 风控
DEFAULT_BILI_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class Settings(BaseSettings):
    """应用设置。环境变量名与字段名大小写不敏感（如 ``BILI_SESSDATA``）。"""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        protected_namespaces=(),
        case_sensitive=False,
    )

    # ------------------------------------------------------------------
    # 服务
    # ------------------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # ------------------------------------------------------------------
    # 数据存放（本机）
    # ------------------------------------------------------------------
    data_dir: Path = PROJECT_ROOT / "data"

    # ------------------------------------------------------------------
    # B站访问
    # ------------------------------------------------------------------
    #: 可选登录态。没有它大概率拿不到 AI 字幕，但会走 Whisper 兜底。
    #: 敏感值：绝不记录到日志、绝不写入笔记文件。
    bili_sessdata: str | None = None
    bili_user_agent: str = DEFAULT_BILI_UA
    #: 请求间隔下限（秒），避免高频触发风控
    bili_min_interval: float = Field(default=1.0, ge=0.0)
    bili_timeout: float = Field(default=20.0, gt=0.0)
    #: 风控退避重试次数
    bili_max_retries: int = Field(default=3, ge=0, le=10)
    #: 缓存元数据/字幕，避免重复出网
    cache_ttl_hours: float = Field(default=24.0 * 7, ge=0.0)

    # ------------------------------------------------------------------
    # 语音转写
    # ------------------------------------------------------------------
    whisper_model: str = "large-v3-turbo"
    whisper_device: Literal["auto", "cuda", "cpu"] = "auto"
    whisper_compute_type: Literal[
        "auto", "float16", "int8_float16", "int8", "float32"
    ] = "auto"
    #: 转写语言；zh 为中文。留空则自动检测。
    whisper_language: str = "zh"
    #: 中文强制简体（faster-whisper 常输出繁体）
    whisper_simplify_chinese: bool = True
    whisper_beam_size: int = Field(default=5, ge=1, le=10)
    #: 传给模型的提示词，用于统一术语与标点风格
    whisper_initial_prompt: str = "以下是普通话内容，请使用简体中文并加上标点。"

    # ------------------------------------------------------------------
    # 语言模型（默认本地 Ollama）
    # ------------------------------------------------------------------
    llm_provider: Literal["ollama", "openai", "none"] = "ollama"

    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3:8b"
    ollama_timeout: float = Field(default=600.0, gt=0.0)

    #: 以下三项只在 allow_cloud=True 时才会被使用
    allow_cloud: bool = False
    openai_base_url: str = "https://api.deepseek.com/v1"
    openai_api_key: str | None = None
    openai_model: str = "deepseek-chat"

    #: 每次送给模型的字符数上限（控制显存/上下文占用）
    llm_chunk_chars: int = Field(default=2400, ge=400, le=20000)
    #: 分块之间的重叠字符数，避免切断上下文
    llm_chunk_overlap_chars: int = Field(default=200, ge=0, le=2000)

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------
    @field_validator("host")
    @classmethod
    def _must_be_loopback(cls, v: str) -> str:
        """只允许绑定回环地址 —— 保证笔记不会暴露给局域网或公网。"""
        if v not in LOOPBACK_HOSTS:
            raise ValueError(
                f"出于数据安全，host 只允许 {sorted(LOOPBACK_HOSTS)}，收到 {v!r}。"
                "本应用的设计前提是「只有你自己能访问」。"
            )
        return v

    @field_validator("data_dir", mode="before")
    @classmethod
    def _expand_data_dir(cls, v: object) -> object:
        """支持相对路径与 ~ 展开，统一解析成绝对路径。"""
        if isinstance(v, str):
            p = Path(v).expanduser()
            return p if p.is_absolute() else (PROJECT_ROOT / p).resolve()
        if isinstance(v, Path):
            p = v.expanduser()
            return p if p.is_absolute() else (PROJECT_ROOT / p).resolve()
        return v

    # ------------------------------------------------------------------
    # 派生路径
    # ------------------------------------------------------------------
    @property
    def notes_dir(self) -> Path:
        """生成的 Markdown 笔记。"""
        return self.data_dir / "notes"

    @property
    def media_dir(self) -> Path:
        """下载的音频等中间产物。"""
        return self.data_dir / "media"

    @property
    def cache_dir(self) -> Path:
        """元数据/字幕缓存。"""
        return self.data_dir / "cache"

    @property
    def models_dir(self) -> Path:
        """Whisper 模型权重缓存。"""
        return self.data_dir / "models"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "mynote.db"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    def ensure_dirs(self) -> None:
        """创建所有数据目录（幂等）。"""
        for d in (
            self.data_dir,
            self.notes_dir,
            self.media_dir,
            self.cache_dir,
            self.models_dir,
            self.log_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 安全相关的便捷判断
    # ------------------------------------------------------------------
    @property
    def cloud_enabled(self) -> bool:
        """云端是否真正可用：既要选它，也要显式授权。"""
        return self.llm_provider == "openai" and self.allow_cloud

    @property
    def has_bili_login(self) -> bool:
        return bool(self.bili_sessdata and self.bili_sessdata.strip())

    def redacted(self) -> dict[str, object]:
        """可安全写日志/展示的配置快照 —— 密钥一律脱敏。"""
        return {
            "host": self.host,
            "port": self.port,
            "data_dir": str(self.data_dir),
            "llm_provider": self.llm_provider,
            "ollama_model": self.ollama_model,
            "allow_cloud": self.allow_cloud,
            "cloud_enabled": self.cloud_enabled,
            "openai_api_key": "***已设置***" if self.openai_api_key else None,
            "bili_sessdata": "***已设置***" if self.has_bili_login else None,
            "whisper_model": self.whisper_model,
            "whisper_device": self.whisper_device,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例。测试可用 ``get_settings.cache_clear()`` 重置。"""
    return Settings()
