"""Embedded API runtime. No subprocess, desktop installer, Node or device scanning."""
from __future__ import annotations

import json
import errno
import logging
import os
import secrets
import socket
import threading
import time
import urllib.request
from pathlib import Path
from logging.handlers import RotatingFileHandler

_server = None
_thread = None
_state = None
_lock = threading.Lock()


def start(directory: str) -> str:
    global _server, _thread, _state
    with _lock:
        if _thread and _thread.is_alive() and _state:
            return json.dumps(_state)
        if _thread and _thread.is_alive():
            raise RuntimeError("旧服务尚未停止，请稍后重试")
        from desktop.common import atomic_json
        base = Path(directory) / "WorkBuddy2API"
        base.mkdir(parents=True, exist_ok=True)
        path = base / "config.json"
        if path.is_file():
            config = json.loads(path.read_text(encoding="utf-8"))
        else:
            config = {"port": 8790, "username": "mobile", "password": secrets.token_urlsafe(32),
                      "jwt_secret": secrets.token_urlsafe(48)}
            atomic_json(path, config)
        nonce = secrets.token_urlsafe(32)
        os.environ.update({"WORKBUDDY_ANDROID": "1", "WB_DESKTOP_BOOT": nonce,
                           "ADMIN_DATABASE_URL": "sqlite:///" + (base / "workbuddy.db").as_posix(),
                           "ADMIN_GATEWAY_LIMIT_BACKEND": "memory", "ADMIN_HOST": "127.0.0.1",
                           "ADMIN_USERNAME": config["username"], "ADMIN_PASSWORD": config["password"],
                           "ADMIN_JWT_SECRET": config["jwt_secret"], "ADMIN_DEV_TOOLS": "0"})
        logs = base / "logs"
        logs.mkdir(exist_ok=True)
        handler = RotatingFileHandler(logs / "mobile.log", maxBytes=4*1048576, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
        import uvicorn
        from mobile_http import MobileServer, MobileH11Protocol
        from admin.server import app
        sock = socket.socket()
        try:
            # Reuse our listening port after accepted connections enter TIME_WAIT.
            # SO_REUSEADDR does not permit sharing an active TCP listener.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            preferred = int(config.get("port", 8790))
            if not 1 <= preferred <= 65535:
                preferred = 8790
            try:
                sock.bind(("127.0.0.1", preferred))
            except OSError as error:
                # Windows' exclusive listeners report WSAEACCES in host tests.
                if error.errno != errno.EADDRINUSE and not (
                        os.name == "nt" and getattr(error, "winerror", None) == 10013):
                    raise
                sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            sock.listen(128)
            server = MobileServer(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None,
                                    access_log=False, loop="asyncio", http=MobileH11Protocol, ws="none",
                                    timeout_graceful_shutdown=5))
            _server = server
            def serve():
                try:
                    server.run(sockets=[sock])
                finally:
                    sock.close()
            _thread = threading.Thread(target=serve, daemon=True, name="mobile-local-api")
            _thread.start()
            origin = f"http://127.0.0.1:{port}"
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline and _thread.is_alive():
                try:
                    request = urllib.request.Request(origin + "/api/app/desktop-ready", headers={"X-Desktop-Boot": nonce})
                    with opener.open(request, timeout=1) as response:
                        if json.load(response).get("ready"):
                            break
                except OSError:
                    time.sleep(.1)
            else:
                raise RuntimeError("本地服务启动失败，请重新打开应用")
            if config.get("port") != port:
                config["port"] = port
                atomic_json(path, config)
            import jwt
            _state = {"origin": origin, "token": jwt.encode({"sub": config["username"], "exp": int(time.time()) + 86400},
                      config["jwt_secret"], algorithm="HS256")}
            return json.dumps(_state)
        except Exception:
            if _server:
                _server.should_exit = True
            if _thread and _thread.is_alive():
                _thread.join(10)
            sock.close()
            raise


def stop():
    global _state, _server, _thread
    with _lock:
        if _server:
            _server.should_exit = True
        if _thread:
            _thread.join(10)
            if _thread.is_alive():
                raise RuntimeError("本地服务仍在停止中，保留原端口等待退出")
        _state = None
        _server = None
        _thread = None


def session_token() -> str:
    with _lock:
        if not _state:
            return ""
        import jwt
        from admin.config import settings
        return jwt.encode({"sub": settings.ADMIN_USERNAME, "exp": int(time.time()) + 86400},
                          settings.JWT_SECRET, algorithm="HS256")
