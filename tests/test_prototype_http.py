"""Synthetic transports and deterministic clock; no network or GIS writes."""
import h11
import pytest
from uvicorn import Config
from uvicorn.server import ServerState

from hidden_view_finder.prototype.http import HeaderTimeoutH11Protocol


class Handle:
    def __init__(self, when, callback):
        self.when, self.callback, self.cancelled = when, callback, False

    def cancel(self):
        self.cancelled = True


class Task:
    def __init__(self, coroutine):
        self.coroutine = coroutine

    def add_done_callback(self, callback):
        pass


class Clock:
    def __init__(self):
        self.now, self.handles, self.tasks = 0.0, [], []

    def call_later(self, delay, callback):
        handle = Handle(self.now + delay, callback)
        self.handles.append(handle)
        return handle

    def create_task(self, coroutine, **kwargs):
        task = Task(coroutine)
        self.tasks.append(task)
        return task

    def advance(self, seconds):
        end = self.now + seconds
        while True:
            pending = [h for h in self.handles if not h.cancelled and h.when <= end]
            if not pending:
                break
            handle = min(pending, key=lambda h: h.when)
            self.now = handle.when
            handle.cancelled = True
            handle.callback()
        self.now = end


class Transport:
    def __init__(self, protocol):
        self.protocol, self.closed, self.output = protocol, False, bytearray()

    def get_extra_info(self, name):
        return {'sockname': ('127.0.0.1', 8000), 'peername': ('127.0.0.1', 12345)}.get(name)

    def is_closing(self):
        return self.closed

    def close(self):
        if not self.closed:
            self.closed = True
            self.protocol.connection_lost(None)

    def write(self, data):
        self.output.extend(data)

    def pause_reading(self):
        pass

    def resume_reading(self):
        pass


@pytest.fixture
def protocols():
    async def application(scope, receive, send):
        raise AssertionError('Synthetic tasks are deliberately not executed')

    clock, state, made = Clock(), ServerState(), []
    settings = Config(application, http=HeaderTimeoutH11Protocol, ws='none',
                      lifespan='off', limit_concurrency=16, timeout_keep_alive=5,
                      access_log=False, log_config=None)

    def create():
        protocol = HeaderTimeoutH11Protocol(settings, state, {}, _loop=clock)
        transport = Transport(protocol)
        protocol.connection_made(transport)
        made.append(protocol)
        return protocol, transport

    yield create, clock, state
    for protocol in made:
        protocol.transport.close()
    for task in clock.tasks:
        task.coroutine.close()


def complete_response(protocol):
    protocol.conn.send(h11.Response(status_code=200, headers=[(b'content-length', b'0')]))
    protocol.conn.send(h11.EndOfMessage())
    protocol.cycle.response_complete = True
    protocol.on_response_complete()


def test_idle_preconnections_expire_and_release_all_sixteen_slots(protocols):
    create, clock, state = protocols
    clients = [create() for _ in range(16)]
    assert len(state.connections) == 16
    clock.advance(4.99)
    assert all(not transport.closed for _, transport in clients)
    clock.advance(.02)
    assert all(transport.closed for _, transport in clients)
    assert not state.connections and not state.tasks
    assert all(protocol.limit_concurrency == 16 for protocol, _ in clients)


def test_dripped_incomplete_headers_do_not_extend_deadline(protocols):
    create, clock, _ = protocols
    protocol, transport = create()
    initial = protocol._header_timeout_handle
    clock.advance(2)
    protocol.data_received(b'GET /api/health HTTP/1.1\r\n')
    clock.advance(2)
    protocol.data_received(b'Host: local')
    assert protocol._header_timeout_handle is initial
    clock.advance(1.01)
    assert transport.closed


@pytest.mark.parametrize('payload', [
    b'GET /api/health HTTP/1.1\r\nHost: localhost\r\n\r\n',
    b'POST /api/recommendations HTTP/1.1\r\nHost: localhost\r\nContent-Length: 100\r\n\r\n{',
])
def test_parsed_headers_cancel_timer_without_interrupting_body_or_active_work(protocols, payload):
    create, clock, _ = protocols
    protocol, transport = create()
    handle = protocol._header_timeout_handle
    protocol.data_received(payload)
    assert protocol.cycle and handle.cancelled
    assert protocol._header_timeout_handle is None
    # Even an already queued timeout callback must not close application work.
    handle.callback()
    clock.advance(100)
    assert not transport.closed


def test_partial_second_request_remains_bounded_after_keepalive_timer_cancels(protocols):
    create, clock, _ = protocols
    protocol, transport = create()
    protocol.data_received(b'GET / HTTP/1.1\r\nHost: localhost\r\n\r\n')
    complete_response(protocol)
    header_handle = protocol._header_timeout_handle
    keepalive_handle = protocol.timeout_keep_alive_task
    clock.advance(2)
    protocol.data_received(b'GET /api/health HTTP/1.1\r\nHost:')
    assert keepalive_handle.cancelled
    assert protocol._header_timeout_handle is header_handle
    clock.advance(3.01)
    assert transport.closed


def test_pipelined_active_request_does_not_inherit_header_deadline(protocols):
    create, clock, _ = protocols
    protocol, transport = create()
    protocol.data_received(b'GET / HTTP/1.1\r\nHost: localhost\r\n\r\n'
                           b'POST /api/recommendations HTTP/1.1\r\nHost: localhost\r\nContent-Length: 10\r\n\r\n{')
    previous = protocol.cycle
    complete_response(protocol)
    assert protocol.cycle is not previous
    assert protocol._header_timeout_handle is None
    clock.advance(100)
    assert not transport.closed


@pytest.mark.parametrize('action', ['shutdown', 'connection_lost'])
def test_connection_cleanup_cancels_timer(protocols, action):
    create, clock, _ = protocols
    protocol, transport = create()
    handle = protocol._header_timeout_handle
    if action == 'connection_lost':
        protocol.connection_lost(RuntimeError('Synthetic disconnect'))
    else:
        protocol.shutdown()
    assert handle.cancelled and protocol._header_timeout_handle is None


def test_bad_headers_close_without_retaining_timeout(protocols):
    create, _, _ = protocols
    protocol, transport = create()
    handle = protocol._header_timeout_handle
    protocol.data_received(b'invalid request\r\n\r\n')
    assert transport.closed and handle.cancelled
    assert protocol._header_timeout_handle is None
