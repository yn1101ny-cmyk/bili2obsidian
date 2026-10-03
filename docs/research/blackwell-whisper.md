# Blackwell（RTX 50 系, sm_120）上跑 faster-whisper 的实证结论

本文件记录本项目选择转写方案时依赖的事实依据。
**每一条都标注了来源**；「已确认」与「不确定」严格区分。

> 本机环境：RTX 5060 Laptop（8GB, sm_120）、Intel Ultra 9 275HX（24 核）、
> 31.4GB RAM、Windows 11、Python 3.13.9。
> 实测安装的版本：`ctranslate2 4.8.2`、`faster-whisper 1.2.1`。

## 一、决定性结论：成败在 ctranslate2 版本

faster-whisper 的推理后端是 CTranslate2，它把 CUDA kernel 编译进 wheel。
Blackwell 是 compute capability 12.0（sm_120），旧 wheel 里没有对应 kernel。

| ctranslate2 版本 | 对 sm_120 的支持 |
|---|---|
| `< 4.6.2` | int8 系必崩 `CUBLAS_STATUS_NOT_SUPPORTED` |
| `4.6.2`（2025-12-05） | 首个显式处理 sm_120 的版本（直接禁用 sm_120 的 int8） |
| **`>= 4.6.3`**（2026-01-06） | ⭐ 首个用 **CUDA 12.8** 构建的版本 = 真正的 sm_120 支持 |
| `>= 4.7.0` | 通过 vocab 16 字节对齐 padding 重新启用 sm_120 的 int8 |

**证据**：`v4.6.2...v4.6.3` 的 `docker/Dockerfile` 差异 ——
`nvidia/cuda:12.4.1` → `nvidia/cuda:12.8.1`。而 CUDA 12.8 是 NVIDIA 官方
第一个支持 Blackwell 的 toolkit。
→ https://github.com/OpenNMT/CTranslate2/compare/v4.6.2...v4.6.3
→ https://developer.nvidia.com/blog/cuda-toolkit-12-8-delivers-nvidia-blackwell-support/

**本项目的做法**：`requirements.txt` 里钉死 `ctranslate2>=4.6.3`。

## 二、四个必须绕开的坑

### 坑 1：`compute_type="auto"` 会崩

`auto` 会选择 `int8_float16`，在 sm_120 上抛
`RuntimeError: cuBLAS failed with status CUBLAS_STATUS_NOT_SUPPORTED`。

**根因**：NVIDIA 在 sm120 上去掉了部分 IMMA kernel，当 `n % 4 != 0` 时
`cublasGemmEx` 失败；而 Whisper 多语言词表大小 51865/51866 不是 4 的倍数。

**做法**：`app/config.py` 的 `whisper_compute_type` 默认值就是 `float16`，
且 `app/asr/whisper_engine.py::resolve_compute_type()` **永不对 GPU 使用 auto**。
`tests/test_config.py` 里有专门的断言盯住这个默认值。
→ https://github.com/OpenNMT/CTranslate2/issues/1865

### 坑 2：`word_timestamps=True` 必崩

同一个根因。本项目只需要**句级**时间戳，所以强制
`word_timestamps=False`（写在 `_CANDIDATE_PARAMS` 里）。

### 坑 3：Windows 找不到 CUDA DLL

pip 装的 `nvidia-cublas-cu12` / `nvidia-cudnn-cu12` 里的 `bin` 目录
不在默认 DLL 搜索路径。Python 3.8+ 上**只改 PATH 不够**，必须
`os.add_dll_directory()`。

**版本要求**：**cuDNN 9 + CUDA 12.8+**。
cuDNN 8 与 CUDA 13 都不被支持（CTranslate2 4.5.0 起不再兼容 cuDNN 8；
CUDA 13 支持的 issue #1933 仍 open）。

⚠️ **陷阱**：opennmt.net 官方安装文档至今仍写 "cuDNN 8"，是过时内容，
以 PyPI README 为准。
→ https://github.com/SYSTRAN/faster-whisper/issues/1080
→ https://github.com/SYSTRAN/faster-whisper/issues/1230
→ https://github.com/OpenNMT/CTranslate2/issues/1933

**做法**：`setup_cuda_dll_paths()` 遍历 `site-packages/nvidia/*/bin`、
`CUDA_PATH`、`CUDNN_PATH` 与 NVIDIA 默认安装目录，逐个注册。

### 坑 4：GPU 失败不一定会自动降级

部分环境下 GPU 初始化失败不会抛到上层，结果是静默用错精度或直接出错。

**做法**：`probe_cuda()` 用 `ctranslate2.get_supported_compute_types("cuda")`
主动探测（快且直接反映 wheel 里的 kernel），加载模型失败时再退回 CPU 重试一次。

## 三、模型与精度选择（8GB 显存）

官方基准锚点（13 分钟音频、beam 5、RTX 3070 Ti 8GB、large-v2）：

| 配置 | 耗时 | 显存 |
|---|---|---|
| fp16 | 1m03s | 4525 MB |
| fp16 batch8 | 17s | 6090 MB |
| int8 | 59s | 2926 MB |
| int8 batch8 | 16s | 4500 MB |

**large-v3-turbo** 相比 large-v3：解码层从 32 层减到 4 层（809M vs 1550M），
官方说法是「速度快得多，代价是轻微的质量下降」。

实测速度差距：顺序 217s → 39s（约 5.6×）；批处理 29.7s → 11.5s（约 2.6×）。
准确率差距很小（WER 17.6% → 9.5% / 7.9% → 7.7%）。

**结论**：本项目默认 `large-v3-turbo` + `float16` + 批处理，
显存估算约 2.5-3GB（batch16 约 4-4.5GB），8GB 宽裕。

时间估算（每小时中文音频）：RTX 5060 Laptop + turbo fp16 + 批处理
约 **1.5-3 分钟**；CPU（24 核 int8）兜底约 9-18 分钟。

→ https://pypi.org/project/faster-whisper/
→ https://huggingface.co/Deepdml/faster-whisper-large-v3-turbo-ct2
→ https://github.com/SYSTRAN/faster-whisper/issues/1030

## 四、中文的两个专门处理

### 1. 繁体输出问题

Whisper 的中文训练语料简繁混杂，`language="zh"` **不区分字形**。

- **提示词只能引导，不能保证**。而且必须是**完整通顺的简体句子** ——
  实测单词/短语（如 `"听起来有点呆"`）完全无效。
- **OpenCC 的 `t2s` 是确定性兜底**。

**做法**：两者叠加。`whisper_initial_prompt` 默认
`"以下是普通话的句子，这是一段中文讲解录音。"`，
转写后再跑一遍 `OpenCC("t2s")`（未安装时安静跳过）。
→ https://github.com/SYSTRAN/faster-whisper/issues/521

### 2. 复读与静音幻觉

中文长音频开启 `condition_on_previous_text` 会显著加剧「复读机」现象。
VAD 的默认 `min_silence_duration_ms` 是 2000ms，对中文口语太长，
会吞掉正常停顿。

**做法**：`condition_on_previous_text=False`；
VAD `min_silence_duration_ms=500`；配 `temperature` 回退链与
`repetition_penalty`。

## 五、本项目采用的参数

见 `app/asr/whisper_engine.py` 的 `_CANDIDATE_PARAMS`。
参数按 `inspect.signature` 过滤后传入，因此不同 faster-whisper 版本
即使参数集有差异也不会报错。

## 六、仍不确定的事项

诚实标注，避免把猜测当事实：

- `repetition_penalty` 的确切起始支持版本未 100% 确认（已用签名过滤兜底）
- 8GB 下 large-v3 / medium / small 的显存为**线性外推估算**，非实测
- 上述耗时全部为估算，仅有 RTX 5090 的约 90× 与官方 CPU 基准是实测锚点
- Windows + CUDA 模型析构死锁（PR #2027，未合并）在 sm_120 上有报告，
  本项目通过「用完即 `unload()`」降低触发概率，但未做长期压测

## 七、备选方案（当前不采用）

| 方案 | 评价 |
|---|---|
| `openai-whisper` + torch cu128 | 能跑（torch 2.7.0+cu128 起含 sm_120），但更慢、显存更高，且需额外装 ffmpeg |
| `whisper.cpp` 自编译 CUDA | 运行期最稳、无 Python 依赖地狱，但需 VS2022 + CUDA 12.8 自行编译（10-30 分钟） |
| WhisperX | 需要词级时间戳/说话人分离时才值得，依赖约束更严 |
| whisper-timestamped | 基于 torch 版，无独特优势 |

**结论**：当前选 faster-whisper + ct2>=4.6.3 + float16。
若实测发现 GPU 路径不稳定，第一备选是 whisper.cpp。
