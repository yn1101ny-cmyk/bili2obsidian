# 让 AI 帮你装好这个项目

这份文档是给「**拿到仓库、但不太想一条条敲命令**」的人准备的：把下面的提示词整段贴给一个有终端权限的 AI（Claude Code、Cursor、Codex、DeepSeek 的 agent 模式都行），它就能把这套环境装起来。

先说清楚边界：**AI 能替你装依赖、拉模型、跑自检；它装不了显卡驱动、也替不了你登录 B 站和 Obsidian。** 需要你动手的地方会标出来。

---

## 一、要装哪些东西

| 东西 | 谁装 | 说明 |
|---|---|---|
| **Python 3.11 ~ 3.13** | AI 可以（winget） | 项目只测过 Windows x64 + Python 3.11/3.12/3.13 |
| **Git**（可选） | AI 可以 | 只想下 ZIP 的话不用装 |
| **项目依赖**（yt-dlp、faster-whisper、httpx…） | AI | `pip install -r requirements.txt` 一次装完，yt-dlp 在里面 |
| **ffmpeg** | 不用装 | `imageio-ffmpeg` 自带一份可执行文件 |
| **Ollama + qwen3:8b** | AI 可以，但**模型要下 5GB** | 唯一必须单独装的外部软件；不装就只能出转写和截图，出不了笔记 |
| **转写模型**（faster-whisper turbo） | 不用下，首次运行自动下载 | 约 1.6GB；国内先设 `HF_ENDPOINT=https://hf-mirror.com` |
| **画面识别**（RapidOCR） | AI，可选 | `pip install -r requirements-vision.txt`，模型 36MB 打包在 wheel 里，不联网 |
| **CUDA 加速** | AI，可选 | 有 NVIDIA 卡才装，`requirements-cuda.txt` 约 1.3GB |
| **B站 SESSDATA** | **你** | 登录后 F12 从 Cookie 里复制；不给也能用，只是每个视频都走本地转写 |
| **Obsidian + 笔记库** | **你** | 笔记默认写在 `data/notes`，要进库改 `DATA_DIR`（见 `docs/OBSIDIAN.md`） |
| **显卡驱动** | **你** | 和机型绑定，AI 只能告诉你装哪个版本 |

资源预期：硬盘 **6~10GB**（含模型）、内存 **8GB+**；装依赖 15~30 分钟（国内网络更久）。10 分钟视频，有 GPU 几分钟出稿，纯 CPU 可能 20 分钟以上。

---

## 二、直接贴给 AI 的提示词

> 下面这段整段复制。把 `<项目路径>` 换成仓库解压/克隆到的目录。

```text
请帮我在这台 Windows 电脑上部署一个 Python 项目，目标是能跑出第一篇视频笔记。

项目路径：<项目路径>
项目说明：README.md（先读它，再读 docs/INSTALL.md）

请按顺序做，每步做完把结果告诉我，出错就停下来解释原因，不要硬猜：

1. 检查 Python：`python --version`，需要 3.11/3.12/3.13。没有就用 winget 装
   Python 3.12，装完重开终端再验证。
2. 建虚拟环境并装依赖（在项目目录里）：
   - `python -m venv .venv`
   - `.\.venv\Scripts\python.exe -m pip install --upgrade pip`
   - `.\.venv\Scripts\python.exe -m pip install -r requirements.txt`
   - 如果这台机器有 NVIDIA 显卡，再装 `requirements-cuda.txt`；
     没有就跳过（跳过了只是慢，不影响功能）。
   - 想要画面文字（OCR），再装 `requirements-vision.txt`。
3. 装 Ollama 并拉模型：
   - 检查 `ollama --version`；没有就 `winget install Ollama.Ollama`，装完重开终端。
   - `ollama pull qwen3:8b`（约 5GB，慢是正常的）。
   - 验证：`ollama run qwen3:8b "说一句话"` 能回话即可，然后退出。
4. 配置：把 `.env.example` 复制成 `.env`，其余保持默认。
   如果在中国大陆，在 `.env` 里加一行 `HF_ENDPOINT=https://hf-mirror.com`。
5. 自检：`.\.venv\Scripts\python.exe -m scripts.doctor`
   把输出完整贴给我。要求结束时没有「失败」项；如果提示缺 Ollama 模型，
   回到第 3 步；如果提示缺 ffmpeg，告诉我，不要自己乱装。
6. 试跑一篇：`.\.venv\Scripts\python.exe -m scripts.mynote BV1GJ411x7h7`
   首次会下载转写模型，可能十几分钟。跑完告诉我笔记文件在哪、有没有报错。

约束：
- 不要修改项目源码；配置只改 .env。
- 不要自己装 CUDA 工具包或显卡驱动，只报告需要装什么。
- 不要动系统代理设置。如果 Ollama 报 HTTP 502，先看 docs/INSTALL.md 的常见问题。
- 每一步的命令输出保留最后 30 行贴给我，方便排查。
```

**B站登录（可选，你来做）**：浏览器登录 B 站 → F12 → Application/存储 → Cookie → 复制 `SESSDATA` 的值，写进 `.env` 的 `BILI_SESSDATA=...`。不配也行，只是拿不到官方 AI 字幕，转写会更慢。

---

## 三、装完的验收标准

跑这三条，都通过就算装好了：

```powershell
.\.venv\Scripts\python.exe -m scripts.doctor          # 环境自检
.\.venv\Scripts\python.exe -m scripts.mynote <任一B站链接>   # 出一篇笔记
.\启动笔记.bat                                         # 启动器能起来
```

- `scripts.doctor` 输出里没有「失败」；
- 跑完视频后 `data\notes\` 下出现一个 `.md`，开头有 `bvid`、`transcript_source`，正文有时间戳；
- `启动笔记.bat` 双击后浏览器能打开本地页面，并且**重复双击不会起第二个服务**。

`启动笔记.bat` 只检查、不安装：虚拟环境或依赖不在时，它会直接把该敲的命令打印出来，不会替你装。

---

## 四、几个必踩的坑（先告诉你，省得排查）

1. **Ollama 明明在跑，却报 HTTP 502、响应体是空的**：Windows 上开着系统代理（Clash/代理软件）时，httpx 会把 `127.0.0.1:11434` 也送进代理。仓库版本已经修了（本地请求绕开代理）；如果你用的是旧版本，升级即可。
2. **拉不到模型**：国内直连 HuggingFace 经常失败，先设 `HF_ENDPOINT=https://hf-mirror.com`；Ollama 模型拉不动就用 `scripts.fetch_gguf` 从镜像下。
3. **B 站下载偶发 `No video formats found!`**：多半是风控抖动，重试一次往往就过；项目里已经带了整轮重试和竖屏兜底。
4. **纯 CPU 很慢**：转写慢 10~20 倍是正常的，不是卡死。
5. **别把笔记写进 Obsidian 的 `.obsidian` 或点开头的目录**：Obsidian 会忽略它们。

---

## 五、想更省事的两条路

- **不装 Ollama，用云端模型**：`.env` 里 `LLM_PROVIDER=openai`、填 `OPENAI_BASE_URL`/`OPENAI_MODEL`/`OPENAI_API_KEY`，并显式设 `ALLOW_CLOUD=true`。效果通常更好，但每篇要花几毛钱。
- **本地出稿 + 云端润色**（省钱推荐）：本地 Ollama 照常生成（0 元），最后把成稿交给云端只改一遍表达。`.env` 里设 `POLISH_ENABLED=true`、`ALLOW_CLOUD=true`、填云端 key 即可；润色结果会被校验（时间戳、代码、长度不得缩水），不合格就保留本地稿。
