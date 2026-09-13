#!/usr/bin/env python3
"""Browser smoke test with a local mocked image provider; never calls OpenAI.

Requires Playwright and its Chromium installation for development checks only.
"""
import base64
from pathlib import Path
import sys
import tempfile
import threading
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'src'))
from playwright.sync_api import sync_playwright, expect
from hidden_view_finder.server import DemoHTTPServer
from hidden_view_finder.service import DemoService
from hidden_view_finder.scene_image import SceneImageGenerator, SceneImageJobs
png = base64.b64encode((ROOT/'src/hidden_view_finder/static/images/forest_window.png').read_bytes()).decode()
calls = []
release = threading.Event()
def transport(body):
    calls.append(body)
    release.wait(10)
    return {'data': [{'b64_json': png}]}
with tempfile.TemporaryDirectory() as directory:
    jobs = SceneImageJobs(SceneImageGenerator('browser-test-key', transport=transport))
    server = DemoHTTPServer(('127.0.0.1', 0), lambda: DemoService(Path(directory)), image_jobs=jobs)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={'width': 1440, 'height': 1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{server.server_port}', wait_until='networkidle')
            button = page.locator('#cards-list [data-generate-scene]').first
            expect(button).to_be_enabled()
            assert calls == []
            button.click()
            expect(page.locator('#cards-list [data-generate-scene]').first).to_be_disabled()
            page.get_by_role('button', name='비교표', exact=True).click()
            expect(page.locator('#comparison-panel')).to_be_visible()
            release.set()
            page.get_by_role('button', name='카드', exact=True).click()
            image = page.locator('#cards-list .scene-image-output img[src*="/api/images/"]').first
            expect(image).to_be_visible(timeout=15000)
            image.evaluate('(image) => image.decode()')
            assert image.evaluate('(image) => image.naturalWidth > 0')
            assert len(calls) == 1
            assert 'browser-test-key' not in page.content()
            page.set_viewport_size({'width': 390, 'height': 844})
            assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
            assert not errors, errors
            browser.close()
            print('PASS: no automatic calls; button/loading; table remains usable; generated PNG decoded; key absent; mobile fits; no JS errors')
    finally:
        release.set()
        server.shutdown()
        thread.join()
        server.server_close()
