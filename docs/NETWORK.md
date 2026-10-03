# 出网清单 —— 数据安全审计

本文件列出 MyNoteApp **全部**会主动发起网络请求的目标，以及各自出于什么目的。
任何新增出网点都必须同步更新本文件与 `tests/test_privacy.py` 的白名单，
否则测试会失败。

> **承诺**：除了下表列出的目标，本应用不会向任何其他地方发送数据。
> 没有遥测、没有埋点、没有崩溃上报、没有「匿名统计」。

## 一、出网目标清单

| # | 目标主机 | 触发时机 | 发出的数据 | 接收的数据 | 代码位置 |
|---|---|---|---|---|---|
| 1 | `api.bilibili.com` | 每次生成笔记 | 视频 BV/av 号、cid（**以及可选的 SESSDATA，仅当你配置了它**） | 视频标题、UP主、时长、分P、标签 | `app/bili/client.py` |
| 2 | `aisubtitle.hdslb.com` | 仅当需要下载字幕时 | 字幕文件的 URL（含 B站签发的 auth_key） | 字幕正文 JSON | `app/bili/client.py` |
| 3 | `b23.tv` | 仅当你粘贴的是短链 | 短链路径 | HTTP 302 跳转地址 | `app/bili/client.py` |
| 4 | `*.bilivideo.com`、`*.bcache*.com` 等 B站 CDN | 仅当需要转写音频时 | 音频文件 URL（含 B站签发的签名与时效） | 音频流（m4a） | `app/media/downloader.py`（经 yt-dlp） |
| 5 | `huggingface.co` 或你配置的镜像（如 `hf-mirror.com`） | **仅首次**下载模型权重 | 模型仓库名 | Whisper 模型文件（约 1.5GB） | `app/asr/model_store.py` |
| 6 | `127.0.0.1:11434` | 每次生成笔记 | 视频转写全文 | 生成的笔记 JSON | `app/llm/ollama_provider.py` |
| 7 | 你配置的云端 LLM（默认 `api.deepseek.com`） | **默认关闭**，需 `ALLOW_CLOUD=true` | 视频转写全文 | 生成的笔记 JSON | `app/llm/openai_provider.py` |

### 逐条说明

**1–4：与 B站的交互。** 目的是获取视频信息、字幕与音频，这是产品的功能前提。
发送的都是视频标识符；除非你主动配置 `BILI_SESSDATA`，否则不发送任何账号凭据。
接收的音频与字幕**只保存在本机** `data/` 目录。

**5：模型权重下载。** 只发生在你第一次使用语音转写时，下载完成后即完全离线。
之后即使断网也能正常转写。模型文件存放在 `data/models/whisper/`，随「清除数据」一并删除。

> **国内网络提示**：实测 huggingface.co **主站可达但文件 CDN 被阻断**——
> 首页 179ms 响应正常，下载 `model.bin` 却直接超时；而且新版 Xet 传输协议
> 会卡在 0 字节毫无进展。
> 因此在 `.env` 里设置 `HF_ENDPOINT=https://hf-mirror.com` 即可切换到镜像
> （实测 0.3~3 MB/s，1.5GB 模型约 8~80 分钟）。
> 下载器支持**断点续传**，中途断了不会前功尽弃。
> 可用 `python -m scripts.doctor` 查看当前生效的端点。

**6：本地模型推理。** 这一条严格说**不是出网**——目标是回环地址，
数据从未离开你的电脑。列在这里是为了完整性。
如果 `OLLAMA_BASE_URL` 被改成非回环地址，程序会把该提供方标记为 `is_remote=True`
并在界面与笔记中给出明确警告。

**7：云端模型（默认不可用）。** 这是唯一会把你的数据发到**本机之外**的路径。
因此做了三重默认拒绝：

1. 必须同时设置 `LLM_PROVIDER=openai` **且** `ALLOW_CLOUD=true`；
2. 提供方构造函数在未获得显式同意时直接抛 `CloudNotAllowed`；
3. 使用云端时，笔记正文会标注「本笔记由云端模型生成」。

**未登录也能拿到音频**是实测确认的（192K AAC），所以「完全不用云端、也不配置 B站登录态」
是一条完整可用的路径。

## 二、绝不外发的内容

以下内容**永远不会**被发送到任何地方：

- 你的笔记正文
- 你的转写文本（除非你显式开启云端模型）
- `SESSDATA`、API Key 等任何凭据（仅在与对应服务通信时作为请求头发送）
- 你的浏览记录、剪贴板、文件列表
- 任何与功能无关的设备信息

## 三、数据落在哪里

全部在本机项目目录下，且**均被 `.gitignore` 排除**：

```
data/
├── notes/            生成的 Markdown 笔记 + 转写全文 + 索引
├── media/            下载的音频
├── cache/            B站接口响应缓存、yt-dlp 缓存
├── models/           Whisper 模型权重（可重新下载）
├── logs/             运行日志
└── mynote.db         SQLite（笔记索引、转写缓存、任务记录）
```

「一键清除全部数据」会删除以上全部内容，并如实回报删除了什么。

## 四、如何自行验证

```powershell
# 1) 静态检查：确认只有被批准的模块引用了网络库
.\.venv\Scripts\python.exe -m pytest tests/test_privacy.py -v

# 2) 查看当前配置里有没有会被外发的东西
.\.venv\Scripts\python.exe -m scripts.doctor

# 3) 确认仓库里没有密钥（应输出空）
git ls-files | Select-String -Pattern '\.env$|cookie|\.key$|SESSDATA'
```

## 五、服务暴露面

Web 服务**只监听回环地址**。这不是约定，是配置层的硬校验：
`app/config.py` 的 `host` 字段在 `127.0.0.1` / `localhost` / `::1` 之外一律拒绝启动，
从根上杜绝把笔记意外暴露到局域网。
