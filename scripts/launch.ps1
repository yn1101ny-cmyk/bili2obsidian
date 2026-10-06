# MyNoteApp 启动器
#
# 由项目根目录的「启动笔记.bat」调用。做四件事：
#   1. 检查虚拟环境在不在（不在就给出可照做的修复指引）
#   2. 已经在跑就直接开浏览器，不重复启动
#   3. Ollama 没在跑就帮忙拉起来（它是笔记生成用的本地模型服务）
#   4. 启动 Web 服务，等服务真的能响应了再打开浏览器
#
# 关闭这个窗口 = 停止服务。数据都在 data/ 目录里，不会丢。

# 注意：这里**不能**加 $ErrorActionPreference = 'Stop'，
# 因为探测端口是否已占用时 Invoke-WebRequest 会抛异常，那是预期内的。

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$Host.UI.RawUI.WindowTitle = 'MyNoteApp'

function Say($text, $color = 'Gray') {
    Write-Host $text -ForegroundColor $color
}

function Test-Http($url, $timeoutSec = 2) {
    try {
        $null = Invoke-WebRequest $url -TimeoutSec $timeoutSec -UseBasicParsing
        return $true
    } catch {
        return $false
    }
}

function Show-Banner {
    Write-Host ''
    Say '  📒  MyNoteApp' 'Yellow'
    Say '      把 B站视频变成看得懂、拿得走的笔记'
    Write-Host ''
}

Show-Banner

# ---------------------------------------------------------------------------
# 1. 虚拟环境
# ---------------------------------------------------------------------------
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    Say '  ❌ 没找到虚拟环境 .venv' 'Red'
    Write-Host ''
    Say '     请在这个目录里执行一次（只需一次）：'
    Say '       python -m venv .venv' 'White'
    Say '       .venv\Scripts\python.exe -m pip install -r requirements.txt' 'White'
    Write-Host ''
    Say "     目录：$root"
    Read-Host ' 按回车关闭'
    exit 1
}

# ---------------------------------------------------------------------------
# 2. 端口（读 .env，没有就用默认 8765）
# ---------------------------------------------------------------------------
$port = 8765
$envFile = Join-Path $root '.env'
if (Test-Path $envFile) {
    $line = Select-String -Path $envFile -Pattern '^\s*PORT\s*=\s*(\d+)' |
            Select-Object -First 1
    if ($line) { $port = [int]$line.Matches[0].Groups[1].Value }
}
$url = "http://127.0.0.1:$port"

# ---------------------------------------------------------------------------
# 3. 已经在跑？直接开浏览器
# ---------------------------------------------------------------------------
if (Test-Http $url) {
    Say "  ✅ 服务已经在运行（$url）" 'Green'
    Say '     直接在浏览器里打开，不需要重复启动。'
    Start-Process $url
    Start-Sleep -Seconds 2
    exit 0
}

# ---------------------------------------------------------------------------
# 4. Ollama（笔记生成用的本地模型服务）
# ---------------------------------------------------------------------------
Say '  ⏳ 检查本地模型服务（Ollama）…'
$ollamaUrl = 'http://127.0.0.1:11434/api/tags'
if (Test-Http $ollamaUrl) {
    Say '     ✅ Ollama 正在运行' 'Green'
} else {
    Say '     没在运行，尝试拉起…' 'DarkYellow'
    $ollamaExe = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama app.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'),
        (Join-Path $env:ProgramFiles 'Ollama\ollama app.exe')
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1

    if ($ollamaExe) {
        Start-Process $ollamaExe -ErrorAction SilentlyContinue
        # 首次加载模型可能比较慢，给到 60 秒
        $ready = $false
        for ($i = 0; $i -lt 120; $i++) {
            if (Test-Http $ollamaUrl 2) { $ready = $true; break }
            Start-Sleep -Milliseconds 500
        }
        if ($ready) {
            Say '     ✅ Ollama 已就绪' 'Green'
        } else {
            Say '     ⚠️  Ollama 还没起来（首次加载模型可能要一会儿）' 'DarkYellow'
            Say '        不影响打开界面和查看已有笔记，只是暂时不能生成新笔记。'
        }
    } else {
        Say '     ⚠️  没找到 Ollama' 'DarkYellow'
        Say '        界面可以正常用，已有笔记也能看；生成新笔记需要它。'
        Say '        下载：https://ollama.com/download'
    }
}

# ---------------------------------------------------------------------------
# 5. 启动服务 → 等就绪 → 开浏览器
# ---------------------------------------------------------------------------
Write-Host ''
Say '  ⏳ 正在启动服务…'

$server = Start-Process -FilePath $python `
    -ArgumentList '-m', 'app' `
    -WorkingDirectory $root `
    -NoNewWindow -PassThru

function Stop-ServerTree($serverProc) {
    # ⚠️ 必须结束**整棵进程树**，不能只 Stop-Process 那一个：
    # Windows 上 venv 的 python.exe 会再拉起一个子进程，
    # 真正监听端口的是子进程。只杀父进程会留下孤儿进程占着端口，
    # 下次启动就会莫名其妙失败（而且看不出原因）。
    if (-not $serverProc) { return }
    $kids = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" `
              -ErrorAction SilentlyContinue |
              Where-Object { $_.ParentProcessId -eq $serverProc.Id })
    if (-not $serverProc.HasExited) {
        & taskkill /F /T /PID $serverProc.Id 2>&1 | Out-Null
    }
    foreach ($kid in $kids) {
        & taskkill /F /T /PID $kid.ProcessId 2>&1 | Out-Null
    }
}

$opened = $false
try {
    for ($i = 0; $i -lt 100; $i++) {
        if ($server.HasExited) { break }
        if (Test-Http $url 2) {
            Say "  ✅ 已就绪：$url" 'Green'
            Start-Process $url
            Say '     浏览器已打开。' 'DarkGray'
            $opened = $true
            break
        }
        Start-Sleep -Milliseconds 300
    }

    if (-not $opened -and -not $server.HasExited) {
        Say '  ⚠️  服务启动较慢，请手动打开：' 'DarkYellow'
        Say "     $url" 'White'
    }

    Write-Host ''
    Say '  ────────────────────────────────────────────────' 'DarkGray'
    Say '   保持这个窗口开着。关闭窗口即停止服务。' 'DarkGray'
    Say "   数据目录：$(Join-Path $root 'data')" 'DarkGray'
    Say '  ────────────────────────────────────────────────' 'DarkGray'
    Write-Host ''

    $server.WaitForExit()
} finally {
    Stop-ServerTree $server
}

if ($server.HasExited -and $server.ExitCode -eq 0) {
    Write-Host ''
    Say '  服务已停止。数据都还在，下次双击同样的文件就能继续。' 'Green'
} elseif ($server.HasExited -and $server.ExitCode -ne 0) {
    Write-Host ''
    Say "  ❌ 服务异常退出（代码 $($server.ExitCode)）" 'Red'
    Say '     常见原因：端口被占用。换一个端口试试：' 'DarkYellow'
    Say "       .venv\Scripts\python.exe -m app --port $($port + 1)" 'White'
    Read-Host ' 按回车关闭'
}
