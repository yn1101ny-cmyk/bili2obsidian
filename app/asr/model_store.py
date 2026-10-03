"""Whisper 模型权重的下载与本地管理。

为什么不直接用 ``hf_hub_download``：

1. **不能续传** —— 1.5GB 在国内镜像上（实测 0.3~3 MB/s 大幅波动）几乎必然会断，
   断了就要从头再来；
2. **没有进度** —— 用户要等十几分钟，界面上必须能看到进度；
3. **缓存目录结构对 Windows 不友好**（默认用符号链接，非开发者模式会退化）。

所以这里自己实现：**断点续传 + 进度回报 + 镜像回退 + 大小校验**。

关于文件大小，有个实测结论值得记下来：

  hf-mirror.com 会 307 重定向到 ``/dropbox-dash/...``，最终返回的是
  **chunked 响应，完全没有 Content-Length**。

因此本模块的探测逻辑把「可达」和「知道大小」严格分开 ——
拿不到大小只影响进度百分比，绝不等于下载不了。
大小的权威来源改用 HF 的**元数据接口**（只取文件清单，不传文件内容）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from app.config import Settings
from app.errors import TranscriptionError

logger = logging.getLogger(__name__)

#: faster-whisper 的模型别名 -> HuggingFace 仓库
MODEL_REPOS: dict[str, str] = {
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "large-v3": "Systran/faster-whisper-large-v3",
    "large-v2": "Systran/faster-whisper-large-v2",
    "medium": "Systran/faster-whisper-medium",
    "small": "Systran/faster-whisper-small",
    "base": "Systran/faster-whisper-base",
    "tiny": "Systran/faster-whisper-tiny",
}

#: 模型目录必需的文件（缺一不可）
REQUIRED_FILES: tuple[str, ...] = ("config.json", "model.bin", "tokenizer.json")
OPTIONAL_FILES: tuple[str, ...] = ("preprocessor_config.json",)
#: 词表在不同模型里命名不同
VOCAB_CANDIDATES: tuple[str, ...] = ("vocabulary.json", "vocabulary.txt")

#: 镜像回退顺序（第一个是官方，后面是镜像）
DEFAULT_MIRRORS: tuple[str, ...] = (
    "https://hf-mirror.com",
    "https://huggingface.co",
)

#: 单次请求的失败重试次数
MAX_RETRIES = 6

#: 下载分块大小
CHUNK = 1 << 20  # 1 MiB

#: 进度回调 ``(已下载字节, 总字节或 0 表示未知, 当前文件名)``
ProgressFn = Callable[[int, int, str], None]


# ---------------------------------------------------------------------------
# 名称与路径
# ---------------------------------------------------------------------------


def resolve_repo(model_name: str) -> str:
    """把别名解析成 HuggingFace 仓库名；已是 ``owner/name`` 形式则原样返回。"""
    name = (model_name or "").strip()
    if not name:
        raise TranscriptionError("模型名称为空")
    if "/" in name:
        return name
    repo = MODEL_REPOS.get(name.lower())
    if repo is None:
        raise TranscriptionError(
            f"不认识的模型别名 {name!r}。已知：{', '.join(sorted(set(MODEL_REPOS)))}；"
            "也可以直接写 owner/repo 形式。"
        )
    return repo


def local_model_dir(settings: Settings, model_name: str) -> Path:
    """模型在本机的落地目录（普通目录，非 HF 缓存结构）。"""
    repo = resolve_repo(model_name)
    return settings.models_dir / "whisper" / repo.replace("/", "--")


def _is_complete(path: Path, expected: int | None) -> bool:
    """文件是否已完整。``expected`` 为 None 时只要求非空。"""
    try:
        if not path.is_file():
            return False
        size = path.stat().st_size
    except OSError:
        return False
    if size == 0:
        return False
    if expected is not None and expected > 0:
        return size == expected
    return True


def find_vocab(model_dir: Path) -> Path | None:
    """找出词表文件（不同模型命名不同）。"""
    for name in VOCAB_CANDIDATES:
        candidate = model_dir / name
        try:
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
        except OSError:
            continue
    return None


def is_model_ready(settings: Settings, model_name: str) -> bool:
    """模型是否已经完整下载到本机。"""
    model_dir = local_model_dir(settings, model_name)
    if not model_dir.is_dir():
        return False
    if not all((model_dir / f).is_file() for f in REQUIRED_FILES):
        return False
    return find_vocab(model_dir) is not None


# ---------------------------------------------------------------------------
# 端点与探测
# ---------------------------------------------------------------------------


def _base_urls(settings: Settings) -> list[str]:
    """按优先级给出下载用的基础地址。"""
    urls: list[str] = []
    if settings.hf_endpoint:
        urls.append(settings.hf_endpoint.rstrip("/"))
    for mirror in DEFAULT_MIRRORS:
        if mirror not in urls:
            urls.append(mirror)
    return urls


def _file_url(base: str, repo: str, filename: str) -> str:
    return f"{base}/{repo}/resolve/main/{filename}"


def _probe_file(client: httpx.Client, url: str) -> tuple[bool, int | None]:
    """探测文件能否下载，并**尽力**获取大小。

    ⚠️ 关键：镜像返回 chunked 响应，没有 ``Content-Length``。
    所以「拿不到大小」只意味着进度条没有总量，
    **绝不代表文件不可达** —— 早期版本把这两件事混为一谈，
    导致明明能下却被判定为「所有端点都不可达」。

    :returns: ``(是否可达, 大小或 None)``
    """
    # 1) HEAD
    try:
        resp = client.head(url, follow_redirects=True)
        if resp.status_code < 400:
            length = resp.headers.get("Content-Length")
            return True, int(length) if length and length.isdigit() else None
    except httpx.HTTPError as exc:
        logger.debug("HEAD %s 失败：%s", url, exc)

    # 2) 请求 1 字节，从 Content-Range 里抠总大小
    try:
        resp = client.get(
            url, headers={"Range": "bytes=0-0"}, follow_redirects=True
        )
        if resp.status_code in (200, 206):
            content_range = resp.headers.get("Content-Range", "")
            if "/" in content_range:
                total = content_range.rsplit("/", 1)[-1].strip()
                if total.isdigit():
                    return True, int(total)
            length = resp.headers.get("Content-Length")
            return True, int(length) if length and length.isdigit() else None
    except httpx.HTTPError as exc:
        logger.debug("Range 探测 %s 失败：%s", url, exc)

    return False, None


def _sizes_from_metadata(repo: str) -> dict[str, int]:
    """通过 HF 元数据接口取各文件大小（只读清单，不传文件内容）。

    这是大小的权威来源 —— 实测镜像下载流里拿不到 Content-Length，
    但元数据接口能正常返回 ``model.bin = 1542.9 MB``。
    失败就返回空字典，功能不受影响（只是没有百分比）。
    """
    try:
        from huggingface_hub import HfApi

        info = HfApi().model_info(repo, files_metadata=True)
    except Exception as exc:  # noqa: BLE001 - 纯锦上添花
        logger.debug("取模型元数据失败（不影响下载）：%s", exc)
        return {}

    sizes: dict[str, int] = {}
    for sibling in getattr(info, "siblings", None) or []:
        name = getattr(sibling, "rfilename", None)
        size = getattr(sibling, "size", None)
        if name and size:
            sizes[str(name)] = int(size)
    return sizes


# ---------------------------------------------------------------------------
# 单文件下载
# ---------------------------------------------------------------------------


def _download_one(
    client: httpx.Client,
    *,
    url: str,
    target: Path,
    label: str,
    expected: int | None = None,
    on_progress: ProgressFn | None = None,
    on_bytes: Callable[[int], None] | None = None,
) -> None:
    """下载单个文件，支持断点续传。

    先写 ``<target>.part``，校验通过后原子重命名 ——
    中途失败不会留下「看起来完整」的坏文件。
    """
    part = target.with_name(target.name + ".part")
    target.parent.mkdir(parents=True, exist_ok=True)

    if _is_complete(target, expected):
        logger.debug("%s 已完整，跳过", label)
        if on_bytes and expected:
            on_bytes(expected)
        return

    for attempt in range(MAX_RETRIES):
        have = part.stat().st_size if part.exists() else 0
        if expected and have > expected:
            part.unlink(missing_ok=True)
            have = 0

        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            with client.stream(
                "GET", url, headers=headers, follow_redirects=True
            ) as resp:
                if resp.status_code not in (200, 206):
                    raise httpx.HTTPStatusError(
                        f"HTTP {resp.status_code}",
                        request=resp.request,
                        response=resp,
                    )
                if have and resp.status_code == 200:
                    # 服务端不支持续传，只能从头来
                    logger.debug("%s 不支持续传，重新下载", label)
                    have = 0
                    part.unlink(missing_ok=True)

                # 流式响应通常没有 Content-Length（chunked），
                # 有则用它修正总量
                total: int | None = expected
                if total is None:
                    length = resp.headers.get("Content-Length")
                    if length and length.isdigit():
                        total = have + int(length)

                mode = "ab" if have else "wb"
                written = have
                last_report = 0.0
                with part.open(mode) as fh:
                    for chunk in resp.iter_bytes(CHUNK):
                        fh.write(chunk)
                        written += len(chunk)
                        if on_bytes:
                            on_bytes(len(chunk))
                        now = time.monotonic()
                        if on_progress and now - last_report > 0.5:
                            on_progress(written, total or 0, label)
                            last_report = now

            if expected and part.stat().st_size != expected:
                raise OSError(
                    f"大小不符：期望 {expected}，实际 {part.stat().st_size}"
                )

            part.replace(target)
            if on_progress:
                done = target.stat().st_size
                on_progress(done, expected or done, label)
            return

        except (httpx.HTTPError, OSError) as exc:
            wait = min(30.0, 1.5 * (2**attempt))
            logger.warning(
                "%s 下载中断（第 %d/%d 次）：%s；%.0fs 后从 %d 字节续传",
                label,
                attempt + 1,
                MAX_RETRIES,
                str(exc)[:150],
                wait,
                part.stat().st_size if part.exists() else 0,
            )
            if attempt == MAX_RETRIES - 1:
                raise TranscriptionError(
                    f"下载 {label} 失败（已重试 {MAX_RETRIES} 次）：{exc}\n"
                    "国内直连 HuggingFace 的文件 CDN 常被阻断，"
                    "请在 .env 设置 HF_ENDPOINT=https://hf-mirror.com 后重试。\n"
                    f"已下载的部分保留在 {part}，下次会从断点继续。"
                ) from exc
            time.sleep(wait)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def ensure_model(
    settings: Settings,
    model_name: str,
    *,
    on_progress: ProgressFn | None = None,
) -> Path:
    """确保模型已下载到本机，返回可交给 faster-whisper 的目录路径。

    :param on_progress: ``(已下载字节, 总字节或 0, 当前文件)``
    """
    if is_model_ready(settings, model_name):
        return local_model_dir(settings, model_name)

    repo = resolve_repo(model_name)
    model_dir = local_model_dir(settings, model_name)
    model_dir.mkdir(parents=True, exist_ok=True)

    bases = _base_urls(settings)
    logger.info("开始下载模型 %s（仓库 %s），候选端点：%s", model_name, repo, bases)

    with httpx.Client(timeout=httpx.Timeout(30.0, connect=15.0)) as client:
        # ---- 选一个可用端点 ----
        base: str | None = None
        for candidate in bases:
            reachable, _ = _probe_file(
                client, _file_url(candidate, repo, "config.json")
            )
            if reachable:
                base = candidate
                break

        if base is None:
            raise TranscriptionError(
                "所有下载端点都不可达。请检查网络，"
                "或在 .env 设置 HF_ENDPOINT=https://hf-mirror.com 后重试。"
            )
        logger.info("使用端点：%s", base)

        # ---- 确定要下哪些文件 ----
        wanted: list[str] = list(REQUIRED_FILES)
        for optional in OPTIONAL_FILES:
            reachable, _ = _probe_file(client, _file_url(base, repo, optional))
            if reachable:
                wanted.append(optional)

        vocab_name: str | None = None
        for name in VOCAB_CANDIDATES:
            reachable, _ = _probe_file(client, _file_url(base, repo, name))
            if reachable:
                vocab_name = name
                break
        if vocab_name is None:
            raise TranscriptionError(
                f"仓库 {repo} 里找不到词表文件（{VOCAB_CANDIDATES}）"
            )
        wanted.append(vocab_name)

        # ---- 取大小（尽力而为，只用于进度显示）----
        sizes = _sizes_from_metadata(repo)
        if sizes:
            logger.debug("已获取 %d 个文件的元数据大小", len(sizes))
        total_known = sum(sizes.get(f, 0) for f in wanted)
        if not total_known:
            logger.info("拿不到文件大小（镜像返回 chunked），进度将只显示已下载量")

        downloaded = [0]
        started = time.time()

        def report(label: str) -> None:
            if on_progress:
                on_progress(downloaded[0], total_known, label)

        for filename in wanted:
            target = model_dir / filename
            expected = sizes.get(filename)

            if _is_complete(target, expected):
                downloaded[0] += target.stat().st_size
                report(filename)
                continue

            _download_one(
                client,
                url=_file_url(base, repo, filename),
                target=target,
                label=filename,
                expected=expected,
                on_progress=lambda _got, _total, lbl: report(lbl),
                on_bytes=lambda n: downloaded.__setitem__(0, downloaded[0] + n),
            )

    elapsed = time.time() - started
    if not is_model_ready(settings, model_name):
        raise TranscriptionError(
            f"模型下载后校验失败，目录 {model_dir} 内容不完整。"
            "请删除该目录后重试。"
        )

    mb = sum(p.stat().st_size for p in model_dir.iterdir() if p.is_file()) / 1024 / 1024
    logger.info(
        "模型 %s 就绪：%s（%.0f MB，耗时 %.0fs）", model_name, model_dir, mb, elapsed
    )
    return model_dir
