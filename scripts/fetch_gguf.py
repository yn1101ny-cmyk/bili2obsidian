"""从 HuggingFace 镜像拉 GGUF 并导入 Ollama。

**为什么需要这个**：``registry.ollama.ai`` 在国内常被阻断。
实测报错::

    Error: pull model manifest: Get "https://registry.ollama.ai/v2/library/
    qwen3/manifests/8b": dial tcp 104.18.17.170:443: i/o timeout

而 HuggingFace 有官方的 GGUF 仓库，走镜像（如 hf-mirror.com）下载后
用 ``ollama create`` 导入即可，绕开 registry。

用法::

    # 默认拉 Qwen3-8B-Q4_K_M 并注册为 qwen3:8b
    .\\.venv\\Scripts\\python.exe -m scripts.fetch_gguf

    # 指定别的模型
    .\\.venv\\Scripts\\python.exe -m scripts.fetch_gguf ^
        --repo Qwen/Qwen2.5-7B-Instruct-GGUF ^
        --file qwen2.5-7b-instruct-q4_k_m.gguf ^
        --name qwen2.5:7b
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.asr.model_store import _download_one, _probe_file  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.console import enable_utf8_output  # noqa: E402

DEFAULT_REPO = "Qwen/Qwen3-8B-GGUF"
DEFAULT_FILE = "Qwen3-8B-Q4_K_M.gguf"
DEFAULT_NAME = "qwen3:8b"

#: 下载用的镜像候选
MIRRORS = ("https://hf-mirror.com", "https://huggingface.co")


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def find_ollama() -> str | None:
    exe = shutil.which("ollama")
    if exe:
        return exe
    for candidate in (
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe",
        Path("C:/Program Files/Ollama/ollama.exe"),
    ):
        if candidate.is_file():
            return str(candidate)
    return None


def download(settings, repo: str, filename: str) -> Path:
    """从可用的镜像下载 GGUF（支持断点续传）。"""
    out_dir = settings.models_dir / "gguf"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / filename

    with httpx.Client(timeout=httpx.Timeout(30.0, connect=15.0)) as client:
        base = None
        for mirror in MIRRORS:
            url = f"{mirror}/{repo}/resolve/main/{filename}"
            print(f"  探测 {mirror} …", flush=True)
            reachable, size = _probe_file(client, url)
            if reachable:
                base = mirror
                print(f"  ✅ 可用，远端大小：{human(size) if size else '未知'}")
                break
        if base is None:
            raise SystemExit("❌ 所有镜像都不可达")

        if target.is_file() and target.stat().st_size > 0:
            print(f"  已存在：{target}（{human(target.stat().st_size)}）")
            return target

        url = f"{base}/{repo}/resolve/main/{filename}"
        print(f"  开始下载 {filename}（可中断，下次会续传）…", flush=True)

        last = [0.0]

        def on_progress(got: int, total: int, _label: str) -> None:
            if time.time() - last[0] < 2.0:
                return
            last[0] = time.time()
            if total:
                pct = got / total * 100
                print(
                    f"    {human(got)} / {human(total)}  ({pct:.1f}%)",
                    flush=True,
                )
            else:
                print(f"    {human(got)}", flush=True)

        _download_one(
            client,
            url=url,
            target=target,
            label=filename,
            expected=None,
            on_progress=on_progress,
        )

    print(f"  ✅ 下载完成：{target}（{human(target.stat().st_size)}）")
    return target


def create_ollama_model(gguf: Path, name: str) -> None:
    """用 Modelfile 把 GGUF 注册进 Ollama。"""
    ollama = find_ollama()
    if ollama is None:
        raise SystemExit("❌ 找不到 ollama 可执行文件，请确认已安装")

    modelfile = gguf.parent / f"Modelfile.{name.replace(':', '_')}"
    # Ollama 需要容器内能看到这个路径，直接用绝对路径最稳
    modelfile.write_text(
        f"FROM {gguf.resolve().as_posix()}\n"
        "PARAMETER temperature 0.3\n"
        "PARAMETER num_ctx 8192\n",
        encoding="utf-8",
    )
    print(f"  Modelfile: {modelfile}")
    print(f"  ollama create {name} …", flush=True)

    proc = subprocess.run(
        [ollama, "create", name, "-f", str(modelfile)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"❌ ollama create 失败：\n{proc.stdout}\n{proc.stderr}"
        )
    print(f"  ✅ 已注册为 {name}")


def main() -> int:
    enable_utf8_output()
    parser = argparse.ArgumentParser(
        prog="fetch_gguf",
        description="从 HF 镜像下 GGUF 并导入 Ollama（绕开被墙的 registry）",
    )
    parser.add_argument("--repo", default=DEFAULT_REPO, help="HF 仓库，如 Qwen/Qwen3-8B-GGUF")
    parser.add_argument("--file", default=DEFAULT_FILE, help="GGUF 文件名")
    parser.add_argument("--name", default=DEFAULT_NAME, help="注册到 Ollama 的模型名")
    parser.add_argument("--no-create", action="store_true", help="只下载，不导入 Ollama")
    args = parser.parse_args()

    settings = get_settings()
    print(f"仓库 : {args.repo}")
    print(f"文件 : {args.file}")
    print(f"目标 : {args.name}")
    print()

    gguf = download(settings, args.repo, args.file)
    if not args.no_create:
        create_ollama_model(gguf, args.name)

    print()
    print(f"完成。可以在 .env 里设置 OLLAMA_MODEL={args.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
