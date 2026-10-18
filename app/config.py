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
    #: 抓多少条高赞评论用于「观众勘误」（0 = 关闭）。
    #: 评论区经常有人指出视频里的错误、补上更准的型号或数据，
    #: 对教程和评测类视频尤其有用。
    bili_top_comments: int = Field(default=20, ge=0, le=50)
    #: 缓存元数据/字幕，避免重复出网
    cache_ttl_hours: float = Field(default=24.0 * 7, ge=0.0)

    # ------------------------------------------------------------------
    # 语音转写
    # ------------------------------------------------------------------
    whisper_model: str = "large-v3-turbo"
    whisper_device: Literal["auto", "cuda", "cpu"] = "auto"
    #: 计算精度。
    #: ⚠️ **不要用 auto**：auto 会选 int8，而 Blackwell(RTX 50系, sm_120)
    #: 上 int8 路径极易抛 CUBLAS_STATUS_NOT_SUPPORTED。
    #: GPU 请显式用 float16；CPU 用 int8。
    whisper_compute_type: Literal[
        "auto", "float16", "int8_float16", "int8", "float32"
    ] = "float16"
    #: 转写语言；zh 为中文。留空则自动检测（偶尔会误判成粤语/日语，不建议留空）。
    whisper_language: str = "zh"
    #: 中文强制简体。faster-whisper 常输出繁体，
    #: OpenCC(t2s) 是确定性兜底，提示词只能引导不能保证。
    whisper_simplify_chinese: bool = True
    #: 复读清理阈值：同一个字符连续出现这么多次就压缩成一个。
    #:
    #: 实测：56 分钟的视频里 Whisper 会在多处整段重复同一个字
    #: （``替替替替…`` / ``宗宗宗宗…``）。这些垃圾会被模型当成内容去总结。
    #: 默认 6 —— 正常的「哈哈」「谢谢」不受影响。
    whisper_repeat_min_run: int = Field(default=6, ge=3, le=20)
    whisper_beam_size: int = Field(default=5, ge=1, le=10)
    #: 批处理推理，可提速 3-5 倍
    whisper_use_batched: bool = True
    whisper_batch_size: int = Field(default=8, ge=1, le=64)
    #: 传给模型的提示词。
    #: ⚠️ 必须是**完整通顺的简体句子** —— 实测单词/短语（如「听起来有点呆」）
    #: 完全无效，写法不规范还可能把模型带偏。
    whisper_initial_prompt: str = "以下是普通话的句子，这是一段中文讲解录音。"

    # ------------------------------------------------------------------
    # 模型权重下载（HuggingFace）
    # ------------------------------------------------------------------
    #: HuggingFace 镜像端点。留空则用官方地址。
    #:
    #: 为什么需要：国内**主站可达但文件 CDN 被阻断** ——
    #: 实测 huggingface.co 首页 179ms 响应正常，但下载 model.bin 直接超时。
    #: 设为 ``https://hf-mirror.com`` 后实测 2.5 MB/s，1.6GB 模型约 11 分钟。
    hf_endpoint: str | None = None
    #: 关闭 HuggingFace 新的 Xet 传输协议。
    #:
    #: 实测：开着 Xet 时国内下载卡在 **0 字节**数分钟毫无进展；
    #: 关掉后立刻跑到 2.5 MB/s。默认关闭以保证可靠性。
    hf_disable_xet: bool = True

    # ------------------------------------------------------------------
    # 画面识别（OCR）—— 「不单纯分析音频」
    # ------------------------------------------------------------------
    #: 是否识别画面上的文字。
    #:
    #: 教程类视频里提示词、代码、终端输出往往**只出现在画面上**，
    #: 光靠音频转写会全部丢失（实测把 git 听成了 Gate）。
    #: 需要 pip install rapidocr（模型随包提供，完全离线）。
    vision_enabled: bool = True
    #: 抽帧间隔（秒）。太长会漏内容，太短会很慢。
    vision_interval: float = Field(default=15.0, ge=2.0, le=120.0)
    #: 单次最多抽多少帧（长视频会自动放宽间隔）
    vision_max_frames: int = Field(default=240, ge=10, le=2000)
    #: 下载视频时限制的高度。抽帧做 OCR 用 480p 足够。
    vision_max_height: int = Field(default=480, ge=144, le=1080)
    #: OCR 线程数。
    #: ⚠️ 不要用满逻辑核 —— 实测混合架构 CPU 上用满 24 线程比用 8 线程慢 3.5 倍。
    #: 留空则自动取逻辑核的一半（夹在 4~12）。
    vision_ocr_threads: int | None = Field(default=None, ge=1, le=64)
    #: 画面文字少于这么多行时，就不塞进笔记提示词（避免噪音）
    vision_min_lines: int = Field(default=3, ge=0, le=100)

    # ------------------------------------------------------------------
    # 语言模型（默认本地 Ollama）
    # ------------------------------------------------------------------
    llm_provider: Literal["ollama", "openai", "none"] = "ollama"

    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3:8b"
    ollama_timeout: float = Field(default=600.0, gt=0.0)
    #: 上下文窗口。
    #:
    #: ⚠️ 实测教训：默认 8192 太小时，长视频的汇总提示词
    #: （8 个分段的中间结果拼起来）会**超过上下文**，
    #: 模型随即陷入异常并一直跑到超时 —— 56 分钟的视频就这样失败了。
    #: 现在改成按体量做中间归并，同时把窗口提到 16384 留出余量。
    llm_num_ctx: int = Field(default=16384, ge=2048, le=131072)

    #: ⚠️ 单次生成的最大 token 数。**不要设为 -1（表示不限）**。
    #:
    #: 不设上限时，生成长度只受 ``num_ctx`` 约束 —— 小模型一旦陷入重复
    #: 就会一直吐到吃满上下文，实测表现是「跑了十分钟然后超时」：
    #: 既慢，又拿不到任何产出。
    #:
    #: 有了上限，最坏情况变成「很快返回一段被截断的内容」，
    #: 而截断的 JSON 通常还能被解析出部分结果，好过彻底失败。
    #:
    #: 4096 对「长视频 + 中文 JSON」偏小：实测一条 6 分半的视频，模型写到
    #: actionables 中途就撞上限，两次都拿不到合法 JSON。8192 留出余量，
    #: 又不至于让小模型一口气吐太久。
    llm_num_predict: int = Field(default=8192, ge=256, le=32768)

    #: 以下三项只在 allow_cloud=True 时才会被使用
    allow_cloud: bool = False
    openai_base_url: str = "https://api.deepseek.com/v1"
    openai_api_key: str | None = None
    openai_model: str = "deepseek-chat"

    # ------------------------------------------------------------------
    # 本地出稿 + 云端单次润色（默认关闭）
    # ------------------------------------------------------------------
    #: 本地模型先按现有流程把笔记**完整生成**（0 元、数据不出本机），
    #: 再把「已经成稿的笔记结构」交给云端模型改一遍：重排结构、去 AI 味、
    #: 加作者口吻。**只调用一次，不重新读转写**。
    #:
    #: 为什么这样分工：本地 8B 模型「把事实抽全」够用，但「写得像人」
    #: 明显不行；云端整篇重写又贵、还容易把时间戳和细节写丢。
    #: 让本地负责「准」、云端负责「顺」，一篇长视频的润色成本只要几分钱。
    #:
    #: ⚠️ 仍受 ``ALLOW_CLOUD`` 约束：没有显式授权就不外发任何内容。
    polish_enabled: bool = False
    #: 润色用的接口与模型。留空则复用 ``OPENAI_*``。
    polish_base_url: str = ""
    polish_api_key: str | None = None
    polish_model: str = ""
    polish_timeout: float = Field(default=180.0, gt=0.0)
    #: 润色输出上限。笔记正文一般 3000~8000 字，8192 够用。
    polish_max_tokens: int = Field(default=8192, ge=512, le=32768)
    #: 润色温度。比正文生成（0.0）高一点，留出改写的余地；
    #: 太高会把事实改走样，0.5 是实测比较稳的点。
    polish_temperature: float = Field(default=0.5, ge=0.0, le=1.2)
    #: 额外的润色要求，直接拼进提示词。
    #: 例如「我的风格：短句、少用破折号、别用『首先/其次』」。
    polish_instructions: str = ""
    #: 初稿超过这么多 token 就不润色（避免异常长笔记烧钱）。
    polish_max_input_tokens: int = Field(default=24000, ge=1000)

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
    def polish_ready(self) -> bool:
        """云端润色是否真正可用。

        三个条件缺一不可：开启开关、显式授权外发、拿到 API Key；
        另外，如果整篇笔记本来就由云端生成（``LLM_PROVIDER=openai``），
        再润色一遍只是白花钱，直接跳过。
        """
        if not self.polish_enabled or not self.allow_cloud:
            return False
        if self.llm_provider == "openai":
            return False
        key = self.polish_api_key or self.openai_api_key or ""
        return bool(key.strip())

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
            "polish_enabled": self.polish_enabled,
            "polish_ready": self.polish_ready,
            "polish_model": self.polish_model or self.openai_model,
            "openai_api_key": "***已设置***" if self.openai_api_key else None,
            "bili_sessdata": "***已设置***" if self.has_bili_login else None,
            "whisper_model": self.whisper_model,
            "whisper_device": self.whisper_device,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例。测试可用 ``get_settings.cache_clear()`` 重置。"""
    return Settings()
