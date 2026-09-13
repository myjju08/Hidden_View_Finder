"""Public HTTP boundary regressions; native work is modeled with blocking events."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.client import HTTPConnection
import json
import socket
import threading
import time

import pytest

from hidden_view_finder.server import DemoHTTPServer, MAX_BODY


def http(server, path='/api/recommend', *, method='POST', body='{}', headers=None):
    connection = HTTPConnection('127.0.0.1', server.server_port, timeout=3)
    try:
        connection.request(method, path, body=body,
                           headers={'Content-Type':'application/json', **(headers or {})})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


class FakeService:
    def __init__(self, events=None, action=None):
        self.events = events if events is not None else []
        self.action = action
        self.events.append(('construct', threading.get_ident()))

    def bootstrap(self):
        self.events.append(('bootstrap', threading.get_ident()))
        return {'version':'test', 'defaults':{'mode':'scenario'}}

    def recommend(self, payload):
        self.events.append(('recommend', threading.get_ident()))
        if self.action:
            self.action(payload)
        return {'request':payload}

    def close(self):
        self.events.append(('close', threading.get_ident()))


@contextmanager
def running(factory=FakeService, **kwargs):
    server = DemoHTTPServer(('127.0.0.1', 0), factory, **kwargs)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval':.01}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def eventually(predicate):
    deadline = time.monotonic() + 2
    while not predicate():
        assert time.monotonic() < deadline, 'Condition did not become true'
        time.sleep(.005)


def test_service_lifecycle_and_all_jobs_use_one_worker():
    events = []
    with running(lambda: FakeService(events)) as server:
        with ThreadPoolExecutor(max_workers=3) as callers:
            results = list(callers.map(lambda n: http(server, body=json.dumps({'n':n})), range(3)))
        assert [status for status, _, _ in results] == [200, 200, 200]
        assert [json.loads(body)['request']['n'] for _, _, body in results] == [0, 1, 2]
        assert http(server, '/api/bootstrap', method='GET', body=None)[0] == 200
    assert [name for name, _ in events] == ['construct', 'bootstrap', 'recommend', 'recommend', 'recommend', 'close']
    assert len({worker for _, worker in events}) == 1
    assert events[0][1] != threading.get_ident()


def test_slow_native_job_keeps_health_bootstrap_and_static_responsive_and_queue_bounded():
    entered, release = threading.Event(), threading.Event()
    def slow(_):
        entered.set()
        assert release.wait(3)
    with running(lambda: FakeService(action=slow), max_pending_jobs=2) as server:
        with ThreadPoolExecutor(max_workers=2) as callers:
            first = callers.submit(http, server)
            assert entered.wait(2)
            second = callers.submit(http, server)
            eventually(lambda: server.pending_jobs == 2)
            try:
                started = time.perf_counter()
                status, headers, body = http(server)
                assert status == 503 and json.loads(body)['code'] == 'server_busy'
                assert headers['Retry-After'] == '2'
                for path in ('/api/health', '/api/bootstrap', '/static/styles.css'):
                    assert http(server, path, method='GET', body=None)[0] == 200
                assert time.perf_counter() - started < 1
                assert server.pending_jobs == 2
            finally:
                release.set()
            assert first.result()[0] == second.result()[0] == 200


def test_timed_out_running_job_retains_its_capacity_until_native_work_finishes():
    entered, release = threading.Event(), threading.Event()
    def slow(_):
        entered.set()
        assert release.wait(3)
    with running(lambda: FakeService(action=slow), max_pending_jobs=1, request_timeout_s=.06) as server:
        try:
            status, _, body = http(server)
            assert entered.is_set()
            assert status == 504 and json.loads(body)['code'] == 'computation_timeout'
            assert server.pending_jobs == 1
            assert http(server)[0] == 503
        finally:
            release.set()
        eventually(lambda: server.pending_jobs == 0)
        assert http(server)[0] == 200


def test_timed_out_queued_job_is_cancelled_without_being_computed():
    entered, release = threading.Event(), threading.Event()
    computed = []
    def slow(payload):
        computed.append(payload['n'])
        entered.set()
        assert release.wait(3)
    with running(lambda: FakeService(action=slow), max_pending_jobs=2, request_timeout_s=.06) as server:
        first = server.submit_recommendation({'n':1})
        assert entered.wait(2)
        try:
            assert http(server, body='{"n":2}')[0] == 504
            assert server.pending_jobs == 1
        finally:
            release.set()
        first.result(timeout=2)
        assert computed == [1]


def test_connection_cap_rejects_without_creating_unbounded_request_threads():
    with running(max_http_connections=2, max_pending_jobs=1) as server:
        sockets = [socket.create_connection(server.server_address, timeout=2) for _ in range(2)]
        try:
            eventually(lambda: server._connections._value == 0)
            assert http(server, '/api/health', method='GET', body=None)[0] == 503
        finally:
            for connection in sockets:
                connection.close()
        eventually(lambda: server._connections._value == 2)
        assert http(server, '/api/health', method='GET', body=None)[0] == 200


@pytest.mark.parametrize('origin,host,status', [
    ('https://demo.example', 'demo.example', 200),
    ('https://DEMO.example:443', 'demo.example', 200),
    ('https://attacker.example', 'demo.example', 403),
    ('null', 'demo.example', 403),
    ('https://name@demo.example', 'demo.example', 403),
    ('https://demo.example/other', 'demo.example', 403),
    ('https://demo.example:broken', 'demo.example', 403),
])
def test_same_origin_after_https_reverse_proxy(origin, host, status):
    with running() as server:
        assert http(server, headers={'Origin':origin, 'Host':host})[0] == status


def test_only_explicit_public_origin_allows_rewritten_host():
    headers = {'Origin':'https://demo.example', 'Host':'127.0.0.1:8123', 'X-Forwarded-Host':'demo.example'}
    with running() as server:
        assert http(server, headers=headers)[0] == 403
    with running(public_origins=('https://demo.example',)) as server:
        assert http(server, headers=headers)[0] == 200
        assert http(server, headers={**headers, 'Sec-Fetch-Site':'cross-site'})[0] == 403


@pytest.mark.parametrize('error,status,code', [
    (FileNotFoundError('/private/customer-data/token.json'), 409, 'missing_prepared_data'),
    (ValueError('/private/customer-data/token.json'), 422, 'unsupported_request'),
    (RuntimeError('/private/customer-data/token.json'), 500, 'computation_failed'),
])
def test_internal_exceptions_are_not_sent_to_browser(error, status, code):
    def fail(_):
        raise error
    with running(lambda: FakeService(action=fail)) as server:
        actual, _, body = http(server)
        assert actual == status
        assert json.loads(body)['code'] == code
        assert b'private' not in body and b'token.json' not in body


def test_coverage_failure_remains_explicit_and_never_becomes_empty_recommendations():
    from seoul_visibility.errors import IncompleteCoverageError
    def fail(_):
        raise IncompleteCoverageError('internal-path: missing terrain')
    with running(lambda: FakeService(action=fail)) as server:
        status, _, body = http(server)
        assert status == 422
        assert json.loads(body)['code'] == 'incomplete_coverage'
        assert b'internal-path' not in body


@pytest.mark.parametrize('body,status', [('{"x":NaN}',422), ('[' * 2000,422), ('null',422), ('{}',200), ('x'*(MAX_BODY+1),413)])
def test_body_validation_remains_bounded(body, status):
    with running() as server:
        assert http(server, body=body)[0] == status


def test_request_framing_rejects_ambiguous_length_and_transfer_encoding():
    with running() as server:
        assert http(server, headers={'Transfer-Encoding':'chunked'})[0] == 400
        for extra in ('Content-Length: 3\r\n', ''):
            with socket.create_connection(server.server_address, timeout=2) as connection:
                length = 'Content-Length: 2\r\n' if extra else ''
                connection.sendall(('POST /api/recommend HTTP/1.0\r\nHost: localhost\r\n'
                    'Content-Type: application/json\r\n' + length + extra + '\r\n{}').encode())
                assert b'400 Bad Request' in connection.recv(2048)


def test_stalled_request_body_times_out_without_holding_native_worker():
    with running(socket_timeout_s=.08) as server:
        with socket.create_connection(server.server_address, timeout=2) as connection:
            connection.sendall(b'POST /api/recommend HTTP/1.0\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: 20\r\n\r\n{')
            response = connection.recv(2048)
            assert b'408 Request Timeout' in response
            assert server.pending_jobs == 0
        assert http(server)[0] == 200


def test_access_log_omits_profile_coordinates_and_query_strings(capsys):
    with running() as server:
        assert http(server, '/api/health?location=PRIVATE_COORDINATES', method='GET', body=None)[0] == 200
        assert http(server, body='{"purpose":"PRIVATE_PURPOSE"}')[0] == 200
    captured = capsys.readouterr()
    assert 'PRIVATE_' not in captured.out + captured.err
    assert 'GET 200' in captured.err and 'POST 200' in captured.err


def test_head_has_security_headers_and_no_body():
    with running() as server:
        status, headers, body = http(server, '/', method='HEAD', body=None)
        assert status == 200 and body == b''
        assert int(headers['Content-Length']) > 0
        assert 'frame-ancestors' in headers['Content-Security-Policy']
        assert 'Python' not in headers['Server']


@pytest.mark.parametrize('kwargs', [
    {'max_http_connections':1}, {'max_pending_jobs':16}, {'max_pending_jobs':0},
    {'request_timeout_s':0}, {'socket_timeout_s':31}, {'public_origins':('null',)},
])
def test_invalid_public_server_bounds_fail_before_opening_service(kwargs):
    with pytest.raises(ValueError):
        DemoHTTPServer(('127.0.0.1',0), FakeService, **kwargs)
