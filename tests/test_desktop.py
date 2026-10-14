"""Offline update contract + fresh SQLite backend integration. No production credentials."""
from __future__ import annotations

import hashlib
import json
import os
import socket
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from desktop.common import REPOSITORY, VERSION
from desktop.updates import UpdateService, newer, release_url


class UpdateTests(unittest.TestCase):
    def test_source_backend_survives_missing_update_dependency(self):
        # A source installation predating packaging must still serve the gateway.
        # Use a fresh process so an already-imported dependency cannot hide this.
        script = r'''
import importlib.abc
import sys
class MissingPackaging(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'packaging' or fullname.startswith('packaging.'):
            raise ModuleNotFoundError("No module named 'packaging'", name='packaging')
sys.meta_path.insert(0, MissingPackaging())
from admin.server import app
from admin.security import require_admin
import asyncio
import httpx
app.dependency_overrides[require_admin] = lambda: True
async def check():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        assert (await client.get('/admin')).status_code == 200
        assert (await client.get('/api/app/version')).status_code == 200
        assert (await client.get('/v1/models')).status_code == 401
        response = await client.get('/api/app/releases')
        assert response.status_code == 503, response.text
        assert 'packaging' in response.json()['detail']
        assert (await client.get('/api/app/version')).status_code == 200
asyncio.run(check())
print('source backend survives missing packaging; update endpoint returns 503')
'''
        with tempfile.TemporaryDirectory() as work:
            env = dict(os.environ)
            for key in list(env):
                if key.startswith(('ADMIN_', 'CONVERTER_', 'WORKBUDDY_')):
                    env.pop(key)
            env.update(ADMIN_DATABASE_URL='sqlite:///' + (Path(work) / 'test.db').as_posix(),
                       ADMIN_REDIS_URL='', ADMIN_GATEWAY_LIMIT_BACKEND='memory',
                       ADMIN_JWT_SECRET='offline-test-secret-with-at-least-32-bytes',
                       PYTHONUTF8='1')
            result = subprocess.run([sys.executable, '-c', script], cwd=ROOT,
                                    env=env, capture_output=True, text=True,
                                    encoding='utf-8', timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_stable_versions(self):
        self.assertTrue(newer("v1.10.0", "1.9.0"))
        for tag in ("v1.9.0", "v1.8.0", "v2.0.0rc1", "nightly"):
            self.assertFalse(newer(tag, "1.9.0"))

    def test_repo_url_boundary(self):
        release_url(f"https://github.com/{REPOSITORY}/releases/download/v2.0.0/a.exe")
        for url in ("http://github.com/" + REPOSITORY + "/releases/a",
                    "https://github.com.evil.test/" + REPOSITORY + "/releases/a",
                    "https://github.com/other/repo/releases/a"):
            with self.assertRaises(ValueError):
                release_url(url)

    def test_checksum_prevents_replacement(self):
        with tempfile.TemporaryDirectory() as work:
            service = UpdateService(Path(work))
            service.releases = [{"id": 1, "tag": "v99.0.0", "installable": True,
                "asset": {"digest": "sha256:" + "a"*64,
                          "browser_download_url": f"https://github.com/{REPOSITORY}/releases/download/v99/a.exe"},
                "checksum": None}]
            def fake_download(url, target, progress):
                target.write_bytes(b"MZtest")
                return hashlib.sha256(b"MZtest").hexdigest()
            with patch("desktop.updates.download", fake_download):
                with self.assertRaisesRegex(ValueError, "校验失败"):
                    service.stage(1)
            self.assertFalse((Path(work) / "updates/1.exe").exists())
            self.assertFalse((Path(work) / "updates/1.part").exists())
            service.releases[0]["asset"]["digest"] = "sha256:" + hashlib.sha256(b"MZtest").hexdigest()
            with patch("desktop.updates.download", fake_download):
                self.assertEqual(service.stage(1).read_bytes(), b"MZtest")

    def test_failed_check_preserves_history(self):
        with tempfile.TemporaryDirectory() as work:
            service = UpdateService(Path(work))
            service.releases = [{"id": 1, "tag": "v99.0.0"}]
            with patch("urllib.request.urlopen", side_effect=OSError("offline")):
                result = service.check()
            self.assertTrue(result["available"])
            self.assertIn("offline", result["error"])
            self.assertEqual(len(result["releases"]), 1)


class PortTests(unittest.TestCase):
    def test_restart_keeps_saved_port_and_falls_back_only_if_occupied(self):
        from desktop.app import select_port, save_port
        with tempfile.TemporaryDirectory() as work:
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                preferred = reservation.getsockname()[1]
            self.assertEqual(select_port(preferred), preferred)
            with socket.socket() as occupied:
                occupied.bind(("127.0.0.1", preferred))
                occupied.listen()
                fallback = select_port(preferred)
                self.assertNotEqual(fallback, preferred)
                config = {"port": preferred, "jwt_secret": "retain-this-value"}
                save_port(Path(work), config, fallback)
                persisted = json.loads((Path(work) / "config.json").read_text(encoding="utf-8"))
                self.assertEqual(persisted["jwt_secret"], "retain-this-value")
                self.assertEqual(select_port(persisted["port"]), fallback)
            # Even after the original port becomes free, retain the last successful API URL.
            self.assertEqual(select_port(persisted["port"]), fallback)


class UIRegressionTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is only needed for this build-time JS check")
    def test_phone_login_then_credit_details_resets_width(self):
        # Exercise the real function after the narrow phone-login dialog, then reopen details.
        html = (ROOT / "admin/static/index.html").read_text(encoding="utf-8")
        function = html[html.index("function openModal("):html.index("function closeModal()")]
        script = r'''
          const vm=require('node:vm'), assert=require('node:assert/strict');
          function classes(initial=[]){const set=new Set(initial);return {
            add(...items){items.forEach(x=>set.add(x))},remove(...items){items.forEach(x=>set.delete(x))},
            contains(x){return set.has(x)},[Symbol.iterator](){return set.values()}}}
          const box={classList:classes(['max-w-6xl']),style:{setProperty(k,v){this[k]=v}},
            setAttribute(){},scrollTop:100}, overlay={classList:classes(['hidden'])};
          const context={$:s=>s==='#modalBox'?box:overlay};vm.createContext(context);
          vm.runInContext(FUNCTION,context);
          context.openModal('phone','max-w-md');
          assert.equal(box.style['--modal-width'],'480px');
          context.openModal('credits');
          assert.equal(box.style['--modal-width'],'1120px');
          assert.equal(box.classList.contains('max-w-md'),false);
          assert.equal(box.classList.contains('max-w-6xl'),true);
          assert.equal(box.scrollTop,0);
          assert.equal(overlay.classList.contains('flex'),true);
        '''.replace("FUNCTION", json.dumps(function))
        subprocess.run([shutil.which("node"), "-e", script], check=True, capture_output=True)

    def test_native_bridge_exposes_only_methods(self):
        from desktop.app import Bridge
        bridge = Bridge(object())
        public = [name for name in dir(bridge) if not name.startswith("_")]
        self.assertTrue(public)
        self.assertTrue(all(callable(getattr(bridge, name)) for name in public))


class FreshBackendTests(unittest.TestCase):
    def test_blank_machine_backend(self):
        with tempfile.TemporaryDirectory() as work:
            base = Path(work)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            env = dict(os.environ, WORKBUDDY_DESKTOP_DATA=work, PYTHONUTF8="1")
            # Explicitly disable inherited production services and credentials in the child.
            for key in list(env):
                if key.startswith(("ADMIN_", "CONVERTER_", "WB_DESKTOP_PARENT")):
                    env.pop(key)
            output = (base / "test.log").open("wb")
            process = subprocess.Popen([sys.executable, str(ROOT / "desktop_entry.py"), "--backend", str(port)],
                                       env=env, stdout=output, stderr=output,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            try:
                url = f"http://127.0.0.1:{port}"
                for _ in range(100):
                    if process.poll() is not None:
                        self.fail((base / "test.log").read_text(encoding="utf-8", errors="replace"))
                    try:
                        with urllib.request.urlopen(url + "/api/app/version", timeout=.5) as response:
                            self.assertEqual(json.load(response)["version"], VERSION)
                        break
                    except OSError:
                        time.sleep(.1)
                else:
                    self.fail("Fresh backend never became ready")
                config = json.loads((base / "config.json").read_text(encoding="utf-8"))
                self.assertNotEqual(config["password"], "admin123")
                body = urllib.parse.urlencode({"username": config["username"], "password": config["password"]}).encode()
                with urllib.request.urlopen(urllib.request.Request(url + "/api/login", data=body)) as response:
                    token = json.load(response)["token"]
                for endpoint in ("/api/accounts", "/api/keys", "/api/models/configs", "/api/schedules",
                                 "/api/stats/usage?granularity=hour", "/api/stats/usage?granularity=day"):
                    request = urllib.request.Request(url + endpoint, headers={"X-Admin-Token": token})
                    try:
                        with urllib.request.urlopen(request, timeout=10) as response:
                            self.assertEqual(response.status, 200, endpoint)
                            json.load(response)
                    except urllib.error.HTTPError as exc:
                        self.fail(f"{endpoint}: {exc.code} {exc.read().decode()}\n" +
                                  (base / "test.log").read_text(encoding="utf-8", errors="replace")[-3000:])
                with self.assertRaises(urllib.error.HTTPError) as denied:
                    urllib.request.urlopen(url + "/api/accounts")
                self.assertEqual(denied.exception.code, 401)
                # gw is mounted, rejects unauthenticated calls rather than exposing the local account.
                with self.assertRaises(urllib.error.HTTPError) as denied:
                    urllib.request.urlopen(url + "/gw/v1/models")
                self.assertEqual(denied.exception.code, 401)
                self.assertTrue((base / "workbuddy.db").is_file())
            finally:
                if os.name == "nt":
                    # A venv's python.exe launcher may have a real interpreter child.
                    subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                                   capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
                else:
                    process.terminate()
                process.wait(timeout=15)
                output.close()


if __name__ == "__main__":
    unittest.main()
