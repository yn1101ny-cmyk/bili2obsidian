# 更新日志

本项目的所有重要改动都记在这里。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## [未发布]

### 新增

- **高赞评论勘误**：从评论区按热度抓前 N 条（`BILI_TOP_COMMENTS`，默认 20，设 0 关闭），去掉纯表情、重复和带链接的引流评论后，写进笔记末尾的「💬 高赞评论（观众补充与勘误）」，并作为勘误线索进提示词（模型会把观众指出的错误记进存疑/要点，并注明来自评论）。评论接口失败不影响出笔记。
- **本地出稿 + 云端单次润色**：本地 Ollama 照常生成完整初稿（0 元），成稿后把笔记 JSON 交给云端只改一次表达（不是整篇转写），一篇通常一两分钱。开法：`POLISH_ENABLED=true` + `ALLOW_CLOUD=true` + 云端 key；编辑偏好写在 `POLISH_INSTRUCTIONS`。润色稿要过硬校验（时间戳、`[mm:ss]`、代码原文、篇幅 60%），任何一条不过就保留本地稿。
- **给 AI 的部署文档**：[docs/DEPLOY-WITH-AI.md](docs/DEPLOY-WITH-AI.md)，内含可直接粘贴的部署提示词、安装清单和验收标准。

### 修复

- **画面识别偶发拿不到视频**：这一步下载视频时，B站 会间歇性返回空格式表（显式浏览器指纹那一路尤其明显，报 `No video formats found!`），原来两路都失败就静默降级、少了画面文字。现在整轮会重试一次（间隔 2 秒）；另外格式匹配补了 `width<=480` 兜底，竖屏视频（高 > 宽）也能选到片源。
- **长视频笔记被截断**：`LLM_NUM_PREDICT` 默认从 4096 提到 8192。实测一条 6 分半的视频，模型写到 `actionables` 中途就撞上限，两次都拿不到合法 JSON；给足余量后能完整输出。
- **Ollama 在跑却报 HTTP 502**：客户端原先会读 Windows 的「系统代理」，把 `http://127.0.0.1:11434` 也发给代理，代理拒绝本地目标、返回空的 502。现在访问本地 Ollama 一律绕过系统代理。
- **指向本机的 OpenAI 兼容接口同样绕过代理**：把 `OPENAI_BASE_URL`/`POLISH_BASE_URL` 设成 `http://127.0.0.1:11434/v1`（Ollama 的 /v1）或 LM Studio 时，之前也会撞上同样的空 body 502。现在按目标主机判断：本机地址不走代理，真云端仍走系统代理。

## [0.1.0] - 2026-10-16

第一个公开版本。

### 新增

- **完整链路**：贴一条 B站链接 → 解析 → 取元数据 → 试字幕 → 下音频 → 本地 Whisper 转写 → 分块 map-reduce 理解 → 结构化 Markdown 笔记落盘。
- **本地 Web 界面**（FastAPI + Jinja2，只监听 `127.0.0.1`）：贴链接、看进度、读笔记、一键清除数据。
- **B站官方字幕优先**：CC / AI 字幕直接取（需要 `SESSDATA`），拿不到才回落到本地转写；字幕全文与本地转写走同一条整理链路。
- **画面识别（OCR）**：抽帧 + 去重 + 只保留持续出现的画面文字。解决「音频把 `git status` 听成 `Gate status`，而画面里写对了」这类问题。
- **命令行入口**：`python -m scripts.mynote`、`smoke_transcribe`、`doctor`、`check_asr`、`fetch_gguf`。
- **批量脚本**：`scripts/v_extract.py`（字幕优先 + 多视频/多P 批量，产出转写稿与画面文字）、`scripts/v_shots.py`（从抽帧缓存里挑关键画面，不重抽帧、不重跑 OCR）。
- **环境自检**：Python / ffmpeg / CUDA / CTranslate2 / Ollama / 模型 逐项检查，缺什么直接说。

### 说明

- 默认只走**本机 Ollama**（`qwen3:8b`），音频、字幕、笔记都不离开本机；云端模型默认关闭，开启需要显式设置 `ALLOW_CLOUD=true`。
- 国内网络需要两个设置项（`HF_ENDPOINT`、`HF_DISABLE_XET`），见 [docs/INSTALL.md](docs/INSTALL.md)。
- 实测数据：20 分钟中文视频，GPU 转写 27~36 秒（33~45x 实时）；56 分钟长视频走分块 map-reduce 可正常产出。
