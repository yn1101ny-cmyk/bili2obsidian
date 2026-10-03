"""离线 OCR —— 基于 RapidOCR（ONNXRuntime 后端）。

## 为什么是 RapidOCR

* **完全离线**：模型（PP-OCRv6 det/rec + cls，约 36MB）**打包在 wheel 里**，
  从 ``site-packages/rapidocr/models/`` 读取并做 SHA256 校验，
  默认路径不发任何网络请求。实测把代理指向死端口仍正常工作。
* **pip 可装、零编译**：纯 Python wheel（``py3-none-any``）。
  ⚠️ 注意是**新版 ``rapidocr``**；旧的 ``rapidocr-onnxruntime`` 声明
  ``Requires-Python <3.13``，在 Python 3.13 上装不了。
* **中英混排**：教程视频常是「中文讲解 + 英文代码」，实测同一次推理能正确识别。

## 线程数必须显式设置（实测 3.5 倍差距）

在这颗 8P+16E 的混合架构 CPU 上，ONNXRuntime 用满 24 个逻辑核**反而最慢**：
线程池栅栏被 E-core 拖住。本机实测 960×540 满屏文字：

    threads=24  →  2.881 s/帧
    threads=8   →  0.820 s/帧     ← 显式设成物理核数

所以这里**必须**传 ``intra_op_num_threads``，不能吃默认值。
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

#: 默认线程数。取常见笔记本的性能核数量；这颗 275HX 是 8P+16E。
DEFAULT_THREADS = 8

_engine = None
_lock = threading.Lock()


def is_available() -> tuple[bool, str]:
    """检查 OCR 是否可用（不加载模型）。"""
    try:
        import rapidocr  # noqa: F401
    except ImportError:
        return False, (
            "未安装 rapidocr。如需识别画面上的文字（教程类视频很有用），"
            "请执行：pip install rapidocr"
        )
    return True, "RapidOCR 已安装（模型随包提供，完全离线）"


def suggested_threads() -> int:
    """给出建议线程数。

    ⚠️ 不要用 ``os.cpu_count()``（那是逻辑核）—— 实测用满逻辑核最慢。
    混合架构下按**物理性能核**来更合适，这里保守取一半并夹在 [4, 12]。
    """
    logical = os.cpu_count() or 4
    return max(4, min(12, logical // 2))


class OcrEngine:
    """RapidOCR 的薄封装。模型只加载一次。"""

    def __init__(self, *, threads: int | None = None) -> None:
        self.threads = threads or suggested_threads()
        self._engine = None
        self._init_lock = threading.Lock()

    # ------------------------------------------------------------------
    def _load(self):
        if self._engine is not None:
            return self._engine
        with self._init_lock:
            if self._engine is not None:
                return self._engine

            # rapidocr 默认的信息级日志太吵（每张图都打模型路径）
            logging.getLogger("RapidOCR").setLevel(logging.WARNING)

            try:
                from rapidocr import RapidOCR
            except ImportError as exc:
                raise RuntimeError(
                    "未安装 rapidocr。请执行：pip install rapidocr"
                ) from exc

            self._engine = RapidOCR(
                params={
                    # ⚠️ 必须显式设置，默认值在这类混合架构 CPU 上慢 3.5 倍
                    "EngineConfig.onnxruntime.intra_op_num_threads": self.threads,
                    # 限制检测边长别乱放大：默认的 min 策略会把小图**反向放大**，
                    # 喂 640×360 实际检测输入会变成 1304×736，白白变慢
                    "Det.limit_type": "max",
                    "Det.limit_side_len": 960,
                }
            )
            logger.info("OCR 引擎就绪（onnxruntime，线程 %d）", self.threads)
            return self._engine

    # ------------------------------------------------------------------
    def recognize(self, image: str | Path) -> list[str]:
        """识别一张图里的所有文本行。识别不出返回空列表。"""
        engine = self._load()
        try:
            result = engine(str(image))
        except Exception as exc:  # noqa: BLE001 - 单张图失败不该中断整条链路
            logger.debug("OCR 失败 %s：%s", image, exc)
            return []

        texts = getattr(result, "txts", None)
        if not texts:
            return []
        return [str(t).strip() for t in texts if str(t).strip()]

    # ------------------------------------------------------------------
    def unload(self) -> None:
        """释放引擎占用的内存。"""
        with self._init_lock:
            self._engine = None
