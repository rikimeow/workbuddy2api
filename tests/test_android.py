"""Android staging and real mobile runtime checks; no account or upstream requests."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("build_android", ROOT / "scripts/build_android.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class AndroidBundleTests(unittest.TestCase):
    def test_restart_reuses_port_after_server_closes_keepalive_clients(self):
        builder.prepare()
        with tempfile.TemporaryDirectory() as work:
            base = Path(work) / 'WorkBuddy2API'
            base.mkdir()
            with socket.socket() as free:
                free.bind(('127.0.0.1', 0))
                port = free.getsockname()[1]
            (base/'config.json').write_text(json.dumps({
                'port': port, 'username': 'mobile', 'password': 'test-only',
                'jwt_secret': 'test-only-secret'}), encoding='utf-8')
            script = r'''
import json, socket, sys
import android_runtime
expected = int(sys.argv[2])
try:
    for attempt in range(4):
        state = json.loads(android_runtime.start(sys.argv[1]))
        port = int(state['origin'].rsplit(':', 1)[1])
        assert port == expected, (attempt, expected, port)
        client = socket.create_connection(('127.0.0.1', port), timeout=5)
        try:
            client.sendall(b'GET /api/app/version HTTP/1.1\r\nHost: localhost\r\nConnection: keep-alive\r\n\r\n')
            assert b'200 OK' in client.recv(8192)
            android_runtime.stop()
        finally:
            client.close()
finally:
    android_runtime.stop()
'''
            env = dict(os.environ, PYTHONUTF8='1')
            for key in list(env):
                if key.startswith(('ADMIN_', 'CONVERTER_', 'WORKBUDDY_', 'WB_DESKTOP_')):
                    env.pop(key)
            result = subprocess.run([sys.executable, '-c', script, work, str(port)],
                cwd=builder.STAGING, env=env, capture_output=True, text=True, timeout=90,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_mobile_bundle_omits_desktop_pages_and_scan_tools(self):
        html = builder.mobile_html((ROOT / "admin/static/index.html").read_text(encoding="utf-8"))
        for excluded in ('id="panel-client"', 'id="appSourceBlock"', 'data-tab="client"',
                         'onclick="scanLocal()"', 'async function scanLocal', 'async function injectAcc',
                         'function loadClientProfile', 'function loadAppSource'):
            self.assertNotIn(excluded, html)
        self.assertIn('href="/join"', html)
        self.assertIn('watchJoinAuthorization()', html)
        if shutil.which("node"):
            import re
            scripts = re.findall(r'<script(?:\s[^>]*)?>(.*?)</script>', html, re.S)
            result = subprocess.run([shutil.which("node"), "-e",
                                     "const vm=require('node:vm'); for(const s of JSON.parse(require('node:fs').readFileSync(0,'utf8'))) new vm.Script(s);"],
                                    input=json.dumps(scripts), text=True, capture_output=True,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_mobile_runtime_is_persistent_and_has_no_desktop_routes(self):
        builder.prepare()
        with tempfile.TemporaryDirectory() as work, socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            original = occupied.getsockname()[1]
            base = Path(work) / "WorkBuddy2API"
            base.mkdir()
            config = {"port": original, "username": "mobile", "password": "test-only-password",
                      "jwt_secret": "test-only-secret-not-a-real-credential"}
            (base / "config.json").write_text(json.dumps(config), encoding="utf-8")
            script = r'''
import json, sys, time
from pathlib import Path
import android_runtime
state = android_runtime.start(sys.argv[1])
Path(sys.argv[1], 'ready.json').write_text(state, encoding='utf-8')
while not Path(sys.argv[1], 'stop').exists(): time.sleep(.05)
android_runtime.stop()
'''
            saved = None
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            for attempt in range(2):
                (Path(work) / "ready.json").unlink(missing_ok=True)
                (Path(work) / "stop").unlink(missing_ok=True)
                env = dict(os.environ, PYTHONUTF8="1")
                for key in list(env):
                    if key.startswith(("ADMIN_", "CONVERTER_", "WORKBUDDY_", "WB_DESKTOP_")):
                        env.pop(key)
                with (Path(work) / "test.log").open("wb") as output:
                    process = subprocess.Popen([sys.executable, "-c", script, work], cwd=builder.STAGING,
                                              env=env, stdout=output, stderr=output,
                                              creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                    try:
                        ready = Path(work) / "ready.json"
                        deadline = time.monotonic() + 35
                        while not ready.exists() and time.monotonic() < deadline:
                            if process.poll() is not None:
                                self.fail((Path(work) / "test.log").read_text(encoding="utf-8", errors="replace"))
                            time.sleep(.1)
                        self.assertTrue(ready.is_file())
                        state = json.loads(ready.read_text(encoding="utf-8"))
                        port = int(state['origin'].rsplit(':', 1)[1])
                        self.assertNotEqual(port, original)
                        if saved is not None:
                            self.assertEqual(port, saved)
                        saved = port
                        self.assertEqual(json.loads((base / 'config.json').read_text())['port'], port)
                        self.assertTrue((base / 'workbuddy.db').is_file())
                        with opener.open(state['origin'] + '/join', timeout=5) as response:
                            self.assertIn(b'/api/join/official/start', response.read())
                        for path in ('/api/accounts/scan-local', '/api/accounts/1/inject', '/api/client-profile', '/api/app-source'):
                            request = urllib.request.Request(state['origin'] + path, headers={'X-Admin-Token': state['token']},
                                                             method='POST' if path.endswith('/inject') else 'GET')
                            with self.assertRaises(urllib.error.HTTPError) as denied:
                                opener.open(request, timeout=5)
                            self.assertIn(denied.exception.code, (404, 405), path)
                        with opener.open(urllib.request.Request(state['origin'] + '/api/accounts',
                                          headers={'X-Admin-Token': state['token']}), timeout=5) as response:
                            self.assertEqual(response.status, 200)
                        with self.assertRaises(urllib.error.HTTPError) as denied:
                            opener.open(state['origin'] + '/v1/models', timeout=5)
                        self.assertEqual(denied.exception.code, 401)
                    finally:
                        (Path(work) / 'stop').touch()
                        try:
                            process.wait(timeout=15)
                        except subprocess.TimeoutExpired:
                            if os.name == 'nt':
                                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True,
                                               creationflags=subprocess.CREATE_NO_WINDOW)
                            else:
                                process.kill()
                            process.wait(timeout=5)
                self.assertEqual(process.returncode, 0)


if __name__ == '__main__':
    unittest.main()
