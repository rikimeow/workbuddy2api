# =============================================================================
# workbuddy2api 守护脚本
# -----------------------------------------------------------------------------
# 解决的问题：把 `python main.py` 挂在某个终端 / Agent 后台会话里跑，
# 会话结束时子进程会被连带回收 —— 日志最后一行还是 "200 OK"，但端口没了。
#
# 本脚本用 Start-Process -WindowStyle Hidden 让服务脱离当前会话，
# 并循环探活：进程不在就拉起来。
#
# 用法：
#   powershell -File scripts\daemon.ps1            启动守护（前台占一个窗口）
#   powershell -File scripts\daemon.ps1 -Once      只启动一次，不做守护
#   powershell -File scripts\daemon.ps1 -Stop      停止服务
#   powershell -File scripts\daemon.ps1 -Status    查看状态
# =============================================================================
param(
    [switch]$Once,
    [switch]$Stop,
    [switch]$Status,
    [int]$Port = 8790,
    [int]$IntervalSec = 10
)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$PidFile = Join-Path $Root "logs\daemon.pid"
$WatchLog = Join-Path $Root "logs\daemon.log"

function Write-WatchLog([string]$msg) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Write-Host $line
    try {
        $dir = Split-Path -Parent $WatchLog
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        Add-Content -Path $WatchLog -Value $line -Encoding UTF8
    } catch { }
}

function Get-ListenerPid {
    $c = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
         Select-Object -First 1
    if ($c) { return [int]$c.OwningProcess }
    return 0
}

function Start-ServiceProcess {
    $env:PYTHONIOENCODING = "utf-8"
    $env:PYTHONUTF8 = "1"
    # -WindowStyle Hidden + Start-Process：脱离当前会话，父进程退出也不受影响
    $p = Start-Process -FilePath $Python -ArgumentList "main.py" `
                       -WorkingDirectory $Root -WindowStyle Hidden -PassThru
    Write-WatchLog "已启动服务 PID=$($p.Id)"
    return $p
}

# ---- -Stop --------------------------------------------------------------
if ($Stop) {
    $svcPid = Get-ListenerPid
    if ($svcPid -gt 0) {
        Stop-Process -Id $svcPid -Force -ErrorAction SilentlyContinue
        Write-WatchLog "已停止服务 PID=$svcPid"
    } else {
        Write-WatchLog "端口 $Port 上没有正在监听的服务"
    }
    if (Test-Path $PidFile) { Remove-Item $PidFile -Force -ErrorAction SilentlyContinue }
    exit 0
}

# ---- -Status ------------------------------------------------------------
if ($Status) {
    $svcPid = Get-ListenerPid
    if ($svcPid -gt 0) {
        Write-Host "服务运行中：PID=$svcPid  端口=$Port"
    } else {
        Write-Host "服务未运行（端口 $Port 空闲）"
    }
    exit 0
}

# ---- -Once --------------------------------------------------------------
if ($Once) {
    if ((Get-ListenerPid) -gt 0) {
        Write-WatchLog "端口 $Port 已被占用，跳过启动"
        exit 0
    }
    Start-ServiceProcess | Out-Null
    Start-Sleep -Seconds 8
    $svcPid = Get-ListenerPid
    if ($svcPid -gt 0) { Write-WatchLog "启动成功 PID=$svcPid" }
    else { Write-WatchLog "启动失败，请检查 logs\admin.log" }
    exit 0
}

# ---- 守护模式 -----------------------------------------------------------
$PID | Out-File -FilePath $PidFile -Encoding ASCII -Force
Write-WatchLog "守护启动（每 ${IntervalSec}s 探活一次，端口 $Port）"

$missCount = 0
while ($true) {
    $svcPid = Get-ListenerPid
    if ($svcPid -eq 0) {
        $missCount++
        Write-WatchLog "检测到服务不在（连续 $missCount 次），正在拉起..."
        Start-ServiceProcess | Out-Null
        Start-Sleep -Seconds 8
        $newPid = Get-ListenerPid
        if ($newPid -gt 0) {
            Write-WatchLog "已恢复 PID=$newPid"
            $missCount = 0
        }
    } else {
        if ($missCount -ne 0) { $missCount = 0 }
    }
    Start-Sleep -Seconds $IntervalSec
}
