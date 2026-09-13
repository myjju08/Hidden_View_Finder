"""Bounded demo HTTP service with one dedicated native-computation worker.

Use a managed HTTPS reverse proxy for public hosting. Request profiles are kept
in memory for the response only; request bodies, coordinates and query strings
are not written to the access log.
"""
from __future__ import annotations

import argparse
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import mimetypes
from pathlib import Path
import socket
from socketserver import ThreadingMixIn
import sys
import threading
from typing import Callable
from urllib.parse import unquote, urlsplit

from .models import RequestError
from .service import DemoService
from .scene_image import ImageError, SceneImageJobs, load_image_env

STATIC = Path(__file__).parent/'static'
MAX_BODY = 64 * 1024


class ServerBusy(Exception):
    """The bounded native job queue is full or the server is shutting down."""


def _json_bytes(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')


def _origin(value: str) -> tuple[str, str, int] | None:
    """Normalize a browser origin without trusting forwarded request headers."""
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or
                parsed.username is not None or parsed.password is not None or
                parsed.path not in ('', '/') or parsed.query or parsed.fragment):
            return None
        return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == 'https' else 80)
    except ValueError:
        return None


class DemoHTTPServer(ThreadingMixIn, HTTPServer):
    """Bound HTTP connections separately from serialized GDAL work.

    A service factory is preferred: construction, bootstrap, recommendations and
    close all execute on the same worker. An unopened service instance remains
    accepted for compatibility. Bootstrap is an immutable startup snapshot; a
    deployment must restart after changing prepared data or its configuration.
    """

    allow_reuse_address = True
    daemon_threads = False
    block_on_close = True
    request_queue_size = 16

    def __init__(self, address: tuple, service: DemoService | Callable[[], DemoService], *,
                 max_http_connections: int = 16, max_pending_jobs: int = 3,
                 request_timeout_s: float = 30.0, socket_timeout_s: float = 10.0,
                 public_origins: tuple[str, ...] = (), image_jobs: SceneImageJobs | None = None):
        if not 2 <= max_http_connections <= 128:
            raise ValueError('max_http_connections must be between 2 and 128')
        if not 1 <= max_pending_jobs < max_http_connections:
            raise ValueError('max_pending_jobs must be positive and below max_http_connections')
        if not 0 < request_timeout_s <= 120 or not 0 < socket_timeout_s <= 30:
            raise ValueError('Request timeout must be in (0, 120] and socket timeout in (0, 30] seconds')
        self.public_origins = frozenset(_origin(value) for value in public_origins)
        if None in self.public_origins:
            raise ValueError('public_origins must contain complete HTTP(S) origins')
        if not callable(service) and getattr(getattr(service, 'seoul', None), 'engine', None) is not None:
            raise ValueError('Pass a service factory or an unopened service; GDAL handles cannot change threads')
        self.request_timeout_s = request_timeout_s
        self.socket_timeout_s = socket_timeout_s
        self.max_pending_jobs = max_pending_jobs
        self._connections = threading.BoundedSemaphore(max_http_connections)
        self._jobs = threading.BoundedSemaphore(max_pending_jobs)
        self._state_lock = threading.Lock()
        self._pending = 0
        self._closing = False
        self._closed = False
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='hidden-view-native')
        self.service = None
        self.image_jobs = image_jobs or SceneImageJobs()
        try:
            super().__init__(address, Handler)
            self.bootstrap_bytes = self._executor.submit(self._initialize, service).result()
        except BaseException:
            self.server_close()
            raise

    def _initialize(self, source: DemoService | Callable[[], DemoService]) -> bytes:
        self.service = source() if callable(source) else source
        return _json_bytes({**self.service.bootstrap(), 'scene_images': self.image_jobs.capabilities()})

    @property
    def pending_jobs(self) -> int:
        with self._state_lock:
            return self._pending

    def _recommend(self, payload: dict) -> bytes:
        # Serialization also finishes here: HTTP threads only receive immutable
        # bytes, never a result object backed by native buffers or shared caches.
        return _json_bytes(self.image_jobs.register_result(self.service.recommend(payload)))

    def submit_recommendation(self, payload: dict) -> Future:
        with self._state_lock:
            if self._closing or not self._jobs.acquire(blocking=False):
                raise ServerBusy
            self._pending += 1
            try:
                future = self._executor.submit(self._recommend, payload)
            except BaseException:
                self._pending -= 1
                self._jobs.release()
                raise

        def finished(_future: Future) -> None:
            with self._state_lock:
                self._pending -= 1
                self._jobs.release()

        future.add_done_callback(finished)
        return future

    def process_request(self, request: socket.socket, client_address: tuple) -> None:
        if not self._connections.acquire(blocking=False):
            # Refuse before allocating another thread; no request content or
            # personal input has been read. The socket always closes afterward.
            body = b'{"error":"Server is busy; please try again shortly.","code":"server_busy"}'
            try:
                request.settimeout(1)
                request.sendall(b'HTTP/1.0 503 Service Unavailable\r\nContent-Type: application/json\r\n'
                                b'Connection: close\r\nRetry-After: 2\r\nCache-Control: no-store\r\n'
                                + f'Content-Length: {len(body)}\r\n\r\n'.encode('ascii') + body)
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._connections.release()
            raise

    def process_request_thread(self, request: socket.socket, client_address: tuple) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connections.release()

    def handle_error(self, request: socket.socket, client_address: tuple) -> None:
        # The stdlib default prints traceback paths. Keep deployment diagnostics
        # terse and independent of request bodies/URLs.
        print('HiddenViewFinder: HTTP request failed', file=sys.stderr, flush=True)

    def server_close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closing = True
            self._closed = True
        try:
            super().server_close()
        finally:
            try:
                if self.service is not None:
                    self._executor.submit(self.service.close).result()
            finally:
                self._executor.shutdown(wait=True, cancel_futures=True)
                self.image_jobs.close()


class Handler(BaseHTTPRequestHandler):
    server_version = 'HiddenViewFinder/0.2'
    sys_version = ''
    # HTTP/1.0 closes each response, bounding speculative browser connections.

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(self.server.socket_timeout_s)

    def log_request(self, code: int | str = '-', size: int | str = '-') -> None:
        # No IP, URL, query string, user profile or request body is logged.
        print(f'HiddenViewFinder: {self.command or "HTTP"} {code}', file=sys.stderr, flush=True)

    def log_error(self, _format: str, *_args) -> None:
        print('HiddenViewFinder: request rejected or failed', file=sys.stderr, flush=True)

    def respond(self, status: int, body: bytes, content_type: str) -> None:
        try:
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store' if content_type.startswith('application/json') or self.path.startswith('/api/images/') else 'no-cache')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Permissions-Policy', 'camera=(), microphone=(), payment=(), geolocation=(self)')
            self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'; form-action 'self'")
            if status == 503:
                self.send_header('Retry-After', '2')
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            # Client disconnects do not cancel a running native operation, whose
            # queue slot stays reserved until it actually finishes.
            self.close_connection = True

    def json_response(self, status: int, value: dict) -> None:
        self.respond(status, _json_bytes(value), 'application/json; charset=utf-8')

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        self.close_connection = True
        self.json_response(code, {'error': 'Invalid or unsupported HTTP request', 'code': 'http_error'})

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        try:
            path = unquote(urlsplit(self.path).path)
        except ValueError:
            return self.json_response(400, {'error': 'Invalid URL'})
        if path == '/api/bootstrap':
            return self.respond(200, self.server.bootstrap_bytes, 'application/json; charset=utf-8')
        if path.startswith('/api/images/'):
            parts = path.split('/')
            try:
                if len(parts) == 4:
                    return self.json_response(200, self.server.image_jobs.status(parts[3]))
                if len(parts) == 5 and parts[4] == 'content':
                    return self.respond(200, self.server.image_jobs.content(parts[3]), 'image/png')
            except ImageError as error:
                return self.json_response(error.status, {'error': str(error), 'code': error.code})
            return self.json_response(404, {'error': 'Not found'})
        if path == '/api/health':
            return self.json_response(200, {'status':'ok', 'version':'0.2.0',
                'job_policy':'one dedicated native worker; bounded HTTP connections and job queue',
                'pending_jobs':self.server.pending_jobs, 'max_pending_jobs':self.server.max_pending_jobs})
        if path == '/api/about':
            return self.json_response(200, {'purpose':'Evidence-aware scenic recommendation demonstration',
                'sources':'https://github.com/myjju08/Hidden_View_Finder/blob/main/docs/demo-sources.md',
                'privacy':'Request profiles are processed in memory; this app does not save profiles or log their contents. Hosting providers may retain connection metadata.',
                'limits':'Fictional preset or approximate Seoul point visibility; unverified hard constraints are separate.'})
        relative = 'index.html' if path == '/' else path.removeprefix('/static/') if path.startswith('/static/') else None
        if relative is None or '\x00' in relative:
            return self.json_response(404, {'error':'Not found'})
        try:
            target = (STATIC/relative).resolve()
            if not target.is_relative_to(STATIC.resolve()) or not target.is_file():
                return self.json_response(404, {'error':'Not found'})
            kind = mimetypes.guess_type(target.name)[0] or 'application/octet-stream'
            if kind.startswith('text/') or kind == 'application/javascript':
                kind += '; charset=utf-8'
            self.respond(200, target.read_bytes(), kind)
        except OSError:
            self.json_response(404, {'error':'Not found'})

    def _same_origin(self) -> bool:
        if self.headers.get('Sec-Fetch-Site', '').lower() == 'cross-site':
            return False
        value = self.headers.get('Origin')
        if not value:
            return True  # CLI clients need no browser Origin header.
        origin = _origin(value)
        if origin is None:
            return False
        # TLS is terminated at the reverse proxy. Compare its preserved Host
        # using the Origin scheme, or an explicitly configured public origin.
        host = self.headers.get('Host', '')
        return origin == _origin(f'{origin[0]}://{host}') or origin in self.server.public_origins

    def do_POST(self) -> None:
        try:
            path = urlsplit(self.path).path
        except ValueError:
            return self.json_response(400, {'error':'Invalid URL'})
        if path not in ('/api/recommend', '/api/images'):
            return self.json_response(404, {'error':'Not found'})
        if not self._same_origin():
            return self.json_response(403, {'error':'Cross-origin requests are disabled'})
        if self.headers.get('Transfer-Encoding') is not None or len(self.headers.get_all('Content-Length', [])) != 1:
            return self.json_response(400, {'error':'One Content-Length header is required; chunked requests are unsupported'})
        if self.headers.get_content_type() != 'application/json':
            return self.json_response(415, {'error':'Use application/json'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            length = 0
        if not 0 < length <= MAX_BODY:
            return self.json_response(413, {'error':'Request body must be 1–65536 bytes'})
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                return self.json_response(400, {'error':'Incomplete request body'})
            payload = json.loads(raw.decode(), parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Nonfinite JSON number')))
        except (socket.timeout, TimeoutError):
            return self.json_response(408, {'error':'Request body timed out'})
        except (ValueError, UnicodeDecodeError, RecursionError):
            return self.json_response(422, {'error':'Provide a valid finite JSON object', 'code':'invalid_json'})
        if not isinstance(payload, dict):
            return self.json_response(422, {'error':'The request must be a JSON object', 'code':'invalid_request'})
        if path == '/api/images':
            if set(payload) != {'scene_id'} or not isinstance(payload['scene_id'], str):
                return self.json_response(422, {'error': '분석 결과의 scene_id만 전달해 주세요.', 'code': 'invalid_request'})
            try:
                result = self.server.image_jobs.submit(payload['scene_id'])
                return self.json_response(202 if result['status'] in ('queued', 'running') else 200, result)
            except ImageError as error:
                return self.json_response(error.status, {'error': str(error), 'code': error.code})
        future = None
        try:
            future = self.server.submit_recommendation(payload)
            body = future.result(timeout=self.server.request_timeout_s)
            self.respond(200, body, 'application/json; charset=utf-8')
        except ServerBusy:
            self.json_response(503, {'error':'다른 요청을 계산 중입니다. 잠시 후 다시 시도해 주세요.', 'code':'server_busy'})
        except FutureTimeout:
            if future is not None:
                future.cancel()  # Queued jobs cancel; active native jobs retain their slot.
            self.json_response(504, {'error':'계산 대기 시간이 초과되었습니다. 잠시 후 다시 시도해 주세요.', 'code':'computation_timeout'})
        except FileNotFoundError:
            self.json_response(409, {'error':'이 모드의 준비 데이터가 현재 서버에 없습니다.', 'code':'missing_prepared_data'})
        except RequestError as error:
            self.json_response(422, {'error':str(error)[:400], 'code':'invalid_request'})
        except ValueError as error:
            name = type(error).__name__
            if name == 'IncompleteCoverageError':
                self.json_response(422, {'error':'선택한 계산 범위에 미확인 지형 또는 건물 높이가 있어 가시성을 판단할 수 없습니다.', 'code':'incomplete_coverage'})
            else:
                self.json_response(422, {'error':'선택한 조건은 현재 준비 자료로 계산할 수 없습니다. 입력 조건과 서비스 범위를 확인해 주세요.', 'code':'unsupported_request'})
        except Exception:
            self.log_error('Recommendation failed')
            self.json_response(500, {'error':'계산을 완료하지 못했습니다. 잠시 후 다시 시도해 주세요.', 'code':'computation_failed'})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--data-root', type=Path, default=Path('data'))
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--context', type=Path, help='Prepared local map context for the selected target/region')
    parser.add_argument('--online-weather', action='store_true', help='Enable bounded Open-Meteo forecasts; errors remain unknown')
    parser.add_argument('--env-file', type=Path, default=Path('.env'), help='Image API settings file; exported environment variables take precedence')
    parser.add_argument('--max-http-connections', type=int, default=16)
    parser.add_argument('--max-pending-jobs', type=int, default=3, help='Includes the running native job; excess requests receive 503')
    parser.add_argument('--request-timeout-s', type=float, default=30.0)
    parser.add_argument('--public-origin', action='append', default=[], help='Explicit HTTPS origin when a trusted reverse proxy rewrites Host; repeatable')
    args = parser.parse_args()
    load_image_env(args.env_file)
    server = DemoHTTPServer((args.host, args.port),
        lambda: DemoService(args.data_root, manifest=args.manifest, context=args.context, online_weather=args.online_weather),
        max_http_connections=args.max_http_connections, max_pending_jobs=args.max_pending_jobs,
        request_timeout_s=args.request_timeout_s, public_origins=tuple(args.public_origin))
    print(f'Hidden View Finder demo: http://{args.host}:{server.server_port}', flush=True)
    try:
        server.serve_forever(poll_interval=.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
