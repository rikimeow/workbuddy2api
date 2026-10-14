"""Bounded local timing records. Never retain headers, credentials or bodies."""
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import threading
import time
import uuid

current = ContextVar('local_request_timing', default=None)
_lock = threading.Lock()
_recent = deque(maxlen=40)
_active = {}


def add_stage(name, milliseconds):
    record = current.get()
    if record is not None:
        with _lock:
            if name in ('client_send_wait', 'client_upload_wait'):
                existing = next((item for item in record['stages'] if item['name'] == name), None)
                if existing is not None:
                    existing['ms'] = round(existing['ms'] + milliseconds, 1)
                    return
            if len(record['stages']) < 80:
                record['stages'].append({'name': name, 'ms': round(milliseconds, 1)})


@contextmanager
def measure(name):
    start = time.perf_counter()
    set_phase(name)
    try:
        yield
    finally:
        add_stage(name, (time.perf_counter()-start)*1000)


def network_trace():
    starts = {}
    def trace(name, info):
        if name.endswith('.started'):
            label = name.rsplit('.', 1)[0]
            starts[label] = time.perf_counter()
            set_phase(label)
            if label == 'http11.send_request_headers':
                record = current.get()
                if record is not None:
                    with _lock:
                        record['upstream_attempts'] = record.get('upstream_attempts', 0) + 1
        elif name.endswith(('.complete', '.failed')):
            label = name.rsplit('.', 1)[0]
            began = starts.pop(label, None)
            if began is not None:
                add_stage(label, (time.perf_counter()-began)*1000)
            if name.endswith('.failed'):
                record = current.get()
                if record is not None:
                    # Retain a fixed exception category, never exception text.
                    category = type(info.get('exception')).__name__
                    allowed = {'ConnectTimeout', 'ReadTimeout', 'WriteTimeout', 'PoolTimeout',
                               'ConnectError', 'ReadError', 'WriteError', 'RemoteProtocolError'}
                    if category != 'CancelledError':
                        with _lock:
                            record['network_error'] = category if category in allowed else 'TransportError'
    return trace


def mark_first_delta():
    record = current.get()
    if record is not None:
        with _lock:
            if record['first_delta_ms'] is None:
                record['first_delta_ms'] = round((time.perf_counter()-record['_perf_start'])*1000, 1)


def mark_terminal():
    record = current.get()
    if record is not None:
        with _lock:
            record['_terminal_ready'] = True


def mark_stream_error():
    record = current.get()
    if record is not None:
        with _lock:
            record['stream_error'] = True


def mark_request_error(kind):
    record = current.get()
    if record is not None:
        with _lock:
            record['request_error'] = kind


def mark_upstream_error(kind, status=None):
    record = current.get()
    allowed = {'hard_credit', 'soft_rate', 'model_rate', 'model_block', 'account_fault',
               'session_dead', 'waf', 'server', 'not_found', 'transport', 'client',
               'no_account', 'success', 'unknown', 'model_queue', 'retry_exhausted',
               'upstream_internal', 'model_behavior'}
    if record is not None:
        with _lock:
            record['upstream_error'] = kind if kind in allowed else 'unknown'
            if isinstance(status, int) and 100 <= status <= 599:
                record['upstream_status'] = status


def set_phase(name):
    record = current.get()
    if record is not None:
        with _lock:
            record['phase'] = name


def snapshot():
    def copy(record):
        result = {key: value for key, value in record.items() if not key.startswith('_')}
        result['stages'] = [dict(stage) for stage in record['stages']]
        health = record.get('_runtime_health')
        if health is not None:
            result.update(health.since(record['_perf_start']))
        result['elapsed_ms'] = record['total_ms'] if record['total_ms'] is not None else round((time.perf_counter()-record['_perf_start'])*1000, 1)
        return result
    with _lock:
        return {'active': [copy(record) for record in _active.values()],
                'recent': [copy(record) for record in reversed(_recent)]}


class TimingMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or not scope.get('path', '').startswith('/v1/'):
            return await self.app(scope, receive, send)
        start = time.perf_counter()
        record = {'id': uuid.uuid4().hex[:10], 'path': scope['path'],
                  'method': scope.get('method', ''),
                  'started_at': datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
                  'status': None, 'headers_ms': None, 'first_body_ms': None, 'total_ms': None,
                  'first_delta_ms': None, 'request_bytes': 0, 'response_bytes': 0,
                  'upload_complete': False, 'response_complete': False, 'terminal_sent': False,
                  'streaming': False,
                  'stream_error': False, 'request_error': None,
                  'outcome': 'active', 'phase': 'gateway_admission', '_perf_start': start,
                  'stages': []}
        connection = scope.get('_mobile_http')
        if connection:
            record['ingress_ms'] = round((start - connection['received_at']) * 1000, 1)
            record['asgi_queue_ms'] = round((start - connection['headers_at']) * 1000, 1)
            record['_runtime_health'] = connection['health']
        token = current.set(record)
        with _lock:
            _active[record['id']] = record
        async def timed_receive():
            began = time.perf_counter()
            reading_upload = not record['upload_complete']
            message = await receive()
            # StreamingResponse also calls receive() to watch for disconnects
            # throughout a reply. That wait is not request-body upload time.
            if reading_upload and message['type'] in ('http.request', 'http.disconnect'):
                add_stage('client_upload_wait', (time.perf_counter()-began)*1000)
            if message['type'] == 'http.request':
                record['request_bytes'] += len(message.get('body', b''))
                record['upload_complete'] = not message.get('more_body', False)
            elif message['type'] == 'http.disconnect':
                record['outcome'] = 'disconnected'
            return message
        async def timed_send(message):
            elapsed = round((time.perf_counter()-start)*1000, 1)
            if message['type'] == 'http.response.start':
                record['status'] = message['status']
                record['headers_ms'] = elapsed
                record['streaming'] = any(key.lower() == b'content-type' and b'text/event-stream' in value
                                          for key, value in message.get('headers', []))
                message = {**message, 'headers': list(message.get('headers', [])) +
                           [(b'x-local-request-id', record['id'].encode())]}
            elif message['type'] == 'http.response.body' and message.get('body'):
                if record['first_body_ms'] is None:
                    record['first_body_ms'] = elapsed
                    if not record['streaming']:
                        record['first_delta_ms'] = elapsed
            began = time.perf_counter()
            await send(message)
            if message['type'] == 'http.response.body':
                record['response_bytes'] += len(message.get('body', b''))
                if record.get('_terminal_ready') and message.get('body'):
                    record['terminal_sent'] = True
                if not message.get('more_body', False):
                    record['response_complete'] = True
                add_stage('client_send_wait', (time.perf_counter()-began)*1000)
        try:
            await self.app(scope, timed_receive, timed_send)
        except BaseException:
            if record['outcome'] == 'active':
                record['outcome'] = 'interrupted'
            raise
        finally:
            record['total_ms'] = round((time.perf_counter()-start)*1000, 1)
            if record['request_error'] == 'client_disconnected':
                record['outcome'] = 'disconnected'
            elif record['stream_error'] or (record['status'] or 0) >= 400:
                record['outcome'] = 'http_error'
            elif record['terminal_sent'] or (record['response_complete'] and not record['streaming']):
                record['outcome'] = 'completed'
            elif record['streaming'] and record['response_complete']:
                record['outcome'] = 'interrupted'
            elif record['outcome'] == 'active':
                record['outcome'] = 'interrupted'
            with _lock:
                if record.get('_runtime_health') is not None:
                    record.update(record.pop('_runtime_health').since(start))
                _active.pop(record['id'], None)
                _recent.append(record)
            current.reset(token)
