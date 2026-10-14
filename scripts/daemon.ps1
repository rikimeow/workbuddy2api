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
    [int]$Port = 18790,
    [int]$IntervalSec = 10
)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$PidFile = Join-Path $Root "logs\daemon.pid"
$WatchLog = Join-Path $Root "logs\daemon.log"
# 单例互斥锁：防止「每跑一次就叠加一个守护」互相抢端口
# （历史事故：两个守护 + 四个进程抢 18790，服务被旧环境变量拉起）
$MutexName = "Global\workbuddy2api-daemon-$Port"

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
    # 显式传 --port：main.py 的优先级是「命令行 > .env > 默认」，
    # 显式传能压过 .env 里可能残留的旧端口（历史上 .env 的 ADMIN_PORT
    # 吞掉过命令行参数，导致拉起错误的端口）。
    # -WindowStyle Hidden + Start-Process：脱离当前会话，父进程退出也不受影响
    $p = Start-Process -FilePath $Python -ArgumentList "main.py", "--port", "$Port" `
                       -WorkingDirectory $Root -WindowStyle Hidden -PassThru
    Write-WatchLog "已启动服务 PID=$($p.Id)"
    return $p
}

# ---- -Stop --------------------------------------------------------------
if ($Stop) {
    # 顺序很重要：**先杀守护再杀服务**。反过来的话守护每 10s 探活，
    # 会把刚停掉的服务又拉起来（表现为「stop 不生效」）。
    #
    # 杀守护用两道判据，缺一不可：
    #   1) pidfile 里的 pid（守护自己写的，最准）；
    #   2) 扫描兜底 —— 覆盖 pidfile 丢失/过期、或历史上叠加出来的多个守护。
    # 扫描的匹配条件必须同时满足，否则会误杀：
    #   * 含 "daemon.ps1" 且**不含** "run_daemon.ps1"（后者文件名里也含该子串，
    #     匹配到会把启动器连同调用它的 bash/cmd 包装一起杀掉 —— 已踩过）；
    #   * 含本端口号（据此排除 cc-connect-daemon.ps1 等无关守护）；
    #   * 不在自己的祖先进程链里（避免 stop 把自己杀掉）。
    # 注意路径分隔符可能是 `/` 或 `\`（取决于谁启动的），所以不能写死斜杠形式。
    $ancestors = @($PID)
    $cur = $PID
    for ($i = 0; $i -lt 10; $i++) {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$cur" -ErrorAction SilentlyContinue
        if (-not $proc -or -not $proc.ParentProcessId) { break }
        $cur = [int]$proc.ParentProcessId
        if ($cur -le 0 -or $ancestors -contains $cur) { break }
        $ancestors += $cur
    }

    $daemonPids = New-Object System.Collections.Generic.HashSet[int]

    # 判据 1：pidfile
    if (Test-Path $PidFile) {
        $saved = (Get-Content -Path $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
        $savedPid = 0
        if ([int]::TryParse("$saved".Trim(), [ref]$savedPid) -and $savedPid -gt 0) {
            $daemonPids.Add($savedPid) | Out-Null
        }
    }

    # 判据 2：扫描兜底
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -and
            $_.CommandLine -like "*daemon.ps1*" -and
            $_.CommandLine -notlike "*run_daemon.ps1*" -and
            $_.CommandLine -like "*$Port*" -and
            $ancestors -notcontains $_.ProcessId
        } |
        ForEach-Object { $daemonPids.Add([int]$_.ProcessId) | Out-Null }

    $daemonKilled = 0
    foreach ($dp in $daemonPids) {
        if ($ancestors -contains $dp) { continue }
        Stop-Process -Id $dp -Force -ErrorAction SilentlyContinue
        $daemonKilled++
    }
    if ($daemonKilled -gt 0) {
        Write-WatchLog "已停止守护进程 $daemonKilled 个（PID: $($daemonPids -join ', ')）"
    } else {
        Write-WatchLog "未发现正在运行的守护进程"
    }
    Start-Sleep -Seconds 2   # 给守护一点时间退出，避免它在我们杀服务后又探活拉起

    # 杀服务树。**不能只杀「监听端口的那一个进程」**：历史上出现过两套服务树
    # 并行（多个守护各拉起一套），只杀其中一个监听者，另一套仍在提供服务，
    # 表现为「stop 之后服务还在」。这里按「本项目路径 + 本项目 venv 的 python」整体清掉。
    $svcKilled = 0
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -and
            $_.CommandLine -like "*$Root*" -and
            $_.CommandLine -like "*.venv\Scripts\python*" -and
            $ancestors -notcontains $_.ProcessId
        } |
        ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            $svcKilled++
        }

    if ($svcKilled -gt 0) {
        Write-WatchLog "已停止服务进程 $svcKilled 个"
    } else {
        Write-WatchLog "端口 $Port 上没有正在监听的服务"
    }
    Start-Sleep -Seconds 1
    # 兜底：若端口仍被占用，直接杀占用者
    $stillPid = Get-ListenerPid
    if ($stillPid -gt 0) {
        Stop-Process -Id $stillPid -Force -ErrorAction SilentlyContinue
        Write-WatchLog "兜底：已停止端口占用者 PID=$stillPid"
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
# 单例检查：已有守护在跑就直接退出，避免叠加（多守护会互相抢端口、
# 并用各自的环境变量拉起服务 —— 曾导致新设的 env 不生效）。
$mutex = New-Object System.Threading.Mutex($false, $MutexName)
if (-not $mutex.WaitOne(0)) {
    Write-WatchLog "已有守护在运行（端口 $Port），本次退出，避免叠加"
    exit 0
}

$PID | Out-File -FilePath $PidFile -Encoding ASCII -Force
Write-WatchLog "守护启动（每 ${IntervalSec}s 探活一次，端口 $Port）"

$missCount = 0
try {
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
} finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
