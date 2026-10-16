#!/usr/bin/env python
"""截图筛选：把已经抽好的关键帧挑成「可插进笔记的配图」，并生成现成的 Markdown 片段。

为什么单独一个脚本：MyNoteApp 的 OCR 流程本来就把帧（`f_00000_000015000.jpg`，
宽度 960）缓存到了 `data/cache/vision/<bvid>_p1/frames/`，但**只输出文字**。
于是笔记里全是文字、一张图都没有 —— 而学习笔记里"一眼看到那个画面"往往比
十行文字有用。这里复用缓存，不重新抽帧、不重新 OCR。

挑选逻辑（不做语义理解，只做"这帧有信息"的启发式）：
  1. 读 OCR 结果（`<bvid>.onscreen.txt`，`[mm:ss]` + 缩进文字行）；
  2. 每个有文字的 OCR 段落，找时间上最近的一帧（±容差）；
  3. 按"文字行数"打分，贪心选取，帧之间保持最小间隔，避免挑出一串几乎一样的画面；
  4. 不够就按时间均匀补帧（保证封面/章节/结尾都可能有图）。

产出（默认写到 <outdir>/shots/）：
  shot_01_0042s.jpg …   挑出来的画面（原始 960px jpg，约 40–70KB/张）
  shots.json            机器可读：时间戳 / 文件 / OCR 文字
  shots.md              直接可贴进笔记的片段（含时间戳跳转链接 + 图注）
  overview.jpg          拼图总览（一张图看完整条视频的画面脉络）

用法：
  python v_shots.py BV1oApe6mELP [--outdir <目录>] [--url <链接>] [--max 10] [--min-gap 30]
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

# 脚本就在仓库里：项目根 = 本文件的上一级目录（scripts/ 的父目录）。
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CACHE = ROOT / "data" / "cache" / "vision"  # 默认 DATA_DIR=data（锚定项目根）
DEFAULT_WORKROOT = Path.home() / "AppData/Local/Temp/dsh-media"

CLOCK_RE = re.compile(r"^\[(\d+):(\d\d)\]\s*$")
FRAME_RE = re.compile(r"^f_(\d{5})_(\d{9})\.jpg$")


def parse_onscreen(path: Path) -> list[dict]:
    """把 `<bvid>.onscreen.txt` 解析成 [{t: 秒, lines: [...]}, ...]。"""
    blocks: list[dict] = []
    if not path.exists():
        return blocks
    cur: dict | None = None
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = CLOCK_RE.match(raw.strip())
        if m:
            cur = {"t": int(m.group(1)) * 60 + int(m.group(2)), "lines": []}
            blocks.append(cur)
            continue
        if cur is not None:
            text = raw.strip()
            if text:
                cur["lines"].append(text)
    return [b for b in blocks if b["lines"]]


def load_frames(vid: str, page: int = 1, frames_dir: Path | None = None) -> list[dict]:
    """读出缓存里已抽好的帧：[{t: 秒, path: Path}, ...]，按时间排序。"""
    base = frames_dir or (CACHE / f"{vid}_p{page}" / "frames")
    if not base.exists():
        return []
    frames = []
    for p in base.iterdir():
        m = FRAME_RE.match(p.name)
        if m:
            frames.append({"t": int(m.group(2)) / 1000.0, "path": p})
    frames.sort(key=lambda f: f["t"])
    return frames


def pick(blocks: list[dict], frames: list[dict], *, max_shots: int, min_gap: float,
         tolerance: float, min_shots: int = 4) -> list[dict]:
    """挑帧：先按"有文字 + 文字多"贪心，再按时间均匀补足。"""
    if not frames:
        return []

    def nearest(t: float) -> dict | None:
        best, best_d = None, None
        for f in frames:
            d = abs(f["t"] - t)
            if best_d is None or d < best_d:
                best, best_d = f, d
        return best if (best is not None and best_d <= tolerance) else None

    cands = []
    for b in blocks:
        f = nearest(b["t"])
        if f:
            score = len(b["lines"]) + (2 if any(len(x) <= 18 for x in b["lines"]) else 0)
            cands.append({"t": f["t"], "path": f["path"], "lines": b["lines"][:6], "score": score})

    chosen: list[dict] = []
    for c in sorted(cands, key=lambda x: -x["score"]):
        if len(chosen) >= max_shots:
            break
        if all(abs(c["t"] - c2["t"]) >= min_gap for c2 in chosen):
            chosen.append(c)

    if len(chosen) < min_shots:  # 画面文字少（访谈/播客）时按时间补
        span = frames[-1]["t"] - frames[0]["t"] or 1.0
        step = span / max(1, min_shots)
        for i in range(min_shots):
            f = nearest(frames[0]["t"] + i * step)
            if f and all(abs(f["t"] - c["t"]) >= min_gap * 0.6 for c in chosen):
                chosen.append({"t": f["t"], "path": f["path"], "lines": []})
            if len(chosen) >= max_shots:
                break

    chosen.sort(key=lambda c: c["t"])
    return chosen[:max_shots]


def clock(t: float) -> str:
    return f"{int(t) // 60:02d}:{int(t) % 60:02d}"


def contact_sheet(files: list[Path], target: Path, *, width: int = 480) -> bool:
    """把截图拼成一张总览图（一张图看完整条视频）。

    用 Pillow 拼：本机的 ffmpeg 构建**没有编译 glob 支持**
    （`-pattern_type glob` 会报 `globbing is not supported by this libavformat build`），
    所以不折腾 ffmpeg，直接用 OCR 依赖里已经有的 Pillow。
    """
    if not files:
        return False
    try:
        from PIL import Image
    except Exception:
        return False
    try:
        cols = 4
        pad = 6
        thumbs = []
        for p in files:
            im = Image.open(p).convert("RGB")
            h = max(1, round(im.height * width / im.width))
            thumbs.append(im.resize((width, h)))
        rows = (len(thumbs) + cols - 1) // cols
        cell_h = max(t.height for t in thumbs)
        sheet = Image.new("RGB", (cols * width + (cols + 1) * pad,
                                  rows * cell_h + (rows + 1) * pad), "white")
        for i, t in enumerate(thumbs):
            r, c = divmod(i, cols)
            sheet.paste(t, (pad + c * (width + pad), pad + r * (cell_h + pad)))
        sheet.save(target, quality=82, optimize=True)
        return target.exists()
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser(prog="v_shots", description="从缓存的帧里挑出可插进笔记的截图")
    ap.add_argument("vid", help="BV 号（用来定位缓存与素材目录）")
    ap.add_argument("--outdir", default="", help="素材目录（默认 %%TEMP%%\\dsh-media\\<id>）")
    ap.add_argument("--url", default="", help="视频链接（用于生成 ?t= 跳转）")
    ap.add_argument("--shots-dir", default="", help="截图输出目录（默认 <outdir>/shots）")
    ap.add_argument("--frames-dir", default="", help="自定义帧目录")
    ap.add_argument("--onscreen", default="", help="自定义 onscreen.txt 路径")
    ap.add_argument("--page", type=int, default=1)
    ap.add_argument("--max", type=int, default=10, help="最多挑几张（默认 10）")
    ap.add_argument("--min-gap", type=float, default=30.0, help="两张截图的最小间隔秒数")
    ap.add_argument("--tolerance", type=float, default=12.0, help="OCR 段落与帧的最大时间差")
    args = ap.parse_args()

    outdir = Path(args.outdir) if args.outdir else DEFAULT_WORKROOT / args.vid
    shots_dir = Path(args.shots_dir) if args.shots_dir else outdir / "shots"
    onscreen = Path(args.onscreen) if args.onscreen else outdir / f"{args.vid}.onscreen.txt"

    frames = load_frames(args.vid, args.page, Path(args.frames_dir) if args.frames_dir else None)
    blocks = parse_onscreen(onscreen)
    if not frames:
        print(json.dumps({"ok": False, "error": f"帧缓存为空（找过 {(args.frames_dir or (CACHE / f'{args.vid}_p{args.page}' / 'frames'))}）"},
                         ensure_ascii=False))
        return 1

    chosen = pick(blocks, frames, max_shots=args.max, min_gap=args.min_gap,
                  tolerance=args.tolerance)
    shots_dir.mkdir(parents=True, exist_ok=True)
    items = []
    files: list[Path] = []
    for i, c in enumerate(chosen, 1):
        # 文件名前缀带上 bvid：Obsidian 的 ![[文件名]] 是"全库唯一即命中"，
        # 多个视频都叫 shot_01_0000s.jpg 会互相串图。
        name = f"shot_{args.vid}_{i:02d}_{clock(c['t']).replace(':', '')}s.jpg"
        dest = shots_dir / name
        shutil.copy2(c["path"], dest)
        files.append(dest)
        items.append({"index": i, "t": round(c["t"], 1), "clock": clock(c["t"]),
                      "file": name, "lines": c["lines"]})

    overview = shots_dir / f"overview_{args.vid}.jpg"
    has_overview = contact_sheet(files, overview)

    url = args.url
    if not url:
        meta_path = outdir / f"{args.vid}.meta.json"
        if meta_path.exists():
            try:
                url = json.loads(meta_path.read_text(encoding="utf-8")).get("url", "")
            except Exception:
                url = ""

    md = [f"# {args.vid} 截图索引（共 {len(items)} 张）", "",
          "> 从本地抽帧里筛出的关键画面：章节卡、板书、命令、图表。",
          "> 笔记里按需embed 几张即可，不必全插 —— 图片是给「一眼看懂」用的，不是装饰。", ""]
    if has_overview:
        md += ["## 总览", "", f"![[{overview.name}]]", ""]
    md += ["## 逐张（含该帧的画面文字）", ""]
    for it in items:
        jump = f"（[跳回 {it['clock']}]({url}?t={int(it['t'])})）" if url else f"（{it['clock']}）"
        md.append(f"### {it['clock']} {jump}")
        md.append("")
        md.append(f"![[{it['file']}]]")
        md.append("")
        for ln in it["lines"]:
            md.append(f"- {ln}")
        md.append("")
    md_path = shots_dir / "截图清单.md"
    md_path.write_text("\n".join(md), encoding="utf-8")

    info = {
        "ok": True,
        "vid": args.vid,
        "frames_available": len(frames),
        "ocr_blocks": len(blocks),
        "shots": items,
        "shots_dir": str(shots_dir),
        "shots_json": str(shots_dir / "shots.json"),
        "shots_md": str(md_path),
        "overview": str(overview) if has_overview else "",
    }
    (shots_dir / "shots.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in info.items() if k != "shots"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
