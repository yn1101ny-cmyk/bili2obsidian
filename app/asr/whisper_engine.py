"""本地语音转写 —— 基于 faster-whisper。

**隐私**：模型在第一次使用时从 HuggingFace 下载到 ``data/models``，
之后完全离线推理。转写过程不出本机。

Windows + CUDA 的两个坑，这里都处理了：

1. **DLL 找不到**：pip 装的 CUDA 运行库（``nvidia-cublas-cu12`` /
   ``nvidia-cudnn-cu12``）不在默认搜索路径里，需要手动加
   ``os.add_dll_directory``，否则报 ``Library cublas64_12.dll is not found``。
2. **算力不匹配**：RTX 50 系是 Blackwell（sm_120），
   旧的 CTranslate2 会报 ``no kernel image is available``。
   检测到这类错误时自动降级到 CPU，并给出明确提示，而不是直接崩。
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from app.config import Settings
from app.errors import TranscriptionError
from app.schemas import Transcript, TranscriptSegment, TranscriptSource

logger = logging.getLogger(__name__)

#: 这些错误信息说明 GPU 路径不可用，应当降级 CPU
_GPU_FALLBACK_MARKERS = (
    "no kernel image is available",
    "cudaErrorNoKernelImageForDevice",
    "cublas64_",
    "cudnn64_",
    "Library cublas",
    "Library cudnn",
    "CUDA driver version is insufficient",
    "CUDA failed with error",
    "out of memory",
    "device kernel image is invalid",
)

#: 简体中文转换器（可选依赖，缺失则退化为仅用 initial_prompt 引导）
_t2s_converter: Any = None
_t2s_tried = False


def _get_t2s_converter() -> Any:
    """懒加载繁→简转换器。装不上也没关系，会用提示词兜底。"""
    global _t2s_converter, _t2s_tried
    if _t2s_tried:
        return _t2s_converter
    _t2s_tried = True
    try:
        from opencc import OpenCC  # type: ignore

        _t2s_converter = OpenCC("t2s")
        logger.info("已启用 OpenCC 繁体转简体")
    except Exception as exc:  # noqa: BLE001 - 可选依赖，任何失败都降级
        logger.info("未安装 opencc，跳过繁转简（将依赖提示词引导）：%s", exc)
        _t2s_converter = None
    return _t2s_converter


def to_simplified(text: str) -> str:
    """尽力把繁体中文转成简体。没有 opencc 时原样返回。"""
    conv = _get_t2s_converter()
    if conv is None or not text:
        return text
    try:
        return conv.convert(text)
    except Exception:  # noqa: BLE001 - 转换失败不该影响主流程
        return text


def setup_cuda_dll_paths() -> list[str]:
    """把 pip 安装的 CUDA 运行库目录加入 DLL 搜索路径（仅 Windows）。

    :returns: 实际加入的目录列表，便于自检展示
    """
    added: list[str] = []
    if sys.platform != "win32":
        return added

    try:
        import site

        roots: list[str] = []
        for sp in site.getsitepackages():
            roots.append(sp)
        user_site = site.getusersitepackages()
        if user_site:
            roots.append(user_site)
        # 虚拟环境下 sys.prefix 下的 site-packages
        roots.append(str(Path(sys.prefix) / "Lib" / "site-packages"))
    except Exception:  # noqa: BLE001
        roots = []

    seen: set[str] = set()
    for root in roots:
        nvidia = Path(root) / "nvidia"
        if not nvidia.is_dir():
            continue
        # nvidia/cublas/bin, nvidia/cudnn/bin, nvidia/cuda_runtime/bin ...
        for bin_dir in nvidia.glob("*/bin"):
            key = str(bin_dir).lower()
            if key in seen or not bin_dir.is_dir():
                continue
            seen.add(key)
            try:
                os.add_dll_directory(str(bin_dir))
            except (OSError, AttributeError) as exc:
                logger.debug("add_dll_directory 失败 %s: %s", bin_dir, exc)
                continue
            # 同时写进 PATH，兼容老版本加载方式
            os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
            added.append(str(bin_dir))

    if added:
        logger.info("已加入 %d 个 CUDA 运行库目录", len(added))
    return added


def detect_device(preference: str = "auto") -> tuple[str, str]:
    """决定用 GPU 还是 CPU。

    :returns: ``(device, compute_type)``，例如 ``("cuda", "float16")``
    """
    if preference == "cpu":
        return "cpu", "int8"

    cuda_ok, reason = _probe_cuda()
    if cuda_ok:
        return "cuda", "float16"

    if preference == "cuda":
        raise TranscriptionError(
            f"配置要求用 CUDA，但 GPU 不可用：{reason}\n"
            "排查建议：\n"
            "  1) 确认 nvidia-smi 能正常输出（驱动是否安装）；\n"
            "  2) 安装 CUDA 运行库："
            "pip install nvidia-cublas-cu12 nvidia-cudnn-cu12；\n"
            "  3) 若报 no kernel image / sm_120，说明 CTranslate2 版本太旧，"
            "请升级：pip install -U ctranslate2 faster-whisper；\n"
            "  4) 也可以把 WHISPER_DEVICE 设为 cpu 先用起来。"
        )

    logger.warning("GPU 不可用（%s），自动降级到 CPU 转写", reason)
    return "cpu", "int8"


def _probe_cuda() -> tuple[bool, str]:
    """尝试真正初始化一次 CUDA，确认能跑而不是只看驱动在不在。"""
    setup_cuda_dll_paths()
    try:
        import ctranslate2
    except ImportError as exc:
        return False, f"未安装 ctranslate2（{exc}）"

    try:
        count = ctranslate2.get_cuda_device_count()
    except Exception as exc:  # noqa: BLE001 - ctranslate2 会抛各种原生异常
        return False, f"查询 CUDA 设备失败：{exc}"

    if count <= 0:
        return False, "ctranslate2 未检测到可用 CUDA 设备"

    # 真正建一个小模型跑一次，才能暴露 sm_120 之类的算力不匹配问题
    try:
        import numpy as np

        model = ctranslate2.models.Whisper(
            _ensure_probe_model(), device="cuda", compute_type="float16"
        )
        features = np.zeros((1, 80, 3000), dtype=np.float32)
        model.encode(features)
        return True, f"CUDA 可用（{count} 个设备，实测推理通过）"
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if any(marker in msg for marker in _GPU_FALLBACK_MARKERS):
            return False, f"CUDA 实测失败，疑似算力/运行库不匹配：{msg[:200]}"
        return False, f"CUDA 实测失败：{msg[:200]}"


def _ensure_probe_model() -> str:
    """返回一个极小的 CTranslate2 Whisper 模型路径，用于能力探测。

    直接复用 faster-whisper 的下载缓存；拿不到就放弃探测（返回 CPU 判断）。
    """
    from faster_whisper.utils import download_model  # type: ignore

    return download_model("tiny", local_files_only=True)


class WhisperEngine:
    """faster-whisper 的封装，模型只加载一次。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._model: Any = None
        self._lock = threading.Lock()
        self.device: str = ""
        self.compute_type: str = ""

    # ------------------------------------------------------------------
    def _load_model(self) -> Any:
        """懒加载模型（首次会下载权重到 data/models）。"""
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is not None:
                return self._model

            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise TranscriptionError(
                    "未安装 faster-whisper。请执行："
                    "pip install faster-whisper"
                ) from exc

            device, compute_type = detect_device(self.settings.whisper_device)
            self.device = device
            self.compute_type = compute_type

            model_dir = self.settings.models_dir
            model_dir.mkdir(parents=True, exist_ok=True)
            logger.info(
                "加载 Whisper 模型 %s（device=%s, compute_type=%s）",
                self.settings.whisper_model,
                device,
                compute_type,
            )

            try:
                self._model = WhisperModel(
                    self.settings.whisper_model,
                    device=device,
                    compute_type=compute_type,
                    download_root=str(model_dir),
                )
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                # GPU 加载失败时降级 CPU 再试一次，别让用户卡死
                if device == "cuda":
                    logger.warning("GPU 加载模型失败，改用 CPU 重试：%s", msg[:200])
                    try:
                        self._model = WhisperModel(
                            self.settings.whisper_model,
                            device="cpu",
                            compute_type="int8",
                            download_root=str(model_dir),
                        )
                        self.device, self.compute_type = "cpu", "int8"
                    except Exception as cpu_exc:  # noqa: BLE001
                        raise TranscriptionError(
                            f"加载 Whisper 模型失败。GPU 错误：{msg[:200]}；"
                            f"CPU 错误：{cpu_exc}"
                        ) from cpu_exc
                else:
                    raise TranscriptionError(
                        f"加载 Whisper 模型 {self.settings.whisper_model!r} 失败：{msg}"
                    ) from exc

            return self._model

    # ------------------------------------------------------------------
    def transcribe(self, audio_path: str | Path) -> Transcript:
        """转写音频文件为带时间戳的文本。"""
        path = Path(audio_path)
        if not path.exists():
            raise TranscriptionError(f"音频文件不存在：{path}")
        if path.stat().st_size == 0:
            raise TranscriptionError(f"音频文件为空：{path}")

        model = self._load_model()
        started = time.time()

        try:
            segments_iter, info = model.transcribe(
                str(path),
                language=self.settings.whisper_language or None,
                beam_size=self.settings.whisper_beam_size,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 500},
                condition_on_previous_text=False,  # 避免长视频里错误累积
                initial_prompt=self.settings.whisper_initial_prompt or None,
            )
            raw_segments = [
                TranscriptSegment(
                    start=float(seg.start),
                    end=float(seg.end),
                    text=(seg.text or "").strip(),
                )
                for seg in segments_iter
            ]
        except Exception as exc:  # noqa: BLE001
            raise TranscriptionError(f"Whisper 转写失败：{exc}") from exc

        elapsed = time.time() - started

        if self.settings.whisper_simplify_chinese:
            for seg in raw_segments:
                seg.text = to_simplified(seg.text)

        kept = [s for s in raw_segments if s.text.strip()]
        if not kept:
            raise TranscriptionError(
                "Whisper 没有识别出任何文字。可能是纯音乐/无人声视频，"
                "或音频下载不完整。"
            )

        duration = kept[-1].end if kept else 0.0
        speed = f"{duration / elapsed:.1f}x 实时" if elapsed > 0 and duration else "未知"
        return Transcript(
            segments=kept,
            source=TranscriptSource.WHISPER,
            language=getattr(info, "language", None) or self.settings.whisper_language,
            note=(
                f"模型 {self.settings.whisper_model}，设备 {self.device}"
                f"（{self.compute_type}），耗时 {elapsed:.1f}s（{speed}）"
            ),
        )

    # ------------------------------------------------------------------
    def unload(self) -> None:
        """释放模型占用的显存/内存。"""
        with self._lock:
            self._model = None
        try:
            import gc

            gc.collect()
        except Exception:  # noqa: BLE001
            pass

    def health(self) -> tuple[bool, str]:
        """不加载大模型，只报告设备可用性。"""
        try:
            device, compute_type = detect_device(self.settings.whisper_device)
        except TranscriptionError as exc:
            return False, str(exc)
        label = "GPU (CUDA)" if device == "cuda" else "CPU"
        return True, (
            f"转写将使用 {label}，模型 {self.settings.whisper_model}"
            f"（compute_type={compute_type}）"
        )
