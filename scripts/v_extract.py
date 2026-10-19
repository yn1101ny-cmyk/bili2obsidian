#!/usr/bin/env python
"""B站/视频 → 结构化素材包（字幕或音频转写 + 画面文字 + 元数据），**不复述、不总结**。

设计意图：把 项目根目录 已经跑通的采集能力（B站字幕、GPU Whisper、热词、
画面 OCR、字幕剔除）单独暴露出来，交给 DSH 里更强的模型写 Obsidian 笔记 ——
本脚本自己不调用任何大模型（Ollama 挂了也能用）。

与 MyNoteApp 的关系：直接 import 它的模块，不改它一行代码。
元数据走 yt-dlp（绕开官方 API 的 -412 风控）。

转写的优先级（和 MyNoteApp 主流程一致）：
  1. **B站官方字幕**（UP 主上传的 CC 或 B站 AI 字幕）—— 最准，且秒回；
     需要登录态：把 `BILI_SESSDATA=...` 写进 `.env（项目根）`（该文件已被
     git 忽略；不配置也能跑，只是拿不到字幕）。
  2. **本地 Whisper 转写** —— 免登录兜底，音频 192K 不需要登录。

用法：
  python v_extract.py <链接或BV号> [更多链接...] [--pages 1-3] [--outdir <目录>]
                      [--no-subs] [--no-vision] [--no-audio] [--force]

产出（默认写到 <outdir>，outdir 默认 %TEMP%\\dsh-media\\<id>）：
  <id>.txt          带 [mm:ss] 时间戳的转写全文（多分P 时是 <id>_p2.txt …）
  <id>.onscreen.txt 画面文字（教程类的命令/代码/提示词）
  <id>.meta.json    元数据 + 转写来源/可信度 + 字幕命中情况
stdout 最后一行是 JSON 摘要（单个视频是对象，多个是 {"ok":…, "items":[…]}）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
import time
from pathlib import Path

# 脚本就在仓库里：项目根 = 本文件的上一级目录（scripts/ 的父目录）。
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.asr.whisper_engine import WhisperEngine, build_hotwords  # noqa: E402
from app.bili.subtitles import merge_adjacent_segments  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.media.downloader import AudioDownloader  # noqa: E402
from app.notes.chunking import transcript_to_text  # noqa: E402

# Windows 控制台默认 GBK：视频标题里带 emoji（🧭 之类）时，最后一行 JSON 会抛
# UnicodeEncodeError 让脚本以退出码 1 结束（产物其实已经写好了）。这里把输出钉成 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - 老解释器或已被重定向的流，忽略
        pass

#: 字幕末段时间戳至少要覆盖全片这个比例，否则认定字幕对不上（B站 偶发返回别的视频的字幕）。
SUBTITLE_MIN_COVERAGE = 0.8

BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
AV_RE = re.compile(r"av(\d+)", re.IGNORECASE)
P_RE = re.compile(r"[?&]p=(\d+)")


def to_url(raw: str) -> str:
    """裸 BV/av 号补成完整链接；已是链接则原样返回。"""
    raw = raw.strip()
    if raw.startswith("http"):
        return raw
    m = BV_RE.search(raw)
    if m:
        return f"https://www.bilibili.com/video/{m.group(1)}"
    m = AV_RE.search(raw)
    if m:
        return f"https://www.bilibili.com/video/av{m.group(1)}"
    return raw


def url_page(url: str) -> int:
    """链接里自带的分P 号（?p=3），没有就是 1。"""
    m = P_RE.search(url)
    return int(m.group(1)) if m else 1


def page_url(url: str, page: int) -> str:
    """把链接规整到指定分P：去掉原有的 p 参数，需要时再补一个。"""
    base = P_RE.sub("", url).rstrip("?&")
    if page <= 1:
        return base
    return f"{base}{'&' if '?' in base else '?'}p={page}"


def parse_pages(spec: str) -> list[int]:
    """解析 --pages：`1-3` / `1,3,5` / `2` 混写都可以。"""
    if not spec.strip():
        return []
    out: list[int] = []
    for chunk in re.split(r"[,\s]+", spec.strip()):
        if not chunk:
            continue
        if m := re.fullmatch(r"(\d+)-(\d+)", chunk):
            a, b = int(m.group(1)), int(m.group(2))
            if b < a:
                a, b = b, a
            out.extend(range(a, b + 1))
        elif chunk.isdigit():
            out.append(int(chunk))
        else:
            raise ValueError(f"看不懂的 --pages 片段：{chunk}")
    return sorted({p for p in out if p >= 1})


def ytdlp_meta(url: str) -> dict:
    """用 yt-dlp 取元数据（不碰 B站官方 API，规避 -412）。"""
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--no-warnings", "--no-playlist", "--socket-timeout", "25", "-J", url,
    ]
    proc = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace")
    out = (proc.stdout or "").strip()
    if not out or out == "null":
        raise RuntimeError(f"yt-dlp 没取到元数据：{(proc.stderr or '')[-300:]}")
    return json.loads(out.splitlines()[-1])


def safe_name(text: str, limit: int = 60) -> str:
    text = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", text or "").strip(" .")
    return text[:limit] or "untitled"


def download_lowres_video(settings, *, url: str, vid: str, page: int, max_height: int, force: bool) -> Path:
    """自己下低清视频轨，供抽帧 OCR 用。

    为什么不直接用 MyNoteApp 的 AudioDownloader.download_video：
    它的格式串是 `bv*[height<=480]/…`，而**竖屏视频**在 yt-dlp 里
    360x640/480x852 的 height 分别是 640/852，于是 height<=480 一条都不匹配
    → Requested format is not available。这里补上 `[width<=N]` 兜底。
    （它的 impersonate 组合拳也会让 B站 返回 No video formats found，这里不带。）
    """
    import yt_dlp

    from app.media.ffmpeg import ffmpeg_dir

    dest = settings.cache_dir / "video-ocr" / f"{vid}_p{page}.mp4"
    if dest.exists() and dest.stat().st_size > 0 and not force:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)

    headers = {
        "User-Agent": settings.bili_user_agent,
        "Referer": "https://www.bilibili.com/",
    }
    if settings.has_bili_login:
        headers["Cookie"] = f"SESSDATA={settings.bili_sessdata}"

    ff = ffmpeg_dir()
    opts: dict = {
        "format": (
            f"bv*[height<={max_height}]/"
            f"bv*[width<={max_height}]/"
            f"bv*"
        ),
        "outtmpl": str(dest.with_suffix("")) + ".%(ext)s",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "http_headers": headers,
        "retries": 3,
        "socket_timeout": 30,
        "cookiefile": None,
        "cachedir": str(settings.cache_dir / "ytdlp"),
    }
    if ff:
        opts["ffmpeg_location"] = ff
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])

    hits = [p for p in sorted(dest.parent.glob(dest.stem + ".*")) if p.is_file()]
    if not hits:
        raise RuntimeError("低清视频下载后找不到产物")
    return hits[0]


AID_RE = re.compile(r'"aid":(\d+)')
CID_RE = re.compile(r'"cid":(\d+)')
BVID_RE = re.compile(r'"bvid":"(BV[0-9A-Za-z]+)"')


def json_array(text: str, key: str):
    """从 HTML 里抠出 `"<key>":[...]` 的内联 JSON（B站把播放状态直接写在页面里）。"""
    marker = f'"{key}":['
    i = text.find(marker)
    if i < 0:
        return None
    start = i + len(marker) - 1
    depth = 0
    for j in range(start, len(text)):
        ch = text[j]
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(text[start : j + 1])
                except ValueError:
                    return None
                return data if isinstance(data, list) else None
    return None


def mine_aid_cid(html: str, bvid: str, page: int):
    """从视频页面 HTML 里解析 aid 与**该分P** 的 cid。

    为什么不直接调 `x/web-interface/view`：那个接口在没有 WBI 签名时很容易吃
    **-412 风控**（实测同一台机器上 `view` 被拦、`player/v2` 正常），而字幕接口
    只要有 aid + cid 就能用。页面内联的播放状态两样都带，还顺带给出每个分P
    的 cid 与标题。
    """
    anchor = html.find('"videoData"')
    if anchor >= 0:
        found = BVID_RE.search(html, anchor)
        if found and found.group(1) != bvid:
            raise RuntimeError(f"页面里的 bvid 是 {found.group(1)}，与请求的 {bvid} 不一致")
    base = anchor if anchor >= 0 else 0

    pages = json_array(html, "pages") or []
    part = None
    cid = 0
    if 1 <= page <= len(pages):
        part = pages[page - 1].get("part")
        cid = int(pages[page - 1].get("cid") or 0)
    if not cid:
        m = CID_RE.search(html, base)
        cid = int(m.group(1)) if m else 0

    m = AID_RE.search(html, base)
    aid = int(m.group(1)) if m else 0
    if not aid or not cid:
        raise RuntimeError("页面里没找到 aid/cid，无法查字幕")
    return aid, cid, part


async def web_aid_cid(settings, bvid: str, page: int):
    """抓视频页面 HTML 解析 aid/cid（绕开 `view` 接口的 -412 风控）。"""
    import httpx

    headers = {
        "User-Agent": settings.bili_user_agent,
        "Referer": "https://www.bilibili.com/",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if settings.has_bili_login:
        # 只在内存里拼请求头，不落 cookie 文件（和 MyNoteApp 的约定一致）
        headers["Cookie"] = f"SESSDATA={settings.bili_sessdata}"
    url = f"https://www.bilibili.com/video/{bvid}"
    async with httpx.AsyncClient(
        headers=headers, timeout=settings.bili_timeout, follow_redirects=True
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        html = resp.text
    return mine_aid_cid(html, bvid, page)


#: 字幕接口不稳定：同一 aid+cid 连问两次，返回的轨道数会变（实测 6 -> 1、0 -> 1）
SUBTITLE_ATTEMPTS = 3
#: 歌词标记：B站 AI 字幕偶尔会把 BGM 的歌词当成字幕
LYRICS_MARKERS = ("♪", "♫", "♬")
LYRICS_SHARE_LIMIT = 0.3


def subtitle_url_owned(url: str, aid: int, cid: int) -> bool | None:
    """判断这条字幕记录是不是本视频的。

    B站 AI 字幕的 CDN 路径形如 ``ai_subtitle/prod/<aid><cid><hash>``，把前缀和自己的
    aid+cid 对一下就能识破"接口回了别人家视频的字幕"（2026-10 实测：黑马 Java p27
    拿到的是另一条视频的记录，内容是歌词）。人工字幕是 ``bfs/subtitle/<hash>``，
    没有这个前缀，判断不了就返回 None（表示"未校验"，不是"不属于"）。
    """
    if "ai_subtitle" not in (url or ""):
        return None
    return f"{aid}{cid}" in url


def looks_like_lyrics(segments) -> bool:
    """段里大量出现 ♪/♫/♬ ⇒ 判定为歌词（BGM），不是讲解。"""
    if not segments:
        return False
    marked = sum(1 for s in segments if any(m in (s.text or "") for m in LYRICS_MARKERS))
    return marked / len(segments) >= LYRICS_SHARE_LIMIT


async def bili_subtitle(settings, bvid: str, page: int, duration_sec: float = 0.0):
    """问 B站 要字幕（走 MyNoteApp 的 BiliClient，需要 BILI_SESSDATA）。

    返回 (Transcript | None, 一句话说明)。**任何异常都在调用方兜住** —— 拿不到字幕是
    预期路径，不是错误。

    三道校验（2026-10-19 加，起因是连续三次拿到"不是这条视频"的字幕）：

    1. **归属**：AI 字幕 URL 里的 ``<aid><cid>`` 必须与本视频一致；
    2. **覆盖**：字幕末段至少覆盖全片的 ``SUBTITLE_MIN_COVERAGE``；
    3. **内容**：♪ 标记占比过高 ⇒ 判为歌词而不是讲解。

    任一不过就重试（接口本身也会抽风）。第一次走缓存，之后强制走网络 ——
    缓存里可能正是那条错的。
    """
    from app.bili.client import BiliClient
    from app.bili.subtitles import pick_best_track
    from app.schemas import Transcript

    if not settings.has_bili_login:
        return None, "未配置 B站登录态（BILI_SESSDATA），B站 AI 字幕需要登录"

    aid, cid, _part = await web_aid_cid(settings, bvid, page)
    reasons: list[str] = []

    async with BiliClient(settings) as bili:
        for attempt in range(1, SUBTITLE_ATTEMPTS + 1):
            try:
                tracks = await bili.get_subtitle_tracks(aid, cid, use_cache=attempt == 1)
            except Exception as exc:  # noqa: BLE001
                reasons.append(f"第{attempt}次取轨道失败（{type(exc).__name__}）")
                await asyncio.sleep(1.0)
                continue

            if not tracks:
                reasons.append(f"第{attempt}次无字幕轨道")
                await asyncio.sleep(1.0)
                continue

            owned = [t for t in tracks if subtitle_url_owned(t.url, aid, cid) is not False]
            if not owned:
                reasons.append(f"第{attempt}次字幕不属于本视频（URL 里没有 {aid}{cid}）")
                await asyncio.sleep(1.0)
                continue
            unverified = all(subtitle_url_owned(t.url, aid, cid) is None for t in owned)

            track = pick_best_track(owned)
            if track is None:
                reasons.append(f"第{attempt}次没有可用轨道")
                continue

            try:
                segments = await bili.fetch_subtitle(track)
            except Exception as exc:  # noqa: BLE001
                reasons.append(f"第{attempt}次下载字幕失败（{type(exc).__name__}）")
                await asyncio.sleep(1.0)
                continue

            seg_end = max((float(getattr(s, "end", 0) or 0) for s in segments), default=0.0)
            if duration_sec and seg_end < duration_sec * SUBTITLE_MIN_COVERAGE:
                reasons.append(
                    f"第{attempt}次字幕只覆盖 {seg_end / duration_sec:.0%}"
                    f"（末段 {seg_end:.0f}s / 全长 {duration_sec:.0f}s）"
                )
                await asyncio.sleep(1.0)
                continue

            if looks_like_lyrics(segments):
                reasons.append(f"第{attempt}次字幕疑似歌词（BGM）")
                await asyncio.sleep(1.0)
                continue

            note = f"命中 B站字幕（{track.source.label}）"
            if unverified:
                note += "；无法校验归属（非 AI 字幕路径）"
            return (
                Transcript(
                    segments=segments,
                    source=track.source,
                    language=track.lan,
                    note=f"字幕轨道 {track.label}",
                ),
                note,
            )

    return None, "字幕不可用：" + "；".join(reasons)


async def run_one(args, raw_url: str, page: int) -> dict:
    settings = get_settings()
    settings.ensure_dirs()

    url = page_url(to_url(raw_url), page)
    t_all = time.time()
    meta = ytdlp_meta(url)
    # 多分P 时 yt-dlp 会把 id 写成 `BV1xq6gBgESU_p2`，去掉这个后缀才是真正的 BV 号
    # （否则缓存目录、文件名会叠成 `..._p2_p2`，和 MyNoteApp 的缓存对不上）。
    vid = re.sub(r"_p\d+$", "", meta["id"])
    title = meta.get("title") or vid
    tag = vid if page <= 1 else f"{vid}_p{page}"

    outdir = Path(args.outdir) if args.outdir else Path(args.workroot) / vid
    outdir.mkdir(parents=True, exist_ok=True)

    info = {
        "id": vid,
        "page": page,
        "title": title,
        "part_title": meta.get("part") if page > 1 else None,
        "uploader": meta.get("uploader") or meta.get("channel"),
        "upload_date": meta.get("upload_date"),
        "duration_sec": int(meta.get("duration") or 0),
        "url": meta.get("webpage_url") or url,
        "description": (meta.get("description") or "").strip(),
        "tags": meta.get("tags") or [],
        "outdir": str(outdir),
        "tag": tag,
    }

    transcript = None
    text = ""
    asr_sec = None

    # ---- 1a) 先问 B站 要字幕（最准、最快）----
    if not args.no_subs:
        try:
            transcript, why = await bili_subtitle(settings, vid, page, info["duration_sec"])
        except Exception as exc:  # noqa: BLE001 - 字幕是加分项，失败就转写
            transcript, why = None, f"字幕查询失败：{type(exc).__name__}: {exc}"
        # 闸门：B站 偶尔会返回「别的视频」的字幕轨道（2026-10-18 实测：hit=True，但内容与
        # 本视频毫无关系，字幕末段只到全片 37%）。用「末段时间戳 vs 视频时长」把它挡掉。
        if transcript is not None:
            segs = getattr(transcript, "segments", None) or []
            seg_end = max((float(getattr(s, "end", 0) or 0) for s in segs), default=0.0)
            duration = float(info.get("duration_sec") or 0)
            if duration > 0 and seg_end < duration * SUBTITLE_MIN_COVERAGE:
                why = (
                    f"字幕与视频时长不匹配（末段 {seg_end:.0f}s / 全长 {duration:.0f}s，"
                    f"覆盖 {seg_end / duration:.0%}），疑似抓到了别的视频的字幕，已弃用"
                )
                transcript = None
        info["subtitle"] = {"hit": transcript is not None, "note": why}
        if transcript is None:
            print(f"[i] {vid} p{page} 没拿到字幕：{why}", file=sys.stderr)

    # ---- 1b) 没字幕就下载音频走本地转写（音频不需要登录）----
    if transcript is None and not args.no_audio:
        # 音频挂掉（CDN 超时、登录态失效）不该让整条任务失败：继续跑画面 OCR，
        # 退化成「画面为主」的产物，并把原因写进 meta.json。
        try:
            downloader = AudioDownloader(settings)
            audio = await downloader.download(
                url=info["url"],
                bvid=vid,
                page=page,
                title=title,
                force=args.force,
                on_progress=lambda f, m: None,
            )
            engine = WhisperEngine(settings)
            hotwords = build_hotwords(
                title=title,
                part_title=info.get("part_title"),
                owner=info["uploader"] or "",
                tags=info["tags"],
            )
            try:
                t0 = time.time()
                transcript = await asyncio.to_thread(
                    engine.transcribe, audio, hotwords=hotwords or None
                )
                asr_sec = time.time() - t0
            finally:
                engine.unload()
        except Exception as exc:  # noqa: BLE001 - 音频是主路径但允许失败
            info["transcript_error"] = f"{type(exc).__name__}: {exc}"
            print(
                f"[!] {vid} p{page} 音频下载/转写失败，改走「画面为主」：{exc}",
                file=sys.stderr,
            )

    if transcript is not None:
        transcript = transcript.model_copy(
            update={"segments": merge_adjacent_segments(transcript.segments)}
        )
        text = transcript_to_text(transcript.segments, with_timestamps=True)
        txt_path = outdir / f"{tag}.txt"
        txt_path.write_text(text, encoding="utf-8")
        info |= {
            "transcript_source": transcript.source.label,
            "transcript_confidence": transcript.source.confidence,
            "transcript_diagnostic": transcript.note,
            "segments": len(transcript.segments),
            "chars": transcript.char_count,
            "asr_sec": round(asr_sec, 1) if asr_sec is not None else None,
            "transcript": str(txt_path),
        }

    # ---- 2) 画面文字（教程类视频的命令/代码只出现在画面上）----
    if not args.no_vision:
        try:
            from app.vision.ocr import OcrEngine
            from app.vision.pipeline import extract_on_screen_text

            video = await asyncio.to_thread(
                download_lowres_video,
                settings,
                url=info["url"],
                vid=vid,
                page=page,
                max_height=args.max_height,
                force=args.force,
            )
            work_dir = settings.cache_dir / "vision" / f"{vid}_p{page}"
            blocks = await asyncio.to_thread(
                extract_on_screen_text,
                video,
                work_dir=work_dir,
                transcript_text=text,
                interval=settings.vision_interval,
                max_frames=settings.vision_max_frames,
                engine=OcrEngine(threads=settings.vision_ocr_threads),
                on_progress=lambda f, m: None,
            )
            lines = []
            for blk in blocks:
                stamp = time.strftime("%M:%S", time.gmtime(blk.timestamp))
                lines.append(f"[{stamp}]")
                lines.extend("  " + ln for ln in blk.lines)
            ocr_path = outdir / f"{tag}.onscreen.txt"
            ocr_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            info |= {"onscreen_blocks": len(blocks), "onscreen": str(ocr_path)}
        except Exception as exc:  # 画面识别是加分项，失败不影响主流程
            info |= {"onscreen_error": f"{type(exc).__name__}: {exc}"}

    # ---- 3) 落盘元数据 ----
    info["elapsed_sec"] = round(time.time() - t_all, 1)
    meta_path = outdir / f"{tag}.meta.json"
    meta_path.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    info["meta"] = str(meta_path)
    info["ok"] = True
    return info


async def run_all(args) -> dict:
    specs = parse_pages(args.pages)
    items: list[dict] = []
    failed: list[dict] = []

    for raw in args.url:
        url = to_url(raw)
        pages = specs or [url_page(url)]
        for page in pages:
            try:
                one = await run_one(args, raw, page)
                items.append(one)
                print(f"[✓] {one['tag']} · {one['title'][:40]} → {one['outdir']}", file=sys.stderr)
            except Exception as exc:  # noqa: BLE001 - 一个分P 失败不该拖垮整批
                failed.append({"url": raw, "page": page, "error": f"{type(exc).__name__}: {exc}"})
                print(f"[✗] {raw} p{page} 失败：{exc}", file=sys.stderr)

    if len(items) == 1 and not failed:
        return items[0]
    return {
        "ok": bool(items),
        "count": len(items),
        "pages": specs or None,
        "items": items,
        "failed": failed,
    }


def main() -> int:
    ap = argparse.ArgumentParser(prog="v_extract", description="视频 → 字幕/转写 + 画面文字 + 元数据")
    ap.add_argument("url", nargs="+", help="B站链接 / BV号 / av号（可给多个）")
    ap.add_argument("--outdir", default="", help="产物目录（默认 %%TEMP%%\\dsh-media\\<id>）")
    ap.add_argument("--workroot", default=str(Path.home() / "AppData/Local/Temp/dsh-media"))
    ap.add_argument(
        "--pages", default="",
        help="分P：`1-3` 或 `1,3,5`（默认只做链接里的那一P；多P视频要全做就写 --pages 1-20）",
    )
    ap.add_argument("--no-subs", action="store_true", help="跳过 B站字幕，直接本地 Whisper")
    ap.add_argument("--no-vision", action="store_true", help="跳过画面 OCR（更快）")
    ap.add_argument("--no-audio", action="store_true", help="只要元数据（不转写）")
    ap.add_argument("--max-height", type=int, default=480, help="OCR 用低清视频的高度上限")
    ap.add_argument("--force", action="store_true", help="忽略音频缓存重新下载")
    args = ap.parse_args()

    try:
        info = asyncio.run(run_all(args))
    except Exception as exc:  # noqa: BLE001 - 让调用方拿到完整错误
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1
    print(json.dumps(info, ensure_ascii=False))
    return 0 if info.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
