# 参与开发

这个项目的目标是**本机跑、本机存**的「B站视频 → 能看懂的笔记」工具。
欢迎提 issue、提 PR，也欢迎只丢一个你跑不通的视频链接。

## 先读这几份

| 想知道什么 | 看哪里 |
|---|---|
| 怎么装、怎么用 | [docs/INSTALL.md](docs/INSTALL.md) |
| 它会往外发什么请求 | [docs/NETWORK.md](docs/NETWORK.md) |
| 踩过的坑和结论 | [docs/LESSONS.md](docs/LESSONS.md) |
| B站字幕/音频的一手实测 | [docs/research/bilibili-subtitle-audio.md](docs/research/bilibili-subtitle-audio.md) |
| Blackwell(RTX 50 系) 转写 | [docs/research/blackwell-whisper.md](docs/research/blackwell-whisper.md) |

## 开发环境

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt          # 已包含 pytest / pytest-asyncio / respx
.\.venv\Scripts\python.exe -m pip install -r requirements-vision.txt   # 动画面识别相关代码时装
```

## 提交前必须做

```powershell
.\.venv\Scripts\python.exe -m pytest -q                     # 必须全绿，CI 也是这么跑的
.\.venv\Scripts\python.exe -m scripts.doctor --offline      # 环境自检
```

`ruff` 配置在 `pyproject.toml`（line-length 100），目前没有强制进 CI，但新代码请顺手对齐。

## 这个项目的硬规矩

PR 会按这几条看：

1. **测试不联网**。所有网络交互用 `respx` 或打桩，CI 里没有网。
2. **隐私红线不能碰**。默认只用本机 Ollama；任何新增的外发请求都要登记进 `docs/NETWORK.md`，并更新测试里的出网白名单。
3. **不硬编码个人路径**（盘符、用户名、`C:\Users\...`）。路径从项目根或配置推导。
4. **一个改动一个 commit**，提交信息用中文，说清「为什么」而不只是「改了什么」。
5. **改行为就要加或改测试**；只改文案/注释可以不带测试。
6. **笔记里区分「视频原话」和模型补充**（原话标 `✅`），模型不确定的东西要进「存疑与待验证」，不能写成结论。
7. **日志里不出现密钥**。`SESSDATA` 一律只显示「已设置」。
8. 面向用户的文案写中文，风格跟现有 README 保持一致：先给结论，再说为什么。

## 提 issue 时请附上

- `python -m scripts.doctor --json` 的输出（它会自动打码密钥）；
- 复现步骤，以及期望结果 / 实际结果；
- 环境：Python 版本、Windows 版本、NVIDIA 显卡型号或纯 CPU、有没有装 `requirements-vision.txt`；
- 如果跟某个视频有关：视频链接（或它的类型：有没有字幕、是不是教程类画面出字）。

## 分支与提交

- 主分支是 `main`，直接往 `main` 提 PR 即可；
- 大改动（换模型、改数据格式、加新的出网入口）请先开 issue 聊一下，避免白干。
