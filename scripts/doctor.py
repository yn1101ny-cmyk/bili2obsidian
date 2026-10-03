"""环境自检 —— 一条命令看清「这台机器到底能不能用」。

用法::

    .\\.venv\\Scripts\\python.exe -m scripts.doctor            # 全量检查
    .\\.venv\\Scripts\\python.exe -m scripts.doctor --offline  # 跳过联网检查
    .\\.venv\\Scripts\\python.exe -m scripts.doctor --json     # 机器可读

**隐私**：联网检查只会请求一个公开视频的元数据，不发送任何用户数据、
不触碰你的笔记。加 ``--offline`` 则完全不联网。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import PROJECT_ROOT, Settings, get_settings  # noqa: E402
from app.console import enable_utf8_output  # noqa: E402

#: 用来验证 B站接口连通性的公开视频（官方 MV，长期稳定）
PROBE_BVID = "BV1GJ411x7h7"

OK = "ok"
INFO = "info"
WARN = "warn"
FAIL = "fail"

_MARK = {OK: "✅", INFO: "ℹ️ ", WARN: "⚠️ ", FAIL: "❌"}


@dataclass
class Check:
    """单项检查结果。"""

    name: str
    status: str
    detail: str
    hint: str = ""

    @property
    def mark(self) -> str:
        return _MARK.get(self.status, "?")


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, check: Check) -> None:
        self.checks.append(check)

    @property
    def worst(self) -> str:
        if any(c.status == FAIL for c in self.checks):
            return FAIL
        if any(c.status == WARN for c in self.checks):
            return WARN
        return OK

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.status == WARN]


# ---------------------------------------------------------------------------
# 各项检查
# ---------------------------------------------------------------------------


def check_python(settings: Settings) -> Check:
    version = ".".join(str(v) for v in sys.version_info[:3])
    if sys.version_info < (3, 11):
        return Check(
            "Python 版本",
            FAIL,
            f"{version}（过低）",
            "本项目需要 Python 3.11+。",
        )
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    return Check(
        "Python 版本",
        OK,
        f"{version}（{'虚拟环境' if in_venv else '全局环境'}）",
        "" if in_venv else "建议在 .venv 虚拟环境里运行，避免污染全局。",
    )


def check_dirs(settings: Settings) -> Check:
    try:
        settings.ensure_dirs()
    except OSError as exc:
        return Check("数据目录", FAIL, f"无法创建：{exc}", "检查磁盘权限与剩余空间。")

    probe = settings.data_dir / ".write_probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return Check(
            "数据目录", FAIL, f"不可写：{settings.data_dir}（{exc}）", "检查目录权限。"
        )
    return Check("数据目录", OK, str(settings.data_dir))


def check_disk(settings: Settings) -> Check:
    try:
        usage = shutil.disk_usage(settings.data_dir)
    except OSError as exc:
        return Check("磁盘空间", WARN, f"无法读取：{exc}")

    free_gb = usage.free / 1024**3
    # Whisper turbo 约 1.6GB，加上音频与模型余量，留 5GB 比较稳妥
    if free_gb < 2:
        return Check(
            "磁盘空间",
            FAIL,
            f"仅剩 {free_gb:.1f} GB",
            "至少需要 2GB（转写模型约 1.6GB）。请清理磁盘。",
        )
    if free_gb < 5:
        return Check(
            "磁盘空间",
            WARN,
            f"剩余 {free_gb:.1f} GB",
            "建议留 5GB 以上，容纳模型与音频缓存。",
        )
    return Check("磁盘空间", OK, f"剩余 {free_gb:.1f} GB")


def check_asr_deps() -> Check:
    try:
        import ctranslate2
    except ImportError:
        return Check(
            "语音转写依赖",
            FAIL,
            "未安装 ctranslate2",
            "执行：pip install -r requirements.txt",
        )

    version = getattr(ctranslate2, "__version__", "?")
    if version < "4.6.3":
        return Check(
            "语音转写依赖",
            WARN,
            f"ctranslate2 {version}",
            "低于 4.6.3：这是首个用 CUDA 12.8 构建、真正支持 "
            "Blackwell(RTX 50系) 的版本，建议升级。",
        )
    return Check("语音转写依赖", OK, f"ctranslate2 {version}")


def check_cuda(settings: Settings) -> Check:
    from app.asr.whisper_engine import probe_cuda, resolve_compute_type

    try:
        ok, detail = probe_cuda()
    except Exception as exc:  # noqa: BLE001
        return Check("GPU 加速", WARN, f"探测失败：{exc}", "可先用 CPU 转写。")

    if not ok:
        return Check(
            "GPU 加速",
            WARN,
            detail,
            "转写将走 CPU（会慢 5-10 倍）。GPU 排查见 docs/research/blackwell-whisper.md。",
        )

    compute = resolve_compute_type(settings.whisper_compute_type, "cuda")
    return Check("GPU 加速", OK, f"{detail}；将使用 compute_type={compute}")


def check_opencc() -> Check:
    try:
        from opencc import OpenCC

        OpenCC("t2s")
    except ImportError:
        return Check(
            "繁简转换",
            WARN,
            "未安装 OpenCC",
            "Whisper 中文常输出繁体。执行："
            "pip install opencc-python-reimplemented",
        )
    except Exception as exc:  # noqa: BLE001
        return Check("繁简转换", WARN, f"OpenCC 初始化失败：{exc}")
    return Check("繁简转换", OK, "OpenCC t2s 可用，中文将强制简体")


def check_ffmpeg() -> Check:
    from app.media.ffmpeg import find_ffmpeg

    exe = find_ffmpeg()
    if exe:
        return Check("ffmpeg", OK, exe)
    return Check(
        "ffmpeg",
        WARN,
        "未找到",
        "主流程不需要它（只下音频流，用 PyAV 解码）。"
        "仅当需要转码时才有用，可 pip install imageio-ffmpeg。",
    )


def check_vision(settings: Settings) -> Check:
    """检查画面文字识别（OCR）是否可用。

    这一步让笔记能看到**屏幕上写的东西** —— 教程类视频的提示词、
    代码、命令往往只出现在画面里，音频听不到或者会听错
    （实测把 ``git init`` 听成 ``Gate``）。
    """
    from app.vision.ocr import is_available, suggested_threads

    if not settings.vision_enabled:
        return Check(
            "画面识别",
            INFO,
            "已在配置中关闭",
            "如需识别屏幕上的文字，把 VISION_ENABLED 设为 true。",
        )

    ok, detail = is_available()
    if not ok:
        return Check(
            "画面识别",
            WARN,
            "未启用（不影响主流程）",
            "装上就能看到画面上的提示词/代码/命令：pip install rapidocr",
        )

    return Check(
        "画面识别",
        OK,
        f"{detail} ｜ OCR 线程 {suggested_threads()} ｜ "
        f"每 {settings.vision_interval:.0f}s 抽一帧，最多 {settings.vision_max_frames} 帧",
    )


def check_curl_cffi() -> Check:
    """检查 B站 下载用的浏览器指纹库。

    不带指纹时 B站 可能直接返回 412；但实测即使装了它，
    是否启用 ``impersonate`` 也要试 —— 所以下载逻辑是「先默认、失败再回退」。
    """
    try:
        import curl_cffi  # noqa: F401
    except ImportError:
        return Check(
            "curl_cffi",
            WARN,
            "未安装",
            "下载低清视频（画面识别用）时 B站 可能返回 412。"
            "建议：pip install curl-cffi",
        )
    version = getattr(curl_cffi, "__version__", "?")
    return Check("curl_cffi", OK, f"{version}（提供浏览器指纹，降低被 B站 拦截的概率）")


def check_hf_endpoint(settings: Settings) -> Check:
    """检查模型下载通道。

    国内直连 HuggingFace 的**主站可达但文件 CDN 被阻断**，
    如果还没下载过模型又用默认端点，第一次转写必然卡住。
    """
    from app.asr.whisper_engine import apply_hf_env, weights_ready

    endpoint = apply_hf_env(settings)
    is_official = endpoint.rstrip("/") == "https://huggingface.co"
    cached = weights_ready(settings)

    if cached:
        return Check("模型下载通道", OK, f"模型已在本机；端点 {endpoint}")

    if is_official:
        return Check(
            "模型下载通道",
            WARN,
            f"尚未下载模型，当前端点 {endpoint}",
            "实测国内直连该端点的文件 CDN 会超时（主站能打开也不算数）。"
            "建议在 .env 设置 HF_ENDPOINT=https://hf-mirror.com 后重试。",
        )
    return Check("模型下载通道", OK, f"已配置镜像 {endpoint}（模型尚未下载）")


def check_ytdlp() -> Check:
    try:
        import yt_dlp

        version = getattr(yt_dlp.version, "__version__", "?")
    except ImportError:
        return Check(
            "音频下载器",
            FAIL,
            "未安装 yt-dlp",
            "执行：pip install -r requirements.txt",
        )
    return Check("音频下载器", OK, f"yt-dlp {version}")


def check_llm_config(settings: Settings) -> Check:
    provider = settings.llm_provider
    if provider == "ollama":
        from app.llm.ollama_provider import OllamaProvider

        remote = OllamaProvider(
            base_url=settings.ollama_base_url, model=settings.ollama_model
        ).is_remote
        if remote:
            return Check(
                "笔记模型配置",
                WARN,
                f"Ollama / {settings.ollama_model}（指向 {settings.ollama_base_url}）",
                "⚠️ OLLAMA_BASE_URL 不是回环地址，数据会离开本机！"
                "确认这是你自己的机器，否则请改回 127.0.0.1。",
            )
        return Check(
            "笔记模型配置",
            OK,
            f"Ollama / {settings.ollama_model}（本机回环，数据不出本机）",
        )
    if provider == "openai":
        if settings.cloud_enabled:
            return Check(
                "笔记模型配置",
                WARN,
                f"云端 / {settings.openai_model}（已授权外发）",
                "⚠️ 视频字幕全文会发送给第三方服务商。",
            )
        return Check(
            "笔记模型配置",
            WARN,
            "选了云端但未授权",
            "设置 ALLOW_CLOUD=true 才会启用；否则建议改回 LLM_PROVIDER=ollama。",
        )
    return Check(
        "笔记模型配置",
        WARN,
        "已关闭（LLM_PROVIDER=none）",
        "只会产出转写文本，不会生成笔记。",
    )


def check_vault(settings: Settings) -> Check:
    from app.storage.vault import Vault

    try:
        stats = Vault(settings).stats()
    except Exception as exc:  # noqa: BLE001
        return Check("本地数据", WARN, f"统计失败：{exc}")
    return Check("本地数据", OK, stats.human())


# ---------------------------------------------------------------------------
# 联网检查
# ---------------------------------------------------------------------------


async def check_bilibili(settings: Settings) -> Check:
    """验证 B站接口连通并确认能拿到元数据（只查公开视频，不发用户数据）。"""
    from app.bili.client import BiliClient
    from app.bili.urls import parse_video_input

    try:
        async with BiliClient(settings) as client:
            meta = await client.get_video_meta(parse_video_input(PROBE_BVID))
    except Exception as exc:  # noqa: BLE001
        return Check(
            "B站接口",
            FAIL,
            f"请求失败：{type(exc).__name__}: {exc}",
            "检查网络与代理。若提示 -412，说明触发了风控，稍后再试。",
        )

    if not meta.title:
        return Check("B站接口", WARN, "接口通了但没拿到标题")

    return Check("B站接口", OK, f"连通，示例视频：《{meta.title[:24]}》")


async def check_bili_login(settings: Settings) -> Check:
    from app.bili.client import BiliClient

    if not settings.has_bili_login:
        return Check(
            "B站登录态",
            WARN,
            "未配置 BILI_SESSDATA",
            "实测结论：未登录**拿不到任何字幕**（WAF 与 WBI 签名都绕不过）。"
            "当前会自动改用本地 Whisper 转写，功能不受影响，只是每个视频都要转写。",
        )

    try:
        async with BiliClient(settings) as client:
            ok, detail = await client.check_login()
    except Exception as exc:  # noqa: BLE001
        return Check("B站登录态", WARN, f"校验失败：{exc}")

    return Check("B站登录态", OK if ok else WARN, detail)


async def check_ollama(settings: Settings) -> Check:
    if settings.llm_provider != "ollama":
        return Check("本地模型服务", OK, "未使用 Ollama（跳过）")

    from app.llm.ollama_provider import OllamaProvider

    provider = OllamaProvider(
        base_url=settings.ollama_base_url,
        model=settings.ollama_model,
        timeout=10.0,
    )
    try:
        ok, detail = await provider.health()
    except Exception as exc:  # noqa: BLE001
        return Check("本地模型服务", FAIL, f"探测失败：{exc}")
    finally:
        await provider.aclose()

    if not ok:
        return Check(
            "本地模型服务",
            FAIL,
            detail,
            f"启动 Ollama 后执行：ollama pull {settings.ollama_model}",
        )
    return Check("本地模型服务", OK, detail)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


async def run_checks(settings: Settings, *, offline: bool) -> Report:
    report = Report()

    report.add(check_python(settings))
    report.add(check_dirs(settings))
    report.add(check_disk(settings))

    report.add(check_asr_deps())
    report.add(check_cuda(settings))
    report.add(check_opencc())
    report.add(check_hf_endpoint(settings))
    report.add(check_ytdlp())
    report.add(check_curl_cffi())
    report.add(check_ffmpeg())
    report.add(check_vision(settings))

    report.add(check_llm_config(settings))
    report.add(check_vault(settings))

    if offline:
        report.add(
            Check("联网检查", WARN, "已跳过（--offline）", "去掉 --offline 可验证网络与模型服务")
        )
        return report

    report.add(await check_ollama(settings))
    report.add(await check_bilibili(settings))
    report.add(await check_bili_login(settings))
    return report


def render_text(settings: Settings, report: Report) -> str:
    lines: list[str] = []
    lines.append("=" * 70)
    lines.append("  MyNoteApp 环境自检")
    lines.append("=" * 70)
    lines.append("")

    width = max((len(c.name) for c in report.checks), default=10)
    for check in report.checks:
        lines.append(f"  {check.mark}  {check.name.ljust(width)}  {check.detail}")
        if check.hint and check.status != OK:
            lines.append(f"      {' ' * width}  ↳ {check.hint}")
    lines.append("")

    cfg = settings.redacted()
    lines.append("-" * 70)
    lines.append("  当前配置（密钥已脱敏）")
    lines.append("-" * 70)
    for key in ("host", "port", "data_dir", "llm_provider", "ollama_model",
                "allow_cloud", "openai_api_key", "bili_sessdata"):
        lines.append(f"    {key:16} = {cfg.get(key)}")
    lines.append("")

    lines.append("-" * 70)
    if report.worst == OK:
        lines.append("  🎉 全部就绪，可以直接用了。")
    elif report.worst == WARN:
        lines.append("  ⚠️  基本可用，但有几项需要留意（见上方 ↳ 提示）。")
    else:
        lines.append("  ❌ 存在阻塞项，请先解决下列问题：")
        for check in report.failed:
            lines.append(f"     · {check.name}：{check.detail}")
            if check.hint:
                lines.append(f"       ↳ {check.hint}")
    lines.append("-" * 70)
    return "\n".join(lines)


async def main_async(args: argparse.Namespace) -> int:
    settings = get_settings()
    report = await run_checks(settings, offline=args.offline)

    if args.json:
        payload = {
            "status": report.worst,
            "checks": [asdict(c) for c in report.checks],
            "config": settings.redacted(),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(render_text(settings, report))

    return 0 if report.worst != FAIL else 1


def main() -> int:
    enable_utf8_output()
    parser = argparse.ArgumentParser(
        prog="doctor", description="MyNoteApp 环境自检"
    )
    parser.add_argument(
        "--offline", action="store_true", help="跳过所有联网检查"
    )
    parser.add_argument(
        "--json", action="store_true", help="输出 JSON（供程序读取）"
    )
    args = parser.parse_args()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
