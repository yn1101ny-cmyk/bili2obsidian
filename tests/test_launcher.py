"""启动器测试。

用户日常只需要双击 `启动笔记.bat`，所以这个文件的可靠性直接决定
「关掉电脑之后还打不开」。而这里最容易出问题的是**编码**：

* `.bat` 会被 cmd 按系统代码页（中文 Windows 是 GBK）解析，
  里面写中文很容易乱码甚至导致命令解析失败 —— 所以 `.bat` 保持纯 ASCII，
  中文提示都放在 `.ps1` 里；
* 但 `.ps1` 又踩另一个坑：Windows PowerShell 5.1 在**没有 BOM** 时
  会按 GBK 读脚本，UTF-8 的中文会变成乱码，还会破坏字符串解析
  （实测报出「字符串缺少终止符」）。所以 `.ps1` **必须带 UTF-8 BOM**。

这两条都是真踩过的坑，所以固化成断言。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.config import PROJECT_ROOT

BAT = PROJECT_ROOT / "启动笔记.bat"
PS1 = PROJECT_ROOT / "scripts" / "launch.ps1"

UTF8_BOM = b"\xef\xbb\xbf"


def _parse_errors(script: Path) -> list[str]:
    """让 PowerShell 解析脚本，返回错误信息列表（空列表 = 语法正确）。"""
    command = (
        "$e=$null;"
        f"[System.Management.Automation.Language.Parser]::ParseFile("
        f"'{script}',[ref]$null,[ref]$e)|Out-Null;"
        "if($e){$e|ForEach-Object{$_.Message}}"
    )
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60,
    )
    return [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]


# ---------------------------------------------------------------------------
# 文件存在
# ---------------------------------------------------------------------------


def test_launcher_bat_exists_at_project_root():
    """放在根目录，用户打开文件夹一眼就能看到。"""
    assert BAT.is_file(), "项目根目录缺少 启动笔记.bat"


def test_launcher_script_exists():
    assert PS1.is_file(), "缺少 scripts/launch.ps1"


# ---------------------------------------------------------------------------
# 编码：两个文件的要求刚好相反
# ---------------------------------------------------------------------------


def test_bat_is_pure_ascii():
    """⛔ `.bat` 里不能有非 ASCII 字符。

    cmd 按系统代码页解析批处理文件，中文很容易乱码，
    严重时连命令都解析不出来。所有中文提示都放在 .ps1 里。
    """
    raw = BAT.read_bytes()
    non_ascii = [(i, b) for i, b in enumerate(raw) if b > 0x7F]
    assert not non_ascii, (
        f"启动笔记.bat 里出现了 {len(non_ascii)} 个非 ASCII 字节，"
        f"首个位置 {non_ascii[0][0]}。中文提示请放到 scripts/launch.ps1"
    )


def test_ps1_has_utf8_bom():
    """⛔ `.ps1` 必须带 UTF-8 BOM。

    Windows PowerShell 5.1 对**没有 BOM** 的脚本按系统代码页（GBK）读取。
    UTF-8 的中文会被解成乱码，而且乱码字节能直接破坏字符串解析 ——
    实测表现为「字符串缺少终止符」的语法错误。
    """
    raw = PS1.read_bytes()
    assert raw.startswith(UTF8_BOM), (
        "scripts/launch.ps1 缺少 UTF-8 BOM，"
        "Windows PowerShell 5.1 会把中文读成乱码"
    )


def test_ps1_is_valid_utf8_after_bom():
    raw = PS1.read_bytes()
    text = raw[len(UTF8_BOM) :].decode("utf-8")  # 解不出来会抛异常
    assert "启动" in text or "笔记" in text


def test_bat_sets_utf8_codepage():
    """控制台切到 UTF-8，中文提示才显示得出来。"""
    text = BAT.read_text(encoding="ascii")
    assert "chcp 65001" in text


def test_bat_delegates_to_ps1():
    text = BAT.read_text(encoding="ascii")
    assert "launch.ps1" in text
    # 允许执行本地脚本，否则默认策略可能直接拒绝运行
    assert "ExecutionPolicy Bypass" in text


# ---------------------------------------------------------------------------
# 启动器的关键行为（检查脚本内容，不真的跑起来）
# ---------------------------------------------------------------------------


def test_launcher_uses_the_venv_python():
    text = PS1.read_text(encoding="utf-8-sig")
    assert r".venv\Scripts\python.exe" in text


def test_launcher_kills_the_whole_process_tree():
    """⛔ 只杀父进程会留下孤儿进程占着端口。

    Windows 上 venv 的 python.exe 会再拉起一个子进程，
    真正监听端口的是子进程。必须用 taskkill /T 结束整棵树，
    否则下次启动会莫名其妙失败（而且看不出原因）。
    """
    text = PS1.read_text(encoding="utf-8-sig")
    assert "taskkill" in text
    assert "/T" in text, "taskkill 必须带 /T 才能结束整棵进程树"


def test_launcher_detects_already_running():
    """已经在跑时应直接开浏览器，而不是再起一个服务。"""
    text = PS1.read_text(encoding="utf-8-sig")
    assert "已经在运行" in text


def test_launcher_waits_for_readiness_not_a_fixed_sleep():
    """要轮询等就绪，不能干等固定秒数 —— 机器慢的时候会打开一个空白页。"""
    text = PS1.read_text(encoding="utf-8-sig")
    assert "Test-Http" in text
    assert "-lt 100" in text or "for (" in text


def test_launcher_does_not_touch_proxy_settings():
    """启动器不该偷偷改代理 —— 那会掩盖真实的网络问题。"""
    text = PS1.read_text(encoding="utf-8-sig")
    for bad in ("HTTP_PROXY", "HTTPS_PROXY", "-Proxy "):
        assert bad not in text, f"启动器里出现了 {bad}，不应该动代理设置"


# ---------------------------------------------------------------------------
# 语法（需要 PowerShell，没有就跳过）
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    shutil.which("powershell") is None, reason="这台机器上没有 powershell"
)
def test_ps1_parses_without_syntax_errors():
    """⛔ 编码坏了会直接表现为语法错误，所以这条要真跑一次解析。"""
    assert _parse_errors(PS1) == [], (
        f"launch.ps1 有语法错误：{_parse_errors(PS1)}"
    )


@pytest.mark.skipif(
    shutil.which("powershell") is None, reason="这台机器上没有 powershell"
)
def test_syntax_check_actually_detects_errors(tmp_path):
    """防止上面那条测试本身失效（比如解析调用写错了，永远返回空）。

    这里故意造一个语法错误的脚本，确认检查能抓出来。
    """
    broken = tmp_path / "broken.ps1"
    broken.write_text("Write-Host 'unterminated\nif ($true) {\n", encoding="utf-8")
    errors = _parse_errors(broken)
    assert errors, "语法检查没能发现故意写坏的文件 —— 检查逻辑本身有问题"
