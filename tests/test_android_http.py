"""Use real TCP sockets: ASGI-only tests cannot check HTTP keepalive/framing."""
import asyncio
import importlib.util
from pathlib import Path
import socket
import time
import unittest

import uvicorn
from admin import request_metrics

spec = importlib.util.spec_from_file_location('mobile_http', Path(__file__).resolve().parents[1] / 'android/python/mobile_http.py')
mobile_http = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mobile_http)


class AndroidHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async def app(scope, receive, send):
            if scope['type'] != 'http':
                return
            body = b''
            while True:
                message = await receive()
                body += message.get('body', b'')
                if not message.get('more_body'):
                    break
            stream = scope['path'].endswith('/stream')
            await send({'type':'http.response.start', 'status':200, 'headers':
                        [(b'content-type', b'text/event-stream')] if stream else [(b'content-length', str(len(body) or 2).encode())]})
            if stream:
                await send({'type':'http.response.body', 'body':b'data: first\n\n', 'more_body':True})
                await asyncio.sleep(.03)
                await send({'type':'http.response.body', 'body':b'data: [DONE]\n\n'})
            else:
                await send({'type':'http.response.body', 'body':body or b'OK'})
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        self.port = sock.getsockname()[1]
        self.server = mobile_http.MobileServer(uvicorn.Config(request_metrics.TimingMiddleware(app),
            http=mobile_http.MobileH11Protocol, loop='asyncio', lifespan='off', log_level='error'))
        self.task = asyncio.create_task(self.server.serve(sockets=[sock]))
        while not self.server.started:
            if self.task.done():
                await self.task
            await asyncio.sleep(.01)

    async def asyncTearDown(self):
        self.server.should_exit = True
        await asyncio.wait_for(self.task, 5)

    async def connect(self):
        return await asyncio.open_connection('127.0.0.1', self.port)

    async def test_api_closes_only_after_complete_body_and_stream_terminal(self):
        for path, body in (('/v1/echo', b'x'*1048576), ('/v1/stream', b'')):
            reader, writer = await self.connect()
            writer.write(('POST '+path+' HTTP/1.1\r\nHost: localhost\r\nConnection: keep-alive\r\nContent-Length: '+str(len(body))+'\r\n\r\n').encode()+body)
            await writer.drain()
            headers = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 3)
            self.assertIn(b'connection: close', headers.lower())
            data = await asyncio.wait_for(reader.read(), 3)
            self.assertIn(body if body else b'data: [DONE]', data)
            if body:
                self.assertEqual(len(data), len(body))
            writer.close()
            await writer.wait_closed()
        row = request_metrics.snapshot()['recent'][0]
        self.assertGreaterEqual(row['ingress_ms'], row['asgi_queue_ms'])
        self.assertIn('event_loop_lag_ms', row)

    async def test_admin_connection_still_reusable(self):
        reader, writer = await self.connect()
        for _ in range(2):
            writer.write(b'GET /admin HTTP/1.1\r\nHost: localhost\r\n\r\n')
            await writer.drain()
            headers = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 3)
            self.assertNotIn(b'connection: close', headers.lower())
            self.assertEqual(await reader.readexactly(2), b'OK')
        writer.close()
        await writer.wait_closed()

    async def test_monitor_exposes_real_loop_stall(self):
        # Controlled stall validates the monitor, not the user's root cause.
        await asyncio.sleep(.05)
        began = time.perf_counter()
        time.sleep(.35)
        await asyncio.sleep(.01)
        self.assertGreater(self.server.config.mobile_loop_health.since(began)['event_loop_lag_ms'], 100)


if __name__ == '__main__':
    unittest.main()
