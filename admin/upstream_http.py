"""Share TLS configuration while keeping each request's cookies isolated."""
from contextlib import asynccontextmanager
import ssl
import threading

import certifi
import httpx
from admin import request_metrics

_ssl_context = None
_ssl_lock = threading.Lock()


def ssl_context():
    global _ssl_context
    with _ssl_lock:
        if _ssl_context is None:
            _ssl_context = ssl.create_default_context(cafile=certifi.where())
        return _ssl_context


class _BorrowedTransport(httpx.AsyncBaseTransport):
    def __init__(self, transport):
        self.transport = transport

    async def handle_async_request(self, request):
        # Attach timings to the actual request; no URLs, headers or bodies retained.
        if 'trace' not in request.extensions:
            sync_trace = request_metrics.network_trace()
            async def trace(name, info):
                sync_trace(name, info)
            request.extensions['trace'] = trace
        return await self.transport.handle_async_request(request)

    async def aclose(self):
        # The application owns the pool, not an individual request.
        pass


async def start(app, limits):
    # Reuse a few recent connections instead of doing DNS/TCP/TLS on every
    # mobile call. HTTPX discards expired/closed sockets; do not retry POSTs
    # automatically as a conversation may already have reached the upstream.
    mobile_limits = httpx.Limits(max_connections=limits.max_connections,
                               max_keepalive_connections=4, keepalive_expiry=30)
    app.state.upstream_transport = httpx.AsyncHTTPTransport(limits=mobile_limits, verify=ssl_context())


async def stop(app):
    transport = getattr(app.state, 'upstream_transport', None)
    app.state.upstream_transport = None
    if transport is not None:
        await transport.aclose()


@asynccontextmanager
async def client(request, timeout, limits):
    # Direct route tests can create a Request without an application in scope.
    app = request.scope.get('app') if hasattr(request, 'scope') else getattr(request, 'app', None)
    transport = getattr(getattr(app, 'state', None), 'upstream_transport', None)
    kwargs = {'timeout': timeout, 'limits': limits}
    if transport is not None:
        kwargs['transport'] = _BorrowedTransport(transport)
    async with httpx.AsyncClient(**kwargs) as connection:
        yield connection
