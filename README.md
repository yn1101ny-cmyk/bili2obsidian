# bili2obsidian

[![tests](https://github.com/yn1101ny-cmyk/bili2obsidian/actions/workflows/tests.yml/badge.svg)](https://github.com/yn1101ny-cmyk/bili2obsidian/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11~3.13-blue.svg)](pyproject.toml)

本地优先的「B站视频 → 可理解笔记 → Obsidian 知识库」工具（内部代号 MyNoteApp）。

把 B站视频链接丢进来，应用自动提取视频里的内容（官方字幕 / 本地语音转写 + 画面识别），
整理成结构化笔记，并**指出你还需要自己搞清楚什么**，让碎片化的知识在
更短的时间里被真正理解。

> **第一次在这台电脑上装？** 逐步说明见 **[docs/INSTALL.md](docs/INSTALL.md)**：
> Python 版本要求、Ollama、国内网络的两个必设项、B站字幕怎么配、常见报错怎么办。

---

## 当前状态

**已完成并实测验证**（Windows 11 + RTX 5060 Laptop 8GB）：

```
贴链接 → 解析 → 取元数据 → 试字幕 ─┬─ 有字幕 → 直接用
                                    └─ 无字幕 → 下音频 → GPU 转写
                                              ↓
                          分块理解 → 整合成笔记 → Markdown 落盘 + 入库
                                              ↓
                                       本地 Web 界面阅读
```

实测数据：20 分钟中文视频，GPU 转写 **27~36 秒（33~45x 实时）**，
输出简体中文；56 分钟长视频走分块 map-reduce 也能正常产出。

已实现：本地 Web 界面、链接解析、B站客户端、字幕解析、音频下载、
本地 Whisper 转写、笔记生成（map-reduce）、Markdown 渲染、
SQLite + 文件存储、一键清除数据、环境自检、命令行入口。

---

## 和同类工具的区别

「视频转笔记」已经有不少项目（[BiliNote](https://github.com/JefferyHcool/BiliNote)、
[video-to-notes](https://github.com/KIRVO-REPORTING/video-to-notes)、各类浏览器插件与 SaaS）。
这个项目挑的是另一条路：

| | 本项目的选择 |
|---|---|
| **成本** | 全本地：转写用本机 GPU，摘要用本机 Ollama。**零 API 费用**，也不把视频内容发给任何云服务 |
| **取材** | **字幕优先**：能拿到 B站官方字幕（人工或 AI）就直接用，拿不到才回落本地 Whisper —— 省时间也省算力 |
| **画面** | 除了语音，还跑**画面 OCR**（提示词、代码、配置单往往只出现在画面上），并挑出信息量最大的**关键截图**一起入库 |
| **产出** | 不是导出一份 Markdown 就完事：直接写进 Obsidian 库的指定主题文件夹，带 frontmatter、标签词表、`?t=` 时间戳跳转、双链与 MOC 主题地图 |
| **过程** | 写笔记前先问「你想要什么（want）」，落盘时区分**视频原话**与**模型补充**，并列出存疑点与下一步 |

---

## 快速开始

**需要什么**：Windows 10/11 + Python **3.11~3.13**；（可选，想快很多）NVIDIA 显卡；（生成笔记用）本机 [Ollama](https://ollama.com/download) 拉一个 `qwen3:8b`。
ffmpeg 不用自己装，依赖里自带。

### 第 0 步：把项目拿到本地

```powershell
git clone https://github.com/yn1101ny-cmyk/bili2obsidian.git
cd bili2obsidian
```

没有 git 就在 GitHub 页面点 **Code → Download ZIP**，解压后进目录继续。

### 日常使用：双击就行

**首次安装**（只需做一次）：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 有 NVIDIA 显卡再装 CUDA 运行库（约 1.3GB，纯 CPU 可跳过）
.\.venv\Scripts\python.exe -m pip install -r requirements-cuda.txt

# 可选：识别画面上的提示词/代码/命令
.\.venv\Scripts\python.exe -m pip install -r requirements-vision.txt

# 配置（可选，全都有默认值）
Copy-Item .env.example .env

# 自检：确认这台机器能不能用
.\.venv\Scripts\python.exe -m scripts.doctor
```

**之后每次要用**，双击项目根目录的 **`启动笔记.bat`** 即可（桌面上也放了一个
「MyNoteApp 笔记」快捷方式）。它会自动：

1. 检查虚拟环境在不在 —— 不在就打印出该敲哪几条命令；
2. 检查 Ollama —— 没在跑就帮忙拉起来（首次加载模型可能要等一会儿）；
3. 启动服务，**等服务真的能响应了再打开浏览器**（不是干等固定秒数）；
4. 已经在运行时直接开浏览器，**不会重复启动第二个服务**。

**关掉那个黑窗口就是停止服务。** 数据都在 `data/` 目录里，不会丢。

> 说明：Windows 上 `python.exe` 会再拉起一个子进程干活，
> 真正监听端口的是子进程。所以启动器清理时用的是 `taskkill /T`
> 结束**整棵进程树** —— 只杀父进程会留下孤儿进程占着端口，
> 下次启动就会莫名其妙失败。

### 命令行方式（不用界面）

```powershell
# 启动本地界面
.\.venv\Scripts\python.exe -m app

# 完全不用界面，直接产出笔记
.\.venv\Scripts\python.exe -m scripts.mynote BV1GJ411x7h7
```

### 界面

三个页面，刻意做得极简：

| 页面 | 内容 |
|---|---|
| **首页** `/` | 粘贴链接 → 实时进度（阶段 / 进度条 / 日志 / 可取消）→ 最近笔记列表 |
| **笔记页** `/notes/{id}` | 渲染好的笔记 + 复制 Markdown + 删除 |
| **自检页** `/checks` | 环境状态、本机数据占用、一键清除数据 |

改配置直接编辑 `.env`，不做设置界面。前端是原生 HTML/CSS/JS，
**不引用任何 CDN 资源** —— 离线可用，也不给页面增加出网点
（有测试盯着这一条）。

### 国内网络的两个必设项

```dotenv
# 国内直连 HuggingFace 的**文件 CDN 被阻断**（主站能打开也不算数）
HF_ENDPOINT=https://hf-mirror.com
```

Ollama registry 在国内也常被阻断（`dial tcp ... i/o timeout`）。
若 `ollama pull` 失败，改用 GGUF 导入：

```powershell
.\.venv\Scripts\python.exe -m scripts.fetch_gguf        # 默认 Qwen3-8B-Q4_K_M
```

---

## 命令行

| 命令 | 用途 |
|---|---|
| `python -m app` | 启动本地 Web 界面 |
| `python -m scripts.mynote <链接>` | 跑完整链路，产出一篇笔记 |
| `python -m scripts.doctor` | 环境自检（`--offline` 不联网，`--json` 机器可读） |
| `python -m scripts.smoke_transcribe <链接>` | 只验证「下载 + 转写」这一段 |
| `python -m scripts.check_asr` | 单独检查 CUDA / CTranslate2 是否可用 |
| `python -m scripts.fetch_gguf` | 从 HF 镜像下 GGUF 并导入 Ollama |
| `scripts\v_extract.py <链接…>` | 只拿转写稿：优先 B站官方字幕，拿不到才本地转写；支持多视频 / `--pages 1-3` |
| `scripts\v_shots.py <BV号>` | 从抽帧缓存里挑关键画面并出总览图（不重抽帧、不重跑 OCR） |

常用参数：`--model qwen3:8b`（换模型）、`--force-transcribe`（忽略转写缓存）、
`--show`（直接打印 Markdown）。

---

## 笔记长什么样

**结构顺序就是阅读优先级**，不是视频的讲述顺序：

```markdown
> 🎯 读完这篇你能做到          ← 先说你最终能拿到什么

## 📌 重点（这个视频最该拿走的）  ← 最重要的一节，排在最前
## 🛠️ 能直接用的东西            ← 提示词原文 / 命令 / 步骤，代码块包裹可直接复制
## 🗺️ 大纲（可跳转）            ← 时间戳可点击，跳回原视频
## 💡 视频讲了什么              ← 背景理解，标注「✅ 视频原话」
## 📚 关键概念                  ← 用大白话解释术语
## 🧩 读完这篇，你可能还需要搞清楚这些   ← 只给问题，不给答案
## ❓ 存疑与待验证
```

**刻意不含转写全文** —— 复述整段音频没有价值，篇幅应该让给重点。
转写另有纯文本文件，需要核对时再翻。

### 为什么重点在「怎么抓」

最初的做法是让模型直接「总结这个视频」，结果产出一篇**什么都没漏、
但什么重点都没有**的笔记 —— 全是「视频介绍了…」「开发流程分为…五步」
这类复述，读者拿不走任何东西。

现在分两步走：

1. **先从标题、简介、标签、B站 AI 章节推断「读者要什么」**
   （只用元信息，不看正文，所以很快）。
   用户的原话是「通过视频标题来获取需要抓取的重点」，正是这一步。
2. 带着这份判断去组织笔记，并要求把**能原样复制的东西**逐字抓下来。

### 画面识别：不单纯分析音频

教程类视频的提示词、代码、命令往往**只出现在画面上**，音频里听不到或者听错。

实测同一个 AI 编程视频，**仅凭音频**得到的命令是错的：

| | 仅音频 | 加了画面识别 |
|---|---|---|
| 查状态 | `Gate status` ❌ | `git status` ✅ |
| 初始化 | `初始划为一个get仓酷` ❌ | `初始化为一个git仓库` ✅ |
| 提交信息 | `git commit -m "Initial commit"` | `git commit -m "docs: add repository contribution guidelines."` ✅ |
| 音频没念过的 | — | `git diff --check`、`cd 马克阅读/`、`npm start` ✅ |

开启方式与代价：

```bash
pip install -r requirements-vision.txt   # 可选，不装也能用
```

* 模型（约 36MB）**打包在 wheel 里**，运行时**零网络请求** —— 隐私红线不受影响
* 自动下载低清视频（≤480p，20 分钟约 40~60MB）；**≤480p 不需要 B站 登录态**
* 20 分钟视频的抽帧 + 识别约 1~2 分钟

顺带一提，画面文字还可以**反过来帮我们剔除字幕** ——
把 OCR 出的每行和已有的音频转写做归一化比对，命中就丢掉，
避免同一段话在提示词里出现两遍。通用 OCR 方案只能靠「字幕总在底部」
这种位置启发式，对花字就失效了，而我们手里正好有转写。

### 为什么「待查清单」只给问题不给答案

这是经过实测才定下来的设计，值得说明。

本地 8B 模型在细分领域**没有可靠的外部知识，却会自信地硬编**。
同一个视频、同一份转写、同一句提示词，三次运行对「MGSD 的全称」
给出三种不同答案（`Master Grade Sword` / `Master Grade SD` /
`Master Grade Scale Diecast`），还编出过不存在的「NT纪元」。

这类错误一旦落进笔记，就变成**「自己记错的知识」**，比不生成更糟 ——
而「补充理解」恰恰是本工具最核心的价值。

关键洞察是模型有两种能力，强弱分明：

| 能力 | 表现 |
|---|---|
| 发现「视频哪里没讲清楚、却默认你懂」 | ✅ 很擅长 |
| 补充「正确的外部事实」 | ❌ 不可靠 |

所以这一节改成**只产出待查问题**：

```markdown
- **MGSD 与 HG / RG / MG 的定位差别是什么？**
  视频反复拿它们做对比，却没给出各自定义。
  🔍 建议搜：高达模型比例
```

「视频哪里没交代」是从转写本身就能推出的结论，**不需要外部知识，
因此不可能出错**。而且它比编造出来的答案更有用：直接告诉你该去查什么。

---

## 数据安全

1. **本地优先**：笔记、转写、缓存全部落在本机 `data/`，默认不上传。
2. **不窃取数据**：无遥测、无埋点、无崩溃上报。`tests/test_privacy.py`
   静态扫描全部源码，出网点白名单之外引用网络库会直接测试失败。
3. **密钥不入库**：`.env`、Cookie、API Key 均由 `.gitignore` 排除。
4. **可审计**：全部出网目标列在 [docs/NETWORK.md](docs/NETWORK.md)。
5. **可删除**：一键清除本机全部数据，并如实回报删除了什么。
6. **来源可追溯**：笔记中区分「视频原话」与「模型补充」。

云端模型默认**不可用**，需同时设置 `LLM_PROVIDER=openai` 与
`ALLOW_CLOUD=true` 才会启用（提供方构造函数还会再拒一次）。
Web 服务只监听回环地址，配置层硬校验，填 `0.0.0.0` 直接拒绝启动。

---

## 架构

```
app/
├── config.py            集中配置（密钥隔离、回环校验、云端默认关闭）
├── schemas.py           领域模型（区分 video / model 来源）
├── pipeline.py          端到端编排 + 后台任务管理
├── logging_setup.py     日志（UTF-8 安全、落盘、不打印密钥）
├── __main__.py          python -m app 启动 Web 界面
├── bili/
│   ├── urls.py          链接解析（纯函数）
│   ├── client.py        B站 API（buvid 指纹、限速、缓存、退避重试）  ← 出网
│   └── subtitles.py     字幕解析与碎句合并（纯函数）
├── media/downloader.py  yt-dlp 音频下载                              ← 出网
├── asr/
│   ├── model_store.py   可续传的模型下载器（镜像回退、断点续传）      ← 出网
│   ├── cleanup.py       复读幻觉清洗（纯函数）
│   └── whisper_engine.py 本地转写（Blackwell 适配、热词偏置）
├── notes/
│   ├── chunking.py      分块（纯函数）
│   ├── prompts.py       提示词与 JSON Schema 展平
│   ├── pipeline.py      map-reduce 生成 + 质量重试
│   └── render.py        Markdown 渲染（纯函数，不产出原始 HTML）
├── llm/                 Ollama（本地）/ OpenAI 兼容（可选云端）       ← 出网
├── storage/             SQLite + 笔记文件仓库
└── web/
    ├── server.py        FastAPI 应用工厂
    ├── routes.py        页面 + JSON API
    ├── markdown_render.py  安全渲染（先转义再转换，杜绝注入）
    ├── templates/       三个页面的 Jinja2 模板
    └── static/          原生 CSS/JS，无 CDN
```

---

## 开发

```powershell
# 全部测试（交付前必须全绿）
.\.venv\Scripts\python.exe -m pytest

# 环境自检
.\.venv\Scripts\python.exe -m scripts.doctor
```

工作约定见 [AGENT.md](AGENT.md)：每次改动必须提交 commit，
且必须编写或更新相关测试并确保全部通过。

### 技术依据

踩过的坑与实证结论都记录在案，避免重蹈覆辙：

- **[docs/LESSONS.md](docs/LESSONS.md)** —— 从零搭这套东西过程中，
  **Web 开发 / AI 部署 / 工程方法**三个层面真正踩过才知道的经验。
  每条都写清「现象 → 原因 → 怎么办」，并标注哪些是实测数据、
  哪些是跨项目成立的原理、哪些还没完全坐实。适合当学习材料读。
- [docs/research/blackwell-whisper.md](docs/research/blackwell-whisper.md)
  —— RTX 50 系（sm_120）跑 faster-whisper 的版本要求、
  `compute_type` 的生死线、Windows CUDA DLL 加载
- [docs/research/bilibili-subtitle-audio.md](docs/research/bilibili-subtitle-audio.md)
  —— B站字幕必须登录（WBI 签名也绕不过）、音频不需要登录、
  Referer 校验、限流码
- [docs/NETWORK.md](docs/NETWORK.md) —— 出网清单与隐私边界

---

## 许可证

[MIT](LICENSE)。你可以自由使用、修改、再发布，包括商用；保留版权声明即可。

参与开发前请读 [CONTRIBUTING.md](CONTRIBUTING.md)（硬规矩：测试不联网、不硬编码个人路径、出网请求必须登记、一个改动一个 commit）。
