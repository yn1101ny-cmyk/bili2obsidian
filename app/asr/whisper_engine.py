"""本地语音转写 —— 基于 faster-whisper。

**隐私**：模型首次使用时从 HuggingFace 下载到 ``data/models``，
之后完全离线推理。转写过程不出本机。

本文件集中处理了 Blackwell（RTX 50 系, sm_120）上的四个已知坑，
每一条都有实证来源（见 ``docs/research/``）：

1. **``compute_type="auto"`` 会崩**
   auto 会选中 ``int8_float16``，而 sm_120 上一度缺少 int8 的 IMMA kernel，
   直接抛 ``CUBLAS_STATUS_NOT_SUPPORTED``。
   → 本模块**永不使用 auto**，GPU 一律显式 ``float16``。

2. **``word_timestamps=True`` 必崩**
   Whisper 多语言词表 51865 不是 4 的倍数，踩到 sm_120 被移除的 kernel。
   → 本模块强制 ``word_timestamps=False``（我们只需要句级时间戳）。

3. **Windows 上找不到 CUDA DLL**
   pip 装的 ``nvidia-cublas-cu12`` / ``nvidia-cudnn-cu12`` 里的 bin 目录
   不在默认搜索路径，需要 ``os.add_dll_directory``；且必须是
   **cuDNN 9 + CUDA 12.8**（cuDNN 8 与 CUDA 13 都不行）。

4. **GPU 失败不会自动降级**
   有些环境 GPU 初始化失败但不报错到上层。→ 主动探测 + 加载失败重试 CPU。
"""

from __future__ import annotations

import inspect
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from app.asr.model_store import (
    ensure_model,
    is_model_ready,
    local_model_dir,
)
from app.config import Settings
from app.errors import TranscriptionError
from app.schemas import Transcript, TranscriptSegment, TranscriptSource

logger = logging.getLogger(__name__)

#: GPU 路径不可用时应当降级的错误特征
_GPU_FALLBACK_MARKERS = (
    "no kernel image is available",
    "CUBLAS_STATUS_NOT_SUPPORTED",
    "cudaErrorNoKernelImageForDevice",
    "cublas64_",
    "cudnn64_",
    "Library cublas",
    "Library cudnn",
    "Could not locate cudnn",
    "CUDA driver version is insufficient",
    "CUDA failed with error",
    "out of memory",
    "device kernel image is invalid",
)

#: 这些参数在部分 faster-whisper 版本上可能不存在，运行时按签名过滤
_CANDIDATE_PARAMS: dict[str, Any] = {
    "beam_size": 5,
    "best_of": 5,
    "patience": 1.0,
    "length_penalty": 1.0,
    # 温度回退链：贪心失败时逐级升温重试，抑制复读
    "temperature": [0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
    "compression_ratio_threshold": 2.4,
    "log_prob_threshold": -1.0,
    "no_speech_threshold": 0.6,
    # 中文长音频里开启它会显著加剧「复读机」现象
    "condition_on_previous_text": False,
    "prompt_reset_on_temperature": 0.5,
    "repetition_penalty": 1.05,
    "no_repeat_ngram_size": 0,
    "word_timestamps": False,  # sm_120 上打开必崩
    "without_timestamps": False,
    "chunk_length": 30,
    "vad_filter": True,
    "vad_parameters": {
        "threshold": 0.5,
        "min_speech_duration_ms": 250,
        # 默认是 2000ms，中文口语停顿短，必须降下来否则会吞掉整句
        "min_silence_duration_ms": 500,
        "speech_pad_ms": 200,
    },
}

#: 简体转换器（可选依赖）
_t2s_converter: Any = None
_t2s_lock = threading.Lock()
_t2s_tried = False


# ---------------------------------------------------------------------------
# 繁 -> 简
# ---------------------------------------------------------------------------


def to_simplified(text: str) -> str:
    """把繁体中文转成简体。

    Whisper 的中文训练语料简繁混杂，``language="zh"`` 不区分字形，
    因此**提示词只能引导、不能保证**。OpenCC 的 t2s 是确定性兜底。
    未安装 opencc 时原样返回（此时只能依赖 initial_prompt 引导）。
    """
    global _t2s_converter, _t2s_tried
    if not text:
        return text
    if not _t2s_tried:
        with _t2s_lock:
            if not _t2s_tried:
                try:
                    from opencc import OpenCC  # type: ignore

                    _t2s_converter = OpenCC("t2s")
                    logger.info("已启用 OpenCC 繁转简")
                except Exception as exc:  # noqa: BLE001 - 可选依赖
                    logger.warning(
                        "未安装 opencc，中文可能输出繁体。"
                        "建议执行：pip install opencc-python-reimplemented（%s）",
                        exc,
                    )
                    _t2s_converter = None
                _t2s_tried = True
    if _t2s_converter is None:
        return text
    try:
        return _t2s_converter.convert(text)
    except Exception:  # noqa: BLE001 - 转换失败不该影响主流程
        return text


# ---------------------------------------------------------------------------
# 模型权重下载（HuggingFace 镜像）
# ---------------------------------------------------------------------------


def apply_hf_env(settings: Settings) -> str:
    """在导入 faster-whisper / huggingface_hub **之前** 设置下载相关环境变量。

    ⚠️ 调用时机很关键：``huggingface_hub`` 只在**模块导入时**读取一次
    ``HF_ENDPOINT``，之后再改环境变量就无效了。所以必须在
    ``from faster_whisper import WhisperModel`` 之前调用。

    为什么需要这些设置（均为本机实测）：

    * 国内 **主站可达但文件 CDN 被阻断** —— huggingface.co 首页 179ms 正常，
      下载 ``model.bin`` 却直接超时；换成 hf-mirror.com 后 2.5 MB/s。
    * **Xet 传输协议会卡死** —— 开着它下载停在 0 字节数分钟毫无进展，
      关掉后立刻正常。

    :returns: 最终生效的 HuggingFace 端点
    """
    if settings.hf_endpoint:
        os.environ["HF_ENDPOINT"] = settings.hf_endpoint
    if settings.hf_disable_xet:
        # setdefault：允许用户在外部显式改回来
        os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    # 关掉一堆噪音警告（Windows 无符号链接权限等）
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    endpoint = os.environ.get("HF_ENDPOINT", "https://huggingface.co")
    logger.debug("HuggingFace 端点：%s", endpoint)
    return endpoint


def download_hint() -> str:
    """模型下载失败时给出的可执行建议。"""
    endpoint = os.environ.get("HF_ENDPOINT", "https://huggingface.co")
    if endpoint.rstrip("/") != "https://huggingface.co":
        return f"（当前 HuggingFace 端点：{endpoint}，若仍失败可换 https://hf-mirror.com）"
    return (
        "如果是下载卡住或超时：国内直连 HuggingFace 的文件 CDN 常被阻断"
        "（主站能打开也不算数，实测下载 model.bin 会直接超时）。\n"
        "请在 .env 中设置：HF_ENDPOINT=https://hf-mirror.com 后重试。"
    )


# ---------------------------------------------------------------------------
# Windows CUDA DLL
# ---------------------------------------------------------------------------


def setup_cuda_dll_paths() -> list[str]:
    """把 CUDA / cuDNN 运行库目录加入 Windows DLL 搜索路径。

    依次查找：项目 venv 的 site-packages/nvidia/*/bin、``CUDA_PATH``、
    ``CUDNN_PATH``、以及 NVIDIA 默认安装目录。

    :returns: 实际生效的目录列表（供自检展示）
    """
    added: list[str] = []
    if sys.platform != "win32":
        return added

    candidates: list[Path] = []

    # 1. pip 安装的 nvidia-*-cu12 包
    try:
        import site

        roots: list[str] = []
        try:
            roots.extend(site.getsitepackages())
        except Exception:  # noqa: BLE001
            pass
        user_site = site.getusersitepackages()
        if user_site:
            roots.append(user_site)
        roots.append(str(Path(sys.prefix) / "Lib" / "site-packages"))

        for root in roots:
            nvidia = Path(root) / "nvidia"
            if nvidia.is_dir():
                candidates.extend(sorted(nvidia.glob("*/bin")))
    except Exception as exc:  # noqa: BLE001
        logger.debug("枚举 site-packages 失败：%s", exc)

    # 2. 环境变量
    for var in ("CUDA_PATH", "CUDNN_PATH"):
        value = os.environ.get(var)
        if value:
            base = Path(value)
            candidates.append(base / "bin")
            candidates.append(base / "lib" / "x64")

    # 3. NVIDIA 默认安装位置
    for pattern in (
        r"C:\Program Files\NVIDIA\CUDNN\*\bin",
        r"C:\Program Files\NVIDIA\CUDNN\*\bin\*\x64",
        r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.*\bin",
    ):
        try:
            candidates.extend(Path("C:/").glob(pattern[3:]))
        except Exception:  # noqa: BLE001
            continue

    seen: set[str] = set()
    for path in candidates:
        try:
            if not path.is_dir():
                continue
        except OSError:
            continue
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            os.add_dll_directory(str(path))
        except (OSError, AttributeError) as exc:
            logger.debug("add_dll_directory 失败 %s：%s", path, exc)
            continue
        os.environ["PATH"] = str(path) + os.pathsep + os.environ.get("PATH", "")
        added.append(str(path))

    if added:
        logger.info("已注册 %d 个 CUDA 运行库目录", len(added))
    return added


# ---------------------------------------------------------------------------
# 设备与精度
# ---------------------------------------------------------------------------


def supported_compute_types(device: str = "cuda") -> set[str]:
    """问 CTranslate2 这个设备支持哪些精度。

    比「能不能 import torch」靠谱得多 —— 它直接反映编译进 wheel 的 kernel。
    """
    try:
        import ctranslate2
    except ImportError:
        return set()
    try:
        return set(ctranslate2.get_supported_compute_types(device))
    except Exception as exc:  # noqa: BLE001
        logger.debug("查询 %s 支持的精度失败：%s", device, exc)
        return set()


def resolve_compute_type(preference: str, device: str) -> str:
    """决定 compute_type。**关键：绝不对 GPU 使用 auto。**

    auto 会选 int8_float16，而 sm_120 上 int8 路径曾长期不可用。
    """
    if device == "cpu":
        # CPU 上 int8 是性价比最高的选择，且没有 sm_120 的问题
        if preference in ("auto", "int8", "int8_float16"):
            return "int8"
        if preference == "float16":
            logger.warning("CPU 不支持 float16，改用 int8")
            return "int8"
        return preference

    # ---- GPU ----
    available = supported_compute_types("cuda")
    if preference == "auto":
        if available and "float16" not in available:
            logger.warning("CUDA 不支持 float16（支持：%s）", sorted(available))
        return "float16"

    if preference.startswith("int8"):
        if available and preference not in available and "int8_float16" not in available:
            logger.warning(
                "CUDA 不支持 %s（支持：%s），改用 float16。"
                "int8 在 Blackwell 上需要 ctranslate2>=4.7.0。",
                preference,
                sorted(available),
            )
            return "float16"
        logger.warning(
            "显式选择了 %s。注意：int8 系在部分 Blackwell 版本上会抛 "
            "CUBLAS_STATUS_NOT_SUPPORTED，若报错请改用 float16。",
            preference,
        )
    return preference


def probe_cuda() -> tuple[bool, str]:
    """判断 GPU 路径是否真的可用。

    用 ``get_supported_compute_types`` 而不是加载模型 —— 快、且直接反映
    wheel 里编译了哪些 kernel。
    """
    setup_cuda_dll_paths()
    try:
        import ctranslate2
    except ImportError as exc:
        return False, f"未安装 ctranslate2（{exc}）"

    try:
        count = ctranslate2.get_cuda_device_count()
    except Exception as exc:  # noqa: BLE001
        return False, f"查询 CUDA 设备失败：{exc}"

    if count <= 0:
        return False, "未检测到可用 CUDA 设备（驱动未装或被占用）"

    types = supported_compute_types("cuda")
    if not types:
        return False, (
            f"检测到 {count} 个 CUDA 设备，但 CTranslate2 报告不支持任何精度。"
            "通常是 CUDA 运行库缺失（需 nvidia-cublas-cu12 / nvidia-cudnn-cu12）"
        )
    if "float16" not in types:
        return False, f"CUDA 不支持 float16（仅支持 {sorted(types)}）"

    version = getattr(ctranslate2, "__version__", "未知")
    return True, f"CUDA 可用（{count} 个设备，ctranslate2 {version}，支持 float16）"


def detect_device(preference: str = "auto") -> tuple[str, str]:
    """决定 ``(device, compute_type)``。"""
    if preference == "cpu":
        return "cpu", "int8"

    ok, reason = probe_cuda()
    if ok:
        return "cuda", resolve_compute_type("auto", "cuda")

    if preference == "cuda":
        raise TranscriptionError(
            f"配置要求使用 CUDA，但 GPU 不可用：{reason}\n"
            "排查建议：\n"
            "  1) nvidia-smi 能否正常输出（驱动是否安装）；\n"
            "  2) 安装 CUDA 12.8 运行库：\n"
            "     pip install \"nvidia-cublas-cu12>=12.8.4.1\" "
            "\"nvidia-cudnn-cu12>=9.8,<10\"\n"
            "  3) 若报 no kernel image / CUBLAS_STATUS_NOT_SUPPORTED：\n"
            "     pip install -U \"ctranslate2>=4.6.3\" faster-whisper\n"
            "  4) 也可以把 WHISPER_DEVICE 设为 cpu 先用起来。"
        )

    logger.warning("GPU 不可用（%s），降级到 CPU 转写", reason)
    return "cpu", "int8"


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------


class WhisperEngine:
    """faster-whisper 封装。模型懒加载且只加载一次。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._model: Any = None
        self._batched: Any = None
        self._lock = threading.Lock()
        self.device: str = ""
        self.compute_type: str = ""
        #: 模型下载进度回调 ``(已下载字节, 总字节, 当前文件)``。
        #: 由上层（流水线）挂上，用来把「正在下载 1.5GB 模型」显示给用户。
        self.on_download_progress: Any = None

    # ------------------------------------------------------------------
    def _load_model(self) -> Any:
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is not None:
                return self._model

            # ⚠️ 必须在导入 faster_whisper 之前设置镜像环境变量
            endpoint = apply_hf_env(self.settings)

            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise TranscriptionError(
                    "未安装 faster-whisper。请执行：pip install faster-whisper"
                ) from exc

            device, compute_type = detect_device(self.settings.whisper_device)
            self.device, self.compute_type = device, compute_type

            model_dir = self.settings.models_dir
            model_dir.mkdir(parents=True, exist_ok=True)

            # 先把权重下到可续传的本地目录，再交给 faster-whisper。
            # 直接让 faster-whisper 自己下的话既不能续传也没有进度，
            # 在国内镜像上（0.3~3 MB/s 波动）1.5GB 几乎必断。
            local_path = self._prepare_weights(model_dir)

            logger.info(
                "加载 Whisper 模型 %s（device=%s, compute_type=%s）",
                local_path,
                device,
                compute_type,
            )

            try:
                self._model = WhisperModel(
                    local_path,
                    device=device,
                    compute_type=compute_type,
                    download_root=str(model_dir),
                )
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                if device == "cpu":
                    raise TranscriptionError(
                        f"加载 Whisper 模型 {self.settings.whisper_model!r} 失败：{msg}\n"
                        f"{download_hint()}"
                    ) from exc
                # GPU 加载失败 -> 退回 CPU，别让用户卡死
                logger.warning("GPU 加载模型失败，改用 CPU 重试：%s", msg[:200])
                try:
                    self._model = WhisperModel(
                        local_path,
                        device="cpu",
                        compute_type="int8",
                        download_root=str(model_dir),
                    )
                    self.device, self.compute_type = "cpu", "int8"
                except Exception as cpu_exc:  # noqa: BLE001
                    raise TranscriptionError(
                        f"加载 Whisper 模型失败。\nGPU 错误：{msg[:300]}\n"
                        f"CPU 错误：{cpu_exc}\n{download_hint()}"
                    ) from cpu_exc

            self._batched = self._make_batched(self._model)
            return self._model

    def _prepare_weights(self, model_dir: Path) -> str:
        """准备模型权重路径，返回可交给 WhisperModel 的字符串。

        * 若配置的是已存在的本地目录，直接用；
        * 否则按别名 / ``owner/repo`` 下载到本机（可续传）。
        """
        configured = self.settings.whisper_model
        as_path = Path(configured)
        if as_path.is_dir():
            logger.info("使用本地模型目录：%s", as_path)
            return str(as_path)

        if is_model_ready(self.settings, configured):
            return str(local_model_dir(self.settings, configured))

        logger.info("模型权重不在本机，开始下载（支持断点续传）…")
        return str(
            ensure_model(
                self.settings,
                configured,
                on_progress=self.on_download_progress,
            )
        )

    def _make_batched(self, model: Any) -> Any:
        """尝试构造批处理推理管道（可提速 3-5 倍）。失败则返回 None。"""
        if not self.settings.whisper_use_batched:
            return None
        try:
            from faster_whisper import BatchedInferencePipeline

            pipeline = BatchedInferencePipeline(model=model)
            logger.info("已启用批处理推理（batch_size=%d）", self.settings.whisper_batch_size)
            return pipeline
        except Exception as exc:  # noqa: BLE001 - 老版本没有这个类
            logger.info("批处理推理不可用，改用逐段转写：%s", exc)
            return None

    # ------------------------------------------------------------------
    def _build_kwargs(self, *, batched: bool) -> dict[str, Any]:
        """按目标函数的真实签名筛出可用参数。

        不同 faster-whisper 版本的参数集有差异（如 repetition_penalty、
        no_repeat_ngram_size 是否支持），按签名过滤比硬编码更稳。
        """
        target = self._batched.transcribe if batched else self._model.transcribe
        try:
            allowed = set(inspect.signature(target).parameters)
        except (TypeError, ValueError):
            allowed = set(_CANDIDATE_PARAMS)

        kwargs: dict[str, Any] = {
            k: v for k, v in _CANDIDATE_PARAMS.items() if k in allowed
        }

        kwargs["language"] = self.settings.whisper_language or None
        kwargs["initial_prompt"] = self.settings.whisper_initial_prompt or None
        if "beam_size" in allowed:
            kwargs["beam_size"] = self.settings.whisper_beam_size
        if batched and "batch_size" in allowed:
            kwargs["batch_size"] = self.settings.whisper_batch_size
        return kwargs

    # ------------------------------------------------------------------
    def transcribe(self, audio_path: str | Path) -> Transcript:
        """把音频文件转写成带时间戳的文本。"""
        path = Path(audio_path)
        if not path.exists():
            raise TranscriptionError(f"音频文件不存在：{path}")
        if path.stat().st_size == 0:
            raise TranscriptionError(f"音频文件为空：{path}")

        model = self._load_model()
        started = time.time()

        segments_iter, info = self._run(path)
        raw_segments = [
            TranscriptSegment(
                start=float(getattr(seg, "start", 0.0)),
                end=float(getattr(seg, "end", 0.0)),
                text=(getattr(seg, "text", "") or "").strip(),
            )
            for seg in segments_iter
        ]
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

        duration = kept[-1].end
        speed = f"{duration / elapsed:.1f}x 实时" if elapsed > 0 and duration else "未知"
        mode = "批处理" if self._batched is not None else "逐段"
        return Transcript(
            segments=kept,
            source=TranscriptSource.WHISPER,
            language=(getattr(info, "language", None) or self.settings.whisper_language),
            note=(
                f"模型 {self.settings.whisper_model}，设备 {self.device}"
                f"（{self.compute_type}，{mode}），耗时 {elapsed:.1f}s（{speed}）"
            ),
        )

    def _run(self, path: Path) -> tuple[Any, Any]:
        """执行转写，批处理失败时自动退回逐段。"""
        attempts: list[tuple[str, bool]] = []
        if self._batched is not None:
            attempts.append(("批处理", True))
        attempts.append(("逐段", False))

        last_error: Exception | None = None
        for label, batched in attempts:
            target = self._batched if batched else self._model
            kwargs = self._build_kwargs(batched=batched)
            try:
                logger.info("开始转写（%s模式）…", label)
                return target.transcribe(str(path), **kwargs)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                text = str(exc)
                hint = ""
                if any(m in text for m in _GPU_FALLBACK_MARKERS):
                    hint = (
                        "\n疑似 GPU 算力/运行库不匹配。可尝试把 WHISPER_DEVICE "
                        "设为 cpu，或升级 ctranslate2>=4.6.3。"
                    )
                logger.warning("%s模式转写失败：%s%s", label, text[:300], hint)
                continue

        raise TranscriptionError(
            f"Whisper 转写失败：{last_error}"
        ) from last_error

    # ------------------------------------------------------------------
    def unload(self) -> None:
        """释放模型占用的显存。"""
        with self._lock:
            self._model = None
            self._batched = None
        try:
            import gc

            gc.collect()
        except Exception:  # noqa: BLE001
            pass

    def health(self) -> tuple[bool, str]:
        """不加载大模型，只报告设备与依赖状态。"""
        try:
            from opencc import OpenCC  # noqa: F401

            opencc_ok = "已装 OpenCC"
        except ImportError:
            opencc_ok = "未装 OpenCC（中文可能输出繁体）"

        try:
            device, compute_type = detect_device(self.settings.whisper_device)
        except TranscriptionError as exc:
            return False, str(exc)

        label = "GPU (CUDA)" if device == "cuda" else "CPU"
        cached = weights_ready(self.settings)
        endpoint = self.settings.hf_endpoint or os.environ.get(
            "HF_ENDPOINT", "https://huggingface.co"
        )
        return True, (
            f"{label} 转写就绪，模型 {self.settings.whisper_model}"
            f"（compute_type={compute_type}），{opencc_ok}；"
            + (
                "模型已下载到本机"
                if cached
                else f"首次运行时会从 {endpoint} 下载模型权重"
            )
        )


def weights_ready(settings: Settings) -> bool:
    """模型权重是否已在本机（或配置的就是本地目录）。"""
    configured = settings.whisper_model
    try:
        if Path(configured).is_dir():
            return True
        return is_model_ready(settings, configured)
    except TranscriptionError:
        return False
