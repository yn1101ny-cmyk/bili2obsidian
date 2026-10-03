"""领域模型。

设计要点：**区分「视频原文」与「模型补充」**。
``Note`` 里的每一项都带 ``source`` 字段，渲染时会明确标注来源，
避免把模型的推断当成 UP 主说过的话 —— 这是这类工具最容易误导人的地方。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# 来源标注
# ---------------------------------------------------------------------------

Source = Literal["video", "model", "mixed"]


class TranscriptSource(str, Enum):
    """转写文本是怎么来的 —— 影响我们对准确率的信心。"""

    BILI_CC = "bili_cc"          # UP 主上传的人工字幕，最准
    BILI_AI = "bili_ai"          # B站自动生成的 AI 字幕
    WHISPER = "whisper"          # 本地 Whisper 转写
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        return {
            "bili_cc": "B站人工字幕",
            "bili_ai": "B站AI字幕",
            "whisper": "本地Whisper转写",
            "unknown": "未知来源",
        }[self.value]

    @property
    def confidence(self) -> str:
        return {
            "bili_cc": "高",
            "bili_ai": "中",
            "whisper": "中",
            "unknown": "低",
        }[self.value]


# ---------------------------------------------------------------------------
# 视频与转录
# ---------------------------------------------------------------------------


class TranscriptSegment(BaseModel):
    """一条带时间戳的文本（字幕行 / Whisper 片段）。"""

    start: float = Field(ge=0.0, description="起始秒")
    end: float = Field(ge=0.0, description="结束秒")
    text: str

    @field_validator("end")
    @classmethod
    def _end_after_start(cls, v: float, info) -> float:
        start = info.data.get("start")
        if start is not None and v < start:
            return start
        return v

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def clock(self) -> str:
        """格式化成 mm:ss 或 h:mm:ss。"""
        return format_clock(self.start)


class VideoPage(BaseModel):
    """多分P视频中的一集。"""

    cid: int
    page: int
    title: str
    duration: int = 0


class VideoMeta(BaseModel):
    """视频元数据。"""

    bvid: str
    aid: int | None = None
    title: str
    #: 分P标题（单P视频为空）
    part_title: str = ""
    desc: str = ""
    owner_name: str = ""
    owner_mid: int | None = None
    duration: int = 0
    cover_url: str = ""
    pubdate: datetime | None = None
    tags: list[str] = Field(default_factory=list)
    #: 当前处理的 cid / 分P序号（从 1 开始）
    cid: int | None = None
    page: int = 1
    total_pages: int = 1
    #: 多P时所有分P，便于后续批量处理
    pages: list[VideoPage] = Field(default_factory=list)

    @property
    def display_title(self) -> str:
        if self.part_title and self.part_title != self.title:
            return f"{self.title} - {self.part_title}"
        return self.title

    @property
    def url(self) -> str:
        base = f"https://www.bilibili.com/video/{self.bvid}"
        if self.page > 1:
            return f"{base}?p={self.page}"
        return base

    def timestamped_url(self, seconds: float) -> str:
        """生成可跳转到指定时间点的链接。"""
        return f"{self.url}&t={int(seconds)}"


class Transcript(BaseModel):
    """完整的转写结果。"""

    segments: list[TranscriptSegment] = Field(default_factory=list)
    source: TranscriptSource = TranscriptSource.UNKNOWN
    language: str = "zh"
    #: 转写耗时等诊断信息
    note: str = ""

    @property
    def full_text(self) -> str:
        return "\n".join(s.text for s in self.segments if s.text.strip())

    @property
    def duration(self) -> float:
        return self.segments[-1].end if self.segments else 0.0

    @property
    def char_count(self) -> int:
        return sum(len(s.text) for s in self.segments)

    def is_empty(self) -> bool:
        return not any(s.text.strip() for s in self.segments)


# ---------------------------------------------------------------------------
# 笔记结构（这也是喂给 LLM 的 JSON Schema）
# ---------------------------------------------------------------------------


class OutlineItem(BaseModel):
    """带时间戳的大纲条目 —— 支撑「跳回原视频核对」。"""

    timestamp: float = Field(default=0.0, ge=0.0, description="该章节起始秒")
    heading: str = Field(description="章节标题，简短")
    summary: str = Field(description="这一节讲了什么，1-2 句")

    def clock(self) -> str:
        return format_clock(self.timestamp)


class KeyPoint(BaseModel):
    """核心要点。"""

    title: str = Field(description="要点，一句话说清")
    detail: str = Field(description="展开解释，包含必要的推理过程")
    source: Source = Field(default="video", description="video=视频明确讲了; model=模型补充")
    timestamp: float | None = Field(default=None, ge=0.0, description="对应视频时间点，没有则 null")

    def clock(self) -> str:
        return format_clock(self.timestamp) if self.timestamp is not None else ""


class Concept(BaseModel):
    """关键概念解释 —— 目标是「没看过视频的人也能看懂」。"""

    term: str = Field(description="概念名称")
    explanation: str = Field(description="用大白话解释，必要时打比方")
    source: Source = Field(default="video")
    example: str | None = Field(default=None, description="具体的例子或类比")
    timestamp: float | None = Field(default=None, ge=0.0)

    def clock(self) -> str:
        return format_clock(self.timestamp) if self.timestamp is not None else ""


class Supplement(BaseModel):
    """补充背景知识 —— 视频没讲但理解它需要知道的内容。"""

    topic: str = Field(description="补充的主题")
    why: str = Field(description="为什么理解原视频需要知道这个")
    content: str = Field(description="补充内容本身，讲清楚，不要只给名词")
    source: Source = Field(default="model")


class Uncertainty(BaseModel):
    """存疑 / 待验证 —— 诚实标注模型没把握的地方。"""

    item: str = Field(description="存疑的内容")
    reason: str = Field(description="为什么存疑：转写不清？前后矛盾？超出知识范围？")


class Note(BaseModel):
    """一份完整的笔记。这是 LLM 结构化输出的目标结构。"""

    title: str = Field(description="笔记标题，可以是更好的概括性标题")
    one_liner: str = Field(description="一句话总结：这个视频最核心的结论是什么")
    summary: str = Field(description="3-5 句话的摘要，覆盖全片脉络")
    outline: list[OutlineItem] = Field(default_factory=list)
    key_points: list[KeyPoint] = Field(default_factory=list)
    concepts: list[Concept] = Field(default_factory=list)
    supplements: list[Supplement] = Field(default_factory=list)
    uncertainties: list[Uncertainty] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)

    def is_thin(self) -> bool:
        """笔记是否信息量不足（用于自检并重试）。"""
        return not self.key_points and not self.summary.strip()


# ---------------------------------------------------------------------------
# 任务
# ---------------------------------------------------------------------------


class JobStage(str, Enum):
    QUEUED = "queued"
    RESOLVING = "resolving"      # 解析链接 + 取元数据
    FETCHING_SUBS = "fetching_subs"
    DOWNLOADING = "downloading"  # 下载音频
    TRANSCRIBING = "transcribing"
    SUMMARIZING = "summarizing"
    RENDERING = "rendering"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def label(self) -> str:
        return {
            "queued": "排队中",
            "resolving": "解析视频信息",
            "fetching_subs": "获取字幕",
            "downloading": "下载音频",
            "transcribing": "语音转写",
            "summarizing": "生成笔记",
            "rendering": "渲染保存",
            "done": "完成",
            "failed": "失败",
            "cancelled": "已取消",
        }[self.value]

    @property
    def is_terminal(self) -> bool:
        return self in (JobStage.DONE, JobStage.FAILED, JobStage.CANCELLED)


class Job(BaseModel):
    """一次「链接 → 笔记」的任务。"""

    id: str
    url: str
    stage: JobStage = JobStage.QUEUED
    progress: float = Field(default=0.0, ge=0.0, le=1.0)
    message: str = ""
    error: str = ""
    bvid: str = ""
    note_id: str | None = None
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    #: 供前端展示的阶段性日志
    log: list[str] = Field(default_factory=list)

    def push(self, message: str) -> None:
        self.message = message
        self.log.append(message)
        self.updated_at = datetime.now()


class NoteRecord(BaseModel):
    """落库的笔记记录（正文在 Markdown 文件里，库里只存索引与元信息）。"""

    id: str
    bvid: str
    title: str
    one_liner: str
    tags: list[str] = Field(default_factory=list)
    transcript_source: TranscriptSource = TranscriptSource.UNKNOWN
    duration: float = 0.0
    char_count: int = 0
    markdown_path: str = ""
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def format_clock(seconds: float | None) -> str:
    """秒 -> ``mm:ss`` / ``h:mm:ss``。负数与 None 返回空串。"""
    if seconds is None:
        return ""
    total = int(seconds)
    if total < 0:
        total = 0
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"
