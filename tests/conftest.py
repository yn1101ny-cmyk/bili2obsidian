"""pytest 公共夹具。

原则：**单元测试绝不联网、绝不调用真实模型**。
所有外部依赖（B站接口、LLM、ASR）都以假实现注入。
需要真实网络的验证放在显式的冒烟脚本里，不与单测混跑。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.config import Settings
from app.llm.base import LLMProvider
from app.schemas import Transcript, TranscriptSegment, TranscriptSource


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """指向临时目录的隔离配置，不读磁盘上的 .env。"""
    s = Settings(
        _env_file=None,  # 屏蔽真实 .env，保证测试可重复
        data_dir=tmp_path / "data",
        bili_min_interval=0.0,  # 测试里不需要限速等待
        llm_provider="none",
    )
    s.ensure_dirs()
    return s


# ---------------------------------------------------------------------------
# 假的 LLM
# ---------------------------------------------------------------------------


class FakeProvider(LLMProvider):
    """按预设顺序返回响应的假模型。"""

    name = "fake/test"
    is_remote = False

    def __init__(self, responses: list[str] | list[dict[str, Any]]) -> None:
        self._responses: list[str] = [
            r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)
            for r in responses
        ]
        self.calls: list[str] = []
        self.closed = False

    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any] | None = None,
        temperature: float = 0.2,
    ) -> str:
        self.calls.append(user)
        if not self._responses:
            raise AssertionError(
                f"假模型收到了第 {len(self.calls)} 次调用，但没有预设响应了"
            )
        return self._responses.pop(0)

    async def health(self) -> tuple[bool, str]:
        return True, "假模型就绪"

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def fake_provider_cls():
    """把 FakeProvider 暴露给测试，便于自建实例。"""
    return FakeProvider


# ---------------------------------------------------------------------------
# 样例数据
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_segments() -> list[TranscriptSegment]:
    """一段带时间戳的中文转写样例。"""
    return [
        TranscriptSegment(start=0.0, end=3.5, text="大家好，今天我们来聊一聊向量数据库。"),
        TranscriptSegment(start=3.5, end=8.0, text="先说结论：它不是银弹。"),
        TranscriptSegment(start=8.0, end=15.2, text="向量数据库的核心是近似最近邻搜索，"),
        TranscriptSegment(start=15.2, end=21.0, text="英文叫 ANN，用牺牲一点准确率换速度。"),
        TranscriptSegment(start=21.0, end=30.0, text="常见的有 HNSW、IVF 这些索引结构。"),
    ]


@pytest.fixture
def sample_transcript(sample_segments: list[TranscriptSegment]) -> Transcript:
    return Transcript(
        segments=sample_segments,
        source=TranscriptSource.WHISPER,
        language="zh",
        note="测试用转写",
    )


@pytest.fixture
def sample_note_payload() -> dict[str, Any]:
    """一份合法的笔记 JSON（字段名故意用模型的常见变体）。"""
    return {
        "title": "向量数据库到底解决什么问题",
        "goal": "能判断自己的场景该不该上向量数据库，以及该选哪类索引。",
        "one_liner": "向量数据库不是银弹，它只解决高维数据的近似最近邻检索。",
        "summary": "视频先给出结论，再解释 ANN 的原理，最后介绍 HNSW 和 IVF 两类索引。",
        "focusPoints": [
            {
                "title": "先确认你的场景是不是「高维相似度检索」",
                "detail": "如果不是，用传统数据库加索引就够了，上向量库只会增加运维负担。",
                "timestamp": "00:00",
            },
            {
                "title": "索引选型先看召回率要求，再看数据量",
                "detail": "HNSW 召回率高但吃内存；IVF 省内存但要调 nprobe。",
                "timestamp": "08:00",
            },
        ],
        "actionables": [
            {
                "kind": "command",
                "title": "建 HNSW 索引",
                "content": "CREATE INDEX ON items USING hnsw (embedding vector_cosine_ops);",
                "note": "向量维度必须与建表时一致。",
                "timestamp": "08:00",
            },
            {
                "kind": "checklist",
                "title": "上线前检查",
                "content": "1. 确认召回率达标\n2. 确认内存余量\n3. 确认有降级方案",
            },
        ],
        "outline": [
            {"timestamp": 0, "heading": "开场", "summary": "提出主题"},
            {"timestamp": 8, "heading": "核心原理", "summary": "讲 ANN 的取舍"},
        ],
        "keyPoints": [
            {
                "title": "ANN 用准确率换速度",
                "detail": "精确最近邻在高维上太慢，ANN 允许少量误差换取数量级提速。",
                "source": "video",
                "timestamp": 8,
            }
        ],
        "concepts": [
            {
                "term": "ANN",
                "explanation": "近似最近邻，不保证找到最接近的那个，但保证很快。",
                "source": "video",
                "example": "像在图书馆里按大致区域找书，而不是逐本比对。",
                "timestamp": 15.2,
            }
        ],
        "follow_ups": [
            {
                "question": "ANN 的召回率怎么调？",
                "why": "视频提到「牺牲一点准确率」但没给出调节手段。",
                "where": "ANN 召回率 参数",
            }
        ],
        "uncertainties": [
            {"item": "HNSW 的具体参数", "reason": "转写里一带而过，没有给出数值"}
        ],
        "tags": ["向量数据库", "检索"],
    }
