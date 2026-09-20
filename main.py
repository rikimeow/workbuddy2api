#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""main.py — 一键拉起 workbuddy2api（单端口单进程部署）。

本项目对外只暴露「一个端口」即可：
  - 管理后台前端（index.html）与后端 API：http://127.0.0.1:8790/admin
  - 对外共享的托管网关（带 Key 校验 / 配额 / 用量记账）：http://127.0.0.1:8790/v1/chat/completions、/v1/models
  - 内嵌的独立网关 converter（本机桌面登录态直连，额外支持 /v1/responses、/v1/messages、/v1/balance）：
        http://127.0.0.1:8790/gw/v1/...

converter 已在 admin/server.py 中挂载到 /gw 前缀，因此无需再单独开 8787 端口/进程。
（若你确实需要独立运行 converter 在 8787，直接 `python converter.py --desensitize` 即可，与本脚本互不冲突。）

子进程输出实时 tee 到控制台 + logs/ 日志；Ctrl+C / 关闭终端优雅关闭。

用法:
  python main.py                  # 前台常驻，监听 0.0.0.0:8790
  python main.py --port 8790
  python main.py --host 127.0.0.1

环境变量（可选，覆盖默认；部署务必设置）:
  ADMIN_PORT / ADMIN_HOST
  ADMIN_JWT_SECRET  : 生产务必覆盖为 >=32 字节随机串（默认是弱密钥，会报警）
  ADMIN_USERNAME / ADMIN_PASSWORD     : 后台登录凭据（**必填**，无默认值；未配置则登录 503）
  CONVERTER_DESENSITIZE / CONVERTER_LOG : 内嵌网关的脱敏开关与日志路径
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable  # 用运行本脚本的同一个解释器（venv / 系统都可）
LOGS = ROOT / "logs"
LOGS.mkdir(exist_ok=True)


def _load_dotenv() -> None:
    """尽早把项目根目录的 .env 注入到 os.environ。

    必须在下面的部署安全检查之前执行：此前这些检查读的是「尚未加载 .env」的
    环境，于是即使 .env 里已经配好了 ADMIN_JWT_SECRET / ADMIN_USERNAME /
    ADMIN_PASSWORD，启动时依然会打印「未设置强 ADMIN_JWT_SECRET」「未配置
    ADMIN_USERNAME / ADMIN_PASSWORD」这类**误导性告警**，让人误以为配置丢了。
    （子系统自身会加载 .env，所以这只是虚惊一场，但足够让人排查半天。）
    """
    env_file = ROOT / ".env"
    if not env_file.is_file():
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(env_file)
        return
    except Exception:
        pass
    # 没装 python-dotenv 时退化为一个极简解析器（够用：KEY=VALUE，忽略注释）
    try:
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
    except Exception:
        pass


_load_dotenv()

_PRINT_LOCK = threading.Lock()


def _log(msg: str) -> None:
    with _PRINT_LOCK:
        print(msg, flush=True)


def _pump(stream, log_path: Path, tag: str) -> None:
    """把子进程输出实时打到控制台并写入日志文件（类 tee，文本模式）。"""
    try:
        with open(log_path, "a", encoding="utf-8") as lf:
            for raw in iter(stream.readline, ""):
                if not raw:
                    break
                text = raw.rstrip("\n")
                _log(f"[{tag}] {text}")
                lf.write(raw)
                lf.flush()
    except Exception:
        pass


# 运行后台所需的核心依赖；当前解释器缺任一即尝试切换到带依赖的虚拟环境。
_REQUIRED_DEPS = ["pymysql", "uvicorn", "fastapi", "redis", "sqlalchemy"]


def _interpreter_with_deps() -> str | None:
    """返回带全部依赖的 python 解释器路径。

    优先用当前解释器；若缺包，则回退到项目内 .venv/venv 或本机 managed venv。
    都找不到返回 None（调用方给出安装提示后退出）。
    """
    try:
        import importlib
        for m in _REQUIRED_DEPS:
            importlib.import_module(m)
        return sys.executable
    except Exception:
        pass
    candidates = [
        ROOT / ".venv" / "Scripts" / "python.exe",
        ROOT / "venv" / "Scripts" / "python.exe",
        ROOT / ".venv" / "bin" / "python",
        ROOT / "venv" / "bin" / "python",
        Path(r"C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe"),
    ]
    for c in candidates:
        if not c.exists():
            continue
        try:
            subprocess.run(
                [str(c), "-c", "import " + ", ".join(_REQUIRED_DEPS)],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return str(c)
        except Exception:
            continue
    return None


def _build_admin_cmd(args) -> tuple[list[str], int, str]:
    port = int(os.getenv("ADMIN_PORT", str(args.port)))
    host = os.getenv("ADMIN_HOST", args.host)
    cmd = [PY, "-m", "uvicorn", "admin.server:app",
           "--host", host, "--port", str(port), "--log-level", "info"]
    return cmd, port, host


#: 宝塔面板的 pid 文件路径。写在这里，面板「停止」按钮才杀得对进程。
_PIDFILE = Path("/www/server/python_project/vhost/pids/workbuddy2api.pid")


def _write_pidfile() -> None:
    """把本进程真实 pid 写入 pid 文件（宝塔「停止」按钮据此结束服务）。"""
    try:
        _PIDFILE.parent.mkdir(parents=True, exist_ok=True)
        _PIDFILE.write_text(str(os.getpid()), encoding="utf-8")
        _log(f"[main] pid {os.getpid()} 已写入 {_PIDFILE}")
    except Exception:
        pass


def _remove_pidfile() -> None:
    """退出时清理 pid 文件；仅当文件里确实是本进程时才删，避免误删新实例的。"""
    try:
        if _PIDFILE.is_file() and _PIDFILE.read_text(encoding="utf-8").strip() == str(os.getpid()):
            _PIDFILE.unlink()
    except Exception:
        pass


def _kill_children(procs: list) -> None:
    """结束所有子进程（含其进程组），确保不留孤儿占着端口。"""
    for _tag, p in procs:
        try:
            if p.poll() is not None:
                continue
            # 子进程以 start_new_session=True 启动，自成进程组 → 整组结束
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            except Exception:
                p.terminate()
        except Exception:
            pass
    # 给一点时间优雅退出，仍存活则强杀
    deadline = time.time() + 12
    for _tag, p in procs:
        try:
            remain = max(0.5, deadline - time.time())
            p.wait(timeout=remain)
        except Exception:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass


def _port_in_use(host: str, port: int) -> bool:
    """探测端口是否已被监听（用于启动前自检，避免撞端口后疯狂重启）。"""
    import socket
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        return s.connect_ex((probe_host, port)) == 0


def _pid_listening_on(port: int) -> list[int]:
    """返回正在监听指定端口的 pid 列表（仅 Linux；失败返回空列表）。"""
    pids: list[int] = []
    try:
        import subprocess as _sp
        # -lntp: 只列 LISTEN、不做域名解析、显示进程
        out = _sp.run(["ss", "-lntpH", f"sport = :{port}"],
                      capture_output=True, text=True, timeout=5).stdout
        import re as _re
        for m in _re.finditer(r"pid=(\d+)", out):
            pid = int(m.group(1))
            if pid not in pids:
                pids.append(pid)
    except Exception:
        pass
    return pids


def _is_our_instance(pid: int) -> bool:
    """该 pid 是否就是本项目的服务进程（工作目录 == 本项目根目录）。"""
    try:
        return os.path.realpath(f"/proc/{pid}/cwd") == os.path.realpath(str(ROOT))
    except Exception:
        return False


#: 重启场景下，等待旧实例释放端口的最长时间（秒）。
#: 宝塔面板的「重启」是 stop → sleep(1) → start：只要旧进程优雅退出略慢于 1 秒，
#: 新进程启动时端口就仍被占用。给一段等待窗口，重启才不会误判失败。
_RESTART_WAIT = 25.0


def _ensure_port_free(host: str, port: int) -> None:
    """启动前检查端口，避免「撞端口 → 无限重启」的重启风暴。

    线上症状：systemd 与宝塔面板同时在拉起本服务，后启动的因
    `address already in use` 立刻退出，而 `Restart=always` 让它每 5 秒重试，
    累计重启 12603 次，日志被刷爆。这里在 bind 之前就明确判断：

      * 端口空闲              → 正常启动；
      * 被**本项目的旧实例**占用 → 说明是「重启」：结束旧实例、等端口释放后接管；
      * 被**其它进程**占用      → 明确报错并退出，绝不盲目重启、更不误杀别人。

    为什么要主动接管而不是直接退出：实测发现，宝塔停止项目时若只 kill 了
    `main.py` 而没能带走它派生的 uvicorn 子进程，端口就会被这个孤儿一直占着。
    此时新实例若只是「退出报错」，面板会显示启动失败，且 pid 文件记录的是
    已死的进程 —— 之后每次「停止」都杀不掉真正在跑的 uvicorn，形成死结。
    主动接管可以自愈这种情况。
    """
    if not _port_in_use(host, port):
        return

    pids = _pid_listening_on(port)
    ours = [p for p in pids if _is_our_instance(p)]

    # 情况一：被本项目的旧实例占用 —— 这是一次重启，接管它
    if ours and len(ours) == len(pids):
        _log(f"[main] 端口 {port} 被本项目的旧实例占用（pid {', '.join(map(str, ours))}），"
             f"按「重启」处理：先结束旧实例…")
        for pid in ours:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    break
                except Exception:
                    break
                time.sleep(1.0 if sig == signal.SIGTERM else 0.2)
                if not _port_in_use(host, port):
                    break
        deadline = time.time() + _RESTART_WAIT
        while time.time() < deadline and _port_in_use(host, port):
            time.sleep(0.5)
        if _port_in_use(host, port):
            _log(f"❌ 旧实例在 {_RESTART_WAIT:.0f}s 内仍未释放端口 {port}，本次不启动。")
            _log("    → 请手动检查后重试。")
            sys.exit(3)
        _log("[main] 旧实例已结束，端口已释放，继续启动。")
        return

    # 情况二：被其它进程占用 —— 绝不动它，直接失败
    _log(f"❌ 端口 {port} 已被占用，无法启动（当前已有实例在运行）。")
    if pids:
        _log(f"    占用进程 pid: {', '.join(str(p) for p in pids)}")
        for pid in pids:
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmdline = f.read().replace(b"\0", b" ").decode(errors="ignore").strip()
                if cmdline:
                    _log(f"    pid {pid}: {cmdline}")
            except Exception:
                pass
        _log(f"    → 若确认要重启，请先停掉旧进程： kill {' '.join(str(p) for p in pids)}")
    _log(f"    → 或改用其他端口： python main.py --port {port + 1}")
    _log("    提示：同一端口只应由一个管理器负责（systemd 或宝塔面板，二选一）。")
    sys.exit(3)


def main() -> None:
    # 依赖自检：当前解释器缺包则自动切换到带依赖的虚拟环境（修复系统 Python 缺 pymysql 导致启动即崩退出）
    py = _interpreter_with_deps()
    if py is None:
        _log("❌ 当前 Python 缺少依赖：" + ", ".join(_REQUIRED_DEPS))
        _log("   请先安装：pip install -r requirements.txt，或激活已装好依赖的虚拟环境后再运行。")
        sys.exit(2)
    if py != sys.executable:
        _log(f"[main] 当前解释器缺依赖，自动改用虚拟环境：{py}")
        os.execv(py, [py, os.path.abspath(__file__)] + sys.argv[1:])

    ap = argparse.ArgumentParser(description="workbuddy2api 一键启动（单端口：管理后台 + 内嵌网关）")
    ap.add_argument("--host", default="0.0.0.0", help="监听地址（默认 0.0.0.0）")
    ap.add_argument("--port", type=int, default=8790, help="服务端口（默认 8790）")
    args = ap.parse_args()

    # 端口自检：已有实例在跑就明确退出，绝不反复撞端口（线上重启风暴的根因）。
    _check_port = int(os.getenv("ADMIN_PORT", str(args.port)))
    _check_host = os.getenv("ADMIN_HOST", args.host)
    _ensure_port_free(_check_host, _check_port)

    # 部署安全检查
    secret = os.getenv("ADMIN_JWT_SECRET", "")
    if not secret or secret.startswith("workbuddy-admin-jwt-secret-please-change"):
        _log("⚠️  未设置强 ADMIN_JWT_SECRET，将使用默认弱密钥 —— 部署请务必通过环境变量覆盖！")
    if not os.getenv("ADMIN_PASSWORD", "") or not os.getenv("ADMIN_USERNAME", ""):
        _log("⚠️  未配置 ADMIN_USERNAME / ADMIN_PASSWORD，后台登录将返回 503。")
        _log("    请在 .env 中设置这两项后重启（已不再提供 admin/admin123 默认口令）。")

    procs: list[tuple[str, subprocess.Popen]] = []
    stop = threading.Event()

    def _launch(tag: str, cmd: list[str], port: int, host: str, logfile: Path) -> None:
        _log(f"[main] 启动 {tag} (http://{host}:{port}) : {' '.join(cmd)}")
        p = subprocess.Popen(
            cmd, cwd=str(ROOT),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            # 独立进程组：停止时能连同子进程一起收掉，不会留下占着端口的孤儿。
            start_new_session=True,
            text=True, bufsize=1, env=os.environ.copy(),
        )
        procs.append((tag, p))
        threading.Thread(target=_pump, args=(p.stdout, logfile, tag), daemon=True).start()

    _launch("admin", *_build_admin_cmd(args), LOGS / "admin.log")

    # 自己写 pid 文件：外部启动器（宝塔 cmd.sh / runuser 包装 / systemd）拿到的
    # `$!` 未必是本进程的真实 pid（例如经 runuser 包装时记录的是包装进程）。
    # 「停止」按钮按这份 pid 去杀，就会杀错对象、留下仍占着 8790 的 uvicorn 孤儿，
    # 之后每次启动都撞端口。由进程自己写，才与「真正在跑的服务」一一对应。
    _write_pidfile()

    def _shutdown(signum, _frame) -> None:
        _log(f"\n[main] 收到信号 {signum}，正在关闭…")
        stop.set()
        _kill_children(procs)
        _remove_pidfile()

    signal.signal(signal.SIGINT, _shutdown)
    try:
        signal.signal(signal.SIGTERM, _shutdown)
    except Exception:
        pass

    admin_port = os.getenv("ADMIN_PORT", str(args.port))
    admin_host = os.getenv("ADMIN_HOST", args.host)
    _log(f"[main] 单端口服务已启动：")
    _log(f"       管理后台   : http://{admin_host}:{admin_port}/admin")
    _log(f"       托管网关   : http://{admin_host}:{admin_port}/v1/chat/completions  (带 Key 配额)")
    _log(f"       内嵌网关   : http://{admin_host}:{admin_port}/gw/v1/...            (桌面登录态 / responses / messages)")
    _log("[main] 按 Ctrl+C 停止。")

    # 主循环：子进程异常退出则整体退出，避免孤儿进程
    while not stop.is_set():
        for tag, p in list(procs):
            rc = p.poll()
            if rc is not None and not stop.is_set():
                _log(f"[main] ❌ {tag} 已退出 (code={rc})，关闭服务…")
                stop.set()
                _kill_children(procs)
                _remove_pidfile()
                sys.exit(rc if rc != 0 else 1)
        time.sleep(0.5)

    _kill_children(procs)
    _remove_pidfile()
    _log("[main] 已停止。")


if __name__ == "__main__":
    main()
