"""Local, single-job demo server. No authentication or production hosting implied."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import mimetypes
from pathlib import Path
import socket
from urllib.parse import unquote, urlsplit

from .service import DemoService

STATIC = Path(__file__).parent/'static'
MAX_BODY = 64 * 1024


class DemoHTTPServer(HTTPServer):
    allow_reuse_address = True
    def __init__(self, address: tuple, service: DemoService):
        self.service = service
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = 'HiddenViewFinder/0.2'
    # HTTP/1.0 closes each connection so browser speculative keepalive cannot
    # hold the single GDAL-owning worker. No dataset handles cross threads.
    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(10)

    def respond(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store' if content_type.startswith('application/json') else 'no-cache')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)

    def json_response(self, status: int, value: dict) -> None:
        self.respond(status, json.dumps(value, ensure_ascii=False, allow_nan=False).encode(), 'application/json; charset=utf-8')

    def do_GET(self) -> None:
        path = unquote(urlsplit(self.path).path)
        if path == '/api/bootstrap':
            return self.json_response(200, self.server.service.bootstrap())
        if path == '/api/health':
            return self.json_response(200, {'status':'ok', 'version':'0.2.0', 'job_policy':'one native job on the serving thread'})
        if path == '/api/about':
            return self.json_response(200, {'purpose':'Local evidence-aware recommendation demonstration',
                'sources':'https://github.com/myjju08/Hidden_View_Finder/blob/main/docs/demo-sources.md',
                'limits':'Fictional preset or approximate Seoul point visibility; unverified hard constraints are separate.'})
        relative = 'index.html' if path == '/' else path.removeprefix('/static/') if path.startswith('/static/') else None
        if relative is None:
            return self.json_response(404, {'error':'Not found'})
        target = (STATIC/relative).resolve()
        if not target.is_relative_to(STATIC.resolve()) or not target.is_file():
            return self.json_response(404, {'error':'Not found'})
        kind = mimetypes.guess_type(target.name)[0] or 'application/octet-stream'
        if kind.startswith('text/') or kind == 'application/javascript':
            kind += '; charset=utf-8'
        self.respond(200, target.read_bytes(), kind)

    def do_POST(self) -> None:
        if urlsplit(self.path).path != '/api/recommend':
            return self.json_response(404, {'error':'Not found'})
        # Browsers cannot submit expensive local queries from another origin.
        origin = self.headers.get('Origin')
        if origin and urlsplit(origin).netloc != self.headers.get('Host'):
            return self.json_response(403, {'error':'Cross-origin requests are disabled'})
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
            payload = json.loads(raw.decode(), parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Nonfinite JSON number')))
            result = self.server.service.recommend(payload)
            self.json_response(200, result)
        except FileNotFoundError as error:
            self.json_response(409, {'error':str(error), 'code':'missing_prepared_data'})
        except (ValueError, UnicodeDecodeError) as error:
            self.json_response(422, {'error':str(error), 'code':type(error).__name__})
        except (socket.timeout, TimeoutError):
            self.json_response(408, {'error':'Request timed out'})
        except Exception as error:
            # No tracebacks, local paths or credentials sent to the browser.
            self.log_error('Recommendation failed: %s', type(error).__name__)
            self.json_response(500, {'error':'The computation failed; inspect the local server log.', 'code':type(error).__name__})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--data-root', type=Path, default=Path('data'))
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--context', type=Path, help='Prepared local map context for the selected target/region')
    parser.add_argument('--online-weather', action='store_true', help='Enable bounded Open-Meteo forecasts; errors remain unknown')
    args = parser.parse_args()
    service = DemoService(args.data_root, manifest=args.manifest, context=args.context, online_weather=args.online_weather)
    server = DemoHTTPServer((args.host, args.port), service)
    print(f'Hidden View Finder demo: http://{args.host}:{server.server_port}', flush=True)
    try:
        server.serve_forever(poll_interval=.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()


if __name__ == '__main__':
    main()
