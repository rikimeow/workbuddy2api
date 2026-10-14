"""Android loopback HTTP policy; desktop and hosted Uvicorn are unaffected."""
import asyncio
from collections import deque
import time

import uvicorn
from uvicorn.protocols.http.h11_impl import H11Protocol


class LoopHealth:
    def __init__(self):
        self.samples = deque(maxlen=240)

    async def watch(self):
        while True:
            began = time.perf_counter()
            await asyncio.sleep(.25)
            ended = time.perf_counter()
            self.samples.append((began, ended, max(0, ended - began - .25)))

    def since(self, began):
        return {'event_loop_lag_ms': round(max((lag for start, end, lag in tuple(self.samples)
                                              if end >= began), default=0) * 1000, 1)}


class MobileServer(uvicorn.Server):
    async def serve(self, sockets=None):
        self.config.mobile_loop_health = LoopHealth()
        monitor = asyncio.create_task(self.config.mobile_loop_health.watch())
        try:
            await super().serve(sockets=sockets)
        finally:
            monitor.cancel()
            try:
                await monitor
            except asyncio.CancelledError:
                pass


class MobileH11Protocol(H11Protocol):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        original_app = self.app
        self.first_byte_at = None

        async def mobile_app(scope, receive, send):
            # Only API connections are short lived. A localhost TCP handshake
            # avoids stale sockets in Android HttpURLConnection's idle pool.
            is_api = scope.get('path', '').startswith('/v1/')
            async def mobile_send(message):
                if is_api and message['type'] == 'http.response.start':
                    headers = [(k, v) for k, v in message.get('headers', []) if k.lower() != b'connection']
                    message = {**message, 'headers': headers + [(b'connection', b'close')]}
                await send(message)
            await original_app(scope, receive, mobile_send)
        self.app = mobile_app

    def data_received(self, data):
        if self.first_byte_at is None:
            self.first_byte_at = time.perf_counter()
        super().data_received(data)

    def handle_events(self):
        super().handle_events()
        scope = getattr(self, 'scope', None)
        if scope is not None and '_mobile_http' not in scope:
            now = time.perf_counter()
            health = getattr(self.config, 'mobile_loop_health', None)
            scope['_mobile_http'] = {'received_at': self.first_byte_at or now,
                                     'headers_at': now, 'health': health}

    def on_response_complete(self):
        self.first_byte_at = None
        super().on_response_complete()
