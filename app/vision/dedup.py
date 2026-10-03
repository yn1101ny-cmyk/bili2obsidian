"""画面去重 —— 在 OCR 之前把「和上一帧差不多」的帧筛掉。

OCR 是整条链路最贵的步骤（约 0.8 秒/帧），而教程视频里大部分帧是
「讲师坐着说话」—— 画面几乎不变。先去重能省掉绝大部分 OCR。

## 为什么不用感知哈希（dHash）

调研实测（960×540，与上一帧比较）::

    方法           同内容(仅噪声)   新出现一行文字   翻页
    dHash 16×16        0~1             3            5     ← 完全区分不出来
    全图 MAD           2.86           11.58        30.74
    8×8 分块 MAD        0 块           14 块        32 块   ← 采用

dHash 把「新增一行字」判成 3、把「翻页」判成 5，两个都低于常见阈值，
于是**新内容会被当成重复帧丢掉**。分块 MAD 的区分度好得多，
而且只要约 4ms/帧，比 OCR 便宜两百倍。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TypeVar

import numpy as np

logger = logging.getLogger(__name__)

#: 分块网格（8×8 = 64 块）
DEFAULT_GRID = 8

#: 判定为「同一画面」的块差阈值（0~255 灰度）
DEFAULT_THRESHOLD = 8.0

T = TypeVar("T")


def block_signature(image: np.ndarray, grid: int = DEFAULT_GRID) -> np.ndarray:
    """把图像压成 ``grid×grid`` 的分块灰度均值矩阵。

    :param image: 灰度图 ``(H, W)`` 或彩色图 ``(H, W, 3)``
    """
    arr = np.asarray(image)
    if arr.ndim == 3:
        # 用简单平均转灰度，避免依赖 cv2 的具体色彩空间约定
        arr = arr[..., :3].mean(axis=2)
    arr = arr.astype(np.float32, copy=False)

    height, width = arr.shape[:2]
    if height == 0 or width == 0:
        return np.zeros((grid, grid), dtype=np.float32)

    grid = max(2, min(grid, height, width))
    # 用切片做块平均（比 reshape 稳，能容忍除不尽的尺寸）
    rows = np.linspace(0, height, grid + 1).astype(int)
    cols = np.linspace(0, width, grid + 1).astype(int)

    out = np.zeros((grid, grid), dtype=np.float32)
    for i in range(grid):
        for j in range(grid):
            block = arr[rows[i] : max(rows[i] + 1, rows[i + 1]),
                        cols[j] : max(cols[j] + 1, cols[j + 1])]
            out[i, j] = float(block.mean()) if block.size else 0.0
    return out


def block_distance(a: np.ndarray, b: np.ndarray) -> float:
    """两个分块签名的平均绝对差（0~255）。"""
    if a.shape != b.shape:
        return float("inf")
    return float(np.abs(a - b).mean())


def changed_block_count(
    a: np.ndarray, b: np.ndarray, *, block_threshold: float = 6.0
) -> int:
    """有多少个块发生了明显变化。

    比整体平均值更能反映「局部出现了新文字」——
    新增一行字只会让少数块变化，整体均值会被大片不变区域稀释。
    """
    if a.shape != b.shape:
        return a.size
    return int(np.count_nonzero(np.abs(a - b) > block_threshold))


def dedup_frames(
    frames: list[T],
    load: Callable[[T], np.ndarray | None],
    *,
    grid: int = DEFAULT_GRID,
    threshold: float = DEFAULT_THRESHOLD,
    min_changed_blocks: int = 3,
    always_keep_first: bool = True,
) -> list[T]:
    """去掉与上一**保留帧**几乎相同的帧。

    两个判据命中任一即保留（OR 而不是 AND，宁可多留不可错杀）：
      * 整体平均差超过 ``threshold``；
      * 有明显变化的块数达到 ``min_changed_blocks``。

    :param load: 把帧读成数组；读不出来的帧**一律保留**（交给 OCR 判断）
    """
    kept: list[T] = []
    previous_sig: np.ndarray | None = None

    for idx, frame in enumerate(frames):
        image = load(frame)
        if image is None:
            # 读不出来就保留 —— 宁可多花一点 OCR 时间，也不要漏内容
            kept.append(frame)
            previous_sig = None
            continue

        signature = block_signature(image, grid=grid)
        if previous_sig is None:
            if always_keep_first or idx == 0:
                kept.append(frame)
            previous_sig = signature
            continue

        distance = block_distance(previous_sig, signature)
        blocks = changed_block_count(previous_sig, signature)
        if distance >= threshold or blocks >= min_changed_blocks:
            kept.append(frame)
            previous_sig = signature
        # 否则：与上一保留帧几乎一致，跳过

    if len(kept) != len(frames):
        logger.info(
            "画面去重：%d -> %d 帧（省下 %d 次 OCR）",
            len(frames),
            len(kept),
            len(frames) - len(kept),
        )
    return kept
