"""前端布局自检 —— 通过 Chrome DevTools 协议**实际测量**页面。

为什么要有这个：布局重叠、横向滚动条、吸顶元素盖住内容这类问题，
光看 HTML/CSS 源码发现不了，看截图也不够精确（而且人眼容易漏）。
这里直接量元素的 ``getBoundingClientRect``。

用法（需要先启动 ``python -m app``）::

    .\\.venv\\Scripts\\python.exe -m scripts.check_layout
    .\\.venv\\Scripts\\python.exe -m scripts.check_layout --note <id> --width 390

**发现重叠时以非零码退出**，所以可以直接当检查用。

用的是系统自带的 Edge/Chrome 的无头模式，不需要额外安装依赖。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.console import enable_utf8_output  # noqa: E402

#: 常见浏览器位置
_BROWSERS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)

#: 在页面里跑的测量脚本
MEASURE = r"""
(() => {
  const $ = (s) => document.querySelector(s);
  const rect = (el) => {
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { top: Math.round(r.top), left: Math.round(r.left),
             right: Math.round(r.right), bottom: Math.round(r.bottom),
             width: Math.round(r.width), height: Math.round(r.height) };
  };
  const overlapOf = (a, b) => {
    if (!a || !b) return null;
    const ox = Math.min(a.right, b.right) - Math.max(a.left, b.left);
    const oy = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
    return (ox > 1 && oy > 1) ? { ox, oy } : null;
  };

  const header  = rect($('.topbar'));
  const sidebar = rect($('.sidebar'));
  const main    = rect($('.note-main'));

  const grid = $('.note-layout') ? getComputedStyle($('.note-layout')).gridTemplateColumns : '';
  // 单列（窄屏）时侧栏会变成吸顶的横向导航 ——
  // 这时它与内容几何上「重叠」是**预期行为**（和吸顶顶栏同理），
  // 所以只在并排布局（多列）时才把重叠判定为缺陷。
  const isStacked = grid.split(' ').filter(Boolean).length < 2;

  const overlaps = [];
  if (!isStacked) {
    const ox = Math.min(sidebar.right, main.right) - Math.max(sidebar.left, main.left);
    const oy = Math.min(sidebar.bottom, main.bottom) - Math.max(sidebar.top, main.top);
    if (ox > 1 && oy > 1) overlaps.push({ pair: 'sidebar/main', ox, oy });
  }

  // 吸顶元素在滚动后不能跑到顶栏上面去
  const sidebarTop = sidebar ? sidebar.top : null;
  const headerBottom = header ? header.bottom : 0;
  const stickyCovered = (sidebarTop !== null && sidebarTop < headerBottom - 1);

  // 吸顶导航必须有**不透明背景**，否则内容会从它下面透出来
  const sidebarBg = $('.sidebar') ? getComputedStyle($('.sidebar')).backgroundColor : '';
  const sidebarOpaque = sidebarBg !== '' && sidebarBg !== 'rgba(0, 0, 0, 0)' && sidebarBg !== 'transparent';

  const doc = document.documentElement;
  return JSON.stringify({
    header, sidebar, main,
    isStacked,
    overlaps,
    stickyCovered,
    sidebarBg, sidebarOpaque,
    sidebarPosition: $('.sidebar') ? getComputedStyle($('.sidebar')).position : null,
    gridColumns: grid,
    horizontalOverflow: doc.scrollWidth - doc.clientWidth,
    docHeight: doc.scrollHeight,
    tocCount: document.querySelectorAll('.toc a').length,
    activeToc: document.querySelectorAll('.toc a.active').length,
    activeTitle: $('.toc a.active') ? $('.toc a.active').textContent.trim() : null,
    bodyBg: getComputedStyle(document.body).backgroundColor,
  });
})()
"""


def find_browser() -> str | None:
    for path in _BROWSERS:
        if Path(path).is_file():
            return path
    return None


async def _evaluate(ws, expression: str) -> str:
    await ws.send(json.dumps({
        "id": 1,
        "method": "Runtime.evaluate",
        "params": {"expression": expression, "returnByValue": True},
    }))
    while True:
        msg = json.loads(await ws.recv())
        if msg.get("id") == 1:
            result = msg.get("result", {})
            if "exceptionDetails" in result:
                raise RuntimeError(str(result["exceptionDetails"])[:300])
            return result.get("result", {}).get("value")


async def run(args: argparse.Namespace) -> int:
    import websockets

    browser = find_browser()
    if browser is None:
        print("❌ 找不到 Edge 或 Chrome，无法做布局检查")
        return 2

    base = args.base.rstrip("/")

    try:
        notes = httpx.get(f"{base}/api/notes", timeout=10).json()
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 连不上 {base}：{exc}")
        print("   请先在另一个终端运行：python -m app")
        return 2

    if not notes:
        print("⚠️  还没有笔记，跳过笔记页检查（只检查首页）")

    note_id = args.note or (notes[0]["id"] if notes else None)
    port = args.port
    profile = Path("tmp/edge-cdp").resolve()
    profile.mkdir(parents=True, exist_ok=True)

    proc = subprocess.Popen(
        [
            browser, "--headless=new", "--disable-gpu", "--no-first-run",
            "--no-default-browser-check",
            f"--user-data-dir={profile}",
            f"--remote-debugging-port={port}",
            f"--window-size={args.width},{args.height}",
            base + "/",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    failures: list[str] = []
    checks_run = 0

    try:
        ws_url = None
        for _ in range(40):
            try:
                targets = httpx.get(
                    f"http://127.0.0.1:{port}/json/list", timeout=2
                ).json()
                pages = [t for t in targets if t.get("type") == "page"]
                if pages:
                    ws_url = pages[0]["webSocketDebuggerUrl"]
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.5)

        if not ws_url:
            print("❌ 调试端口没起来")
            return 2

        async with websockets.connect(ws_url, max_size=8 * 1024 * 1024) as ws:
            # ⚠️ 必须用 CDP 强制视口，不能只靠 --window-size：
            # Edge 在 Windows 上有最小窗口宽度（实测 390 会变成 489），
            # 于是「窄屏检查」其实根本没在窄屏下跑，数据是假的。
            await ws.send(json.dumps({
                "id": 50,
                "method": "Emulation.setDeviceMetricsOverride",
                "params": {
                    "width": args.width,
                    "height": args.height,
                    "deviceScaleFactor": 1,
                    "mobile": args.width < 768,
                },
            }))

            async def visit(label: str, url: str, *, scroll: int = 0) -> None:
                nonlocal checks_run
                checks_run += 1
                await ws.send(json.dumps({
                    "id": 99, "method": "Page.navigate", "params": {"url": url}
                }))
                await asyncio.sleep(1.5)
                if scroll:
                    await _evaluate(ws, f"window.scrollTo(0, {scroll}); 'ok'")
                    await asyncio.sleep(0.5)

                data = json.loads(await _evaluate(ws, MEASURE))

                print(f"\n=== {label} ===")
                print(
                    f"  文档高 {data['docHeight']}px ｜ "
                    f"横向溢出 {data['horizontalOverflow']}px"
                )
                if data["sidebar"]:
                    s = data["sidebar"]
                    print(
                        f"  侧栏 {s['width']}×{s['height']} @({s['left']},{s['top']}) "
                        f"position={data['sidebarPosition']}"
                    )
                if data["main"]:
                    m = data["main"]
                    print(f"  主内容 left={m['left']} width={m['width']}")
                if data["gridColumns"]:
                    mode = "单列（吸顶横向导航）" if data["isStacked"] else "并排"
                    print(f"  栅格 {data['gridColumns']}  → {mode}")
                if data["tocCount"]:
                    print(
                        f"  目录 {data['tocCount']} 项 ｜ 高亮 {data['activeToc']} 项 "
                        f"→ {data['activeTitle']}"
                    )
                print(f"  侧栏背景 {data['sidebarBg']}  页面背景 {data['bodyBg']}")

                # ---- 判定 ----
                if data["horizontalOverflow"] > 1:
                    failures.append(
                        f"{label}: 出现横向滚动（溢出 {data['horizontalOverflow']}px）"
                    )
                    print(f"  ❌ 横向溢出 {data['horizontalOverflow']}px")
                else:
                    print("  ✅ 无横向滚动")

                if data["sidebar"] is None:
                    # 首页没有侧栏，跳过所有与侧栏相关的判定
                    print("  （本页无侧栏，跳过侧栏相关检查）")
                elif data["isStacked"]:
                    # 单列时侧栏是吸顶导航，与内容几何重叠是预期行为，
                    # 但它必须有**不透明背景**，否则内容会从下面透出来
                    if not data["sidebarOpaque"]:
                        failures.append(f"{label}: 吸顶导航背景透明，内容会透出来")
                        print("  ❌ 吸顶导航背景透明")
                    else:
                        print("  ✅ 吸顶导航背景不透明（重叠为预期行为）")
                elif data["overlaps"]:
                    failures.append(f"{label}: 元素重叠 {data['overlaps']}")
                    print(f"  ❌ 重叠：{data['overlaps']}")
                else:
                    print("  ✅ 无重叠")

                if data["sidebar"] is None:
                    pass
                elif data["stickyCovered"]:
                    failures.append(f"{label}: 吸顶侧栏盖住了顶栏")
                    print("  ❌ 吸顶侧栏位置异常")
                else:
                    print("  ✅ 吸顶元素位置正常")

                if data["tocCount"] and data["activeToc"] != 1:
                    failures.append(
                        f"{label}: 滚动监听高亮项数异常（{data['activeToc']}）"
                    )
                    print("  ❌ 滚动监听异常")
                elif data["tocCount"]:
                    print("  ✅ 滚动监听正常")

            if note_id:
                await visit(
                    f"笔记页 · {args.width}px",
                    f"{base}/notes/{note_id}",
                )
                await visit(
                    f"笔记页 · {args.width}px · 滚动后",
                    f"{base}/notes/{note_id}",
                    scroll=1800,
                )
            await visit(f"首页 · {args.width}px", base + "/")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            proc.kill()

    print("\n" + "=" * 60)
    if failures:
        print(f"❌ {checks_run} 项检查中发现 {len(failures)} 个问题：")
        for f in failures:
            print(f"   · {f}")
        return 1
    print(f"✅ {checks_run} 项布局检查全部通过")
    return 0


def main() -> int:
    enable_utf8_output()
    parser = argparse.ArgumentParser(
        prog="check_layout", description="前端布局自检（需要 app 服务在跑）"
    )
    parser.add_argument("--base", default="http://127.0.0.1:8765", help="服务地址")
    parser.add_argument("--note", default="", help="指定笔记 id（默认取第一篇）")
    parser.add_argument("--width", type=int, default=1440, help="视口宽度")
    parser.add_argument("--height", type=int, default=1000, help="视口高度")
    parser.add_argument("--port", type=int, default=9222, help="调试端口")
    args = parser.parse_args()

    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
