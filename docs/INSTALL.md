# 安装与上手

面向**第一次在这台电脑上装 MyNoteApp 的人**。全程 Windows，命令都在 **PowerShell** 里执行。
如果你只是想给某一条视频做笔记，看「[四、开始用](#四开始用)」就够了。

> 已经装好、想直接跑的人：双击项目根目录的 `启动笔记.bat`。
>
> 想让生成的笔记直接进 Obsidian 库？看 **[docs/OBSIDIAN.md](OBSIDIAN.md)** ——
> 最快三步（把 `DATA_DIR` 指到库里的一个文件夹），也有「模型和音频留在库外」的干净接法。

---

## 一、你需要什么

| 项目 | 要求 | 说明 |
|---|---|---|
| 操作系统 | Windows 10 / 11（x64） | 目前只在 Windows 实测过；代码里没有硬编码盘符，其他系统需要自己踩 |
| Python | **3.11 ~ 3.13** | 3.10 及以下不行（用了 `X \| None` 语法） |
| 显卡 | 可选：NVIDIA（驱动支持 CUDA 12.8+） | 没有显卡也能用，自动回落到 CPU 转写，慢 10~20 倍 |
| 磁盘 | 约 **6~10 GB** | Python 依赖 ~1.5 GB + Whisper 模型 ~1.6 GB + 视频缓存 |
| 内存 | 8 GB 起 | 转写和抽帧都吃内存，16 GB 更稳 |
| ffmpeg | **不用自己装** | 依赖里的 `imageio-ffmpeg` 自带一份可执行文件 |
| 本地模型 | [Ollama](https://ollama.com/download) + `qwen3:8b` | 生成笔记用；**默认数据不出本机** |

---

## 二、复制项目 + 装依赖

```powershell
git clone https://github.com/yn1101ny-cmyk/bili2obsidian.git
cd bili2obsidian

# 1) 建虚拟环境（只用这个 venv，不要装到系统 Python）
python -m venv .venv

# 2) 基础依赖（必装）
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 3) 画面识别：教程类视频的提示词/代码只出现在画面上，装上才能记下来（推荐）
.\.venv\Scripts\python.exe -m pip install -r requirements-vision.txt

# 4) 只有 NVIDIA 显卡才装（约 1.3 GB；纯 CPU 跳过）
.\.venv\Scripts\python.exe -m pip install -r requirements-cuda.txt
```

没有 git 的话，在 GitHub 页面点 **Code → Download ZIP**，解压后从 `cd` 那一步继续。

---

## 三、配置（可以全部跳过，所有配置都有默认值）

```powershell
Copy-Item .env.example .env
```

`.env` 已被 `.gitignore` 排除，**不会进版本库**。按需改这几项：

### 1. 本地模型（默认值就能用）

```powershell
ollama pull qwen3:8b
```

Ollama registry 在国内常被阻断。`ollama pull` 超时就改用镜像导入：

```powershell
.\.venv\Scripts\python.exe -m scripts.fetch_gguf
```

### 2. 国内网络的两个必设项

```dotenv
HF_ENDPOINT=https://hf-mirror.com
HF_DISABLE_XET=true
```

直连 `huggingface.co` 时主站能打开、**文件 CDN 被阻断**，模型权重下不下来；换镜像后实测 ~2.5 MB/s。

### 3. B站登录态（可选）

不配也能用：拿不到字幕就自动改用本地 Whisper 转写，只是每个视频都要转一次。
想要官方字幕（含 AI 字幕）就配：

1. 浏览器登录 B站 → `F12` → Application → Cookies → `https://www.bilibili.com` → 复制 `SESSDATA` 的值；
2. 写进 `.env`：`BILI_SESSDATA=你复制到的值`。

> ⚠️ SESSDATA **等价于登录凭证**，泄露即可免密登录你的账号。建议用小号；本应用只把它当请求头发送，不写日志、不写笔记。
> 有效期约 1 个月，过期后字幕会重新变成「拿不到」，删掉这一行就回到 Whisper 兜底。

### 4. 云端模型（默认关闭）

把字幕全文发给第三方才需要，**必须同时**设置 `LLM_PROVIDER=openai` 与 `ALLOW_CLOUD=true` 才会生效。

---

## 四、开始用

### 1. 先自检

```powershell
.\.venv\Scripts\python.exe -m scripts.doctor        # 机器可读：--json；不联网：--offline
```

会告诉你：Python / ffmpeg / 显卡 / CTranslate2 / Ollama / 模型 是否就绪，缺什么补什么。

### 2. 打开界面

```powershell
.\启动笔记.bat
```

或者：

```powershell
.\.venv\Scripts\python.exe -m app
```

浏览器打开 <http://127.0.0.1:8765>（只监听回环地址，局域网访问不到；把 `HOST` 改成 `0.0.0.0` 会被直接拒绝启动）。

### 3. 或者完全不用界面

```powershell
# 贴链接，跑完整链路，产出一篇 Markdown 笔记
.\.venv\Scripts\python.exe -m scripts.mynote "https://www.bilibili.com/video/BVxxxxxxxxx"

# 只验证「下载 + 转写」这一段，排查问题时用
.\.venv\Scripts\python.exe -m scripts.smoke_transcribe "https://www.bilibili.com/video/BVxxxxxxxxx"
```

笔记默认落在 `data/notes/`（Markdown），索引和数据在 `data/`（SQLite + 文件）。

---

## 五、进阶：批量拿转写稿和关键画面

这两个脚本原本是给 AI 助手（Claude Code / DSH 之类）批量干活的，也可以手动用。

```powershell
# 转写稿：优先取 B站官方字幕（要配 SESSDATA），拿不到才回落本地 GPU Whisper
.\.venv\Scripts\python.exe scripts\v_extract.py "BV号或链接" --outdir D:\某处

# 多个视频 / 多P 一起跑
.\.venv\Scripts\python.exe scripts\v_extract.py "BV1xxx" "BV2yyy" --pages 1-3

# 关键画面截图：复用画面识别已经抽好的帧，几秒出图，不重新抽帧、不重新 OCR
.\.venv\Scripts\python.exe scripts\v_shots.py BVxxxxxxxxx --outdir D:\某处
```

- `v_extract.py` 产出 `<tag>.txt`（带时间戳的转写稿）、`<tag>.onscreen.txt`（画面文字）、`<tag>.meta.json`（元数据 + 来源）。多P 时 `<tag>` 为 `BV号_p2` 这种形式。
- 常用开关：`--no-subs`（跳过字幕、直接 Whisper）、`--no-audio`、`--no-vision`、`--force`、`--max-height 480`。
- 两个脚本都必须在**本项目的 venv** 里跑（要 import `app` 包），但可以从任意目录调用。

---

## 六、卸载 / 清数据

| 想清掉什么 | 怎么做 |
|---|---|
| 生成的笔记和数据库 | 界面里的「一键清除数据」，或直接删 `data/` |
| 视频帧缓存（占地最大） | 删 `data/cache/`，下次跑会重新下 |
| Whisper 模型 | 删 HuggingFace 缓存目录（`%USERPROFILE%\.cache\huggingface`） |
| 整个应用 | 删项目目录 + `.venv`，再把 Ollama 里不用的模型 `ollama rm qwen3:8b` |

---

## 七、常见问题

**装 CUDA 依赖后仍然报 `no kernel image is available`**
你的显卡比 CUDA 运行时新（比如 RTX 50 系 / Blackwell）。装 `requirements-cuda.txt`，它锁的是 CUDA 12.8+ / cuDNN 9；
`WHISPER_COMPUTE_TYPE` 保持 `float16`，**不要改成 `auto`**（auto 会选 int8，在 Blackwell 上直接 `CUBLAS_STATUS_NOT_SUPPORTED`）。

**模型权重下载卡在 0 字节**
`.env` 里加上 `HF_ENDPOINT=https://hf-mirror.com` 与 `HF_DISABLE_XET=true`。

**提示「该视频没有可用字幕」**
没配 SESSDATA（B站字幕必须登录，WBI 签名绕不过），或这个视频确实没有字幕。都会自动回落到 Whisper，不影响出笔记。

**OCR 很慢**
`VISION_OCR_THREADS` 别填满逻辑核。实测在 8P+16E 的混合架构上，24 线程比 8 线程**慢 3.5 倍**（2.88s/帧 vs 0.82s/帧）。

**端口被占用**
`.env` 里改 `PORT`。

**生成笔记时把 `git status` 听成了 `Gate status`**
这就是画面识别存在的理由：音频听错的命令，画面里是对的。装上 `requirements-vision.txt` 并保持 `VISION_ENABLED=true`。

---

## 八、隐私

- 默认只用**本机 Ollama**，音频、字幕、笔记都不离开这台电脑。
- 不联网就装不了依赖、下不了模型；运行时除 B站请求和（首次）模型下载外没有其他外发流量。
- 本地 Web 界面只绑定 `127.0.0.1`，不会暴露到局域网。
- 云端模型默认关闭，开启需要你在 `.env` 里显式接受「字幕全文会发给第三方」。

细节见 [README 的「数据安全」](../README.md#数据安全) 与 [docs/NETWORK.md](NETWORK.md)（完整的出网清单）。
