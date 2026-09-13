"""Offline image API contracts, scene semantics and asynchronous HTTP integration."""
import base64
import copy
import json
import threading
from http.client import HTTPConnection
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from email.message import Message
from urllib.error import HTTPError, URLError

import pytest

from hidden_view_finder.scene_image import (
    ImageError, SceneImageGenerator, SceneImageJobs, build_scene, build_prompt, load_image_env,
)
from hidden_view_finder.scenarios import default_request
from hidden_view_finder.service import DemoService
from test_public_server import running, http, eventually

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1sAAAAASUVORK5CYII=')


@pytest.fixture(autouse=True)
def no_live_credentials(monkeypatch):
    for name in ('OPENAI_API_KEY', 'HVF_SCENE_IMAGE_MODEL', 'HVF_SCENE_IMAGE_SIZE',
                 'HVF_SCENE_IMAGE_QUALITY', 'HVF_SCENE_IMAGE_ENABLED'):
        monkeypatch.delenv(name, raising=False)


def prototype_scene():
    def sample(name, target, category, facing, height, state='visible'):
        return {'name': name, 'target_id': target, 'category': category, 'bearing_deg': facing,
                'distance_m': 2500, 'target': {'z_m': height}, 'angular_elevation_deg': 12,
                'target_height_reference': 'absolute model surface', 'state': state}
    return {'orientation': {'bearing_deg': 355, 'fov_deg': 70},
            'standing': {'observer_z_m': 45, 'eye_height_m': 1.8},
            'scene_samples': [sample('북한산', 'peak', 'mountain', 340, 836.5),
                              sample('호수', 'lake', 'water', 10, 25),
                              sample('가려진 산', 'hidden', 'mountain', 350, 300, 'blocked'),
                              sample('미확인 강', 'unknown', 'river', 355, None, 'unknown'),
                              sample('뒤쪽 산', 'behind', 'mountain', 180, 400)]}


def test_named_targets_heights_wraparound_and_only_visible_in_fov():
    source = prototype_scene()
    before = copy.deepcopy(source)
    scene = build_scene(source)
    assert source == before
    assert [row['name'] for row in scene['targets']] == ['북한산', '호수']
    left, right = scene['targets']
    assert left['absolute_elevation_m'] == 836.5
    assert left['height_reference'] == 'absolute model surface'
    assert left['relative_bearing_deg'] == -15 and right['relative_bearing_deg'] == 15
    assert left['horizontal_position'] < .5 < right['horizontal_position']
    assert scene['camera']['eye_height_agl_m'] == 1.8
    prompt = build_prompt(scene)
    assert '북한산' in prompt and '호수' in prompt and '836.5' in prompt
    assert '가려진 산' not in prompt and '미확인 강' not in prompt


def test_demo_targets_preserve_agl_offset_without_fabricated_absolute_height():
    service = DemoService()
    result = service.recommend(default_request())
    candidate = result['recommendations'][0]
    scene = build_scene(candidate, result['landmarks'], result['request_summary']['request'])
    assert scene['scope'] == 'fictional_scenario'
    assert scene['targets'][0]['height_reference'] == 'agl'
    assert scene['targets'][0]['absolute_elevation_m'] is None
    assert scene['targets'][0]['distance_m'] > 0
    assert '126.978' not in build_prompt(scene)  # user start/profile is not sent
    service.close()


@pytest.mark.parametrize('value', [float('nan'), float('inf'), True, -1, 361, '355'])
def test_invalid_orientation_never_generates(value):
    source = prototype_scene()
    source['orientation']['bearing_deg'] = value
    with pytest.raises(ImageError, match='방향'):
        build_scene(source)


def test_unknown_evidence_cannot_generate():
    source = prototype_scene()
    for sample in source['scene_samples']:
        sample['state'] = 'unknown'
    with pytest.raises(ImageError) as error:
        build_scene(source)
    assert error.value.code == 'no_visible_targets'


def test_env_file_only_sets_image_keys_and_respects_export(tmp_path, monkeypatch):
    env = tmp_path/'.env'
    env.write_text('OPENAI_API_KEY="file-key"\nHVF_SCENE_IMAGE_MODEL=gpt-image-2.5-sunburst # comment\n'
                   'export HVF_SCENE_IMAGE_SIZE=1024x1024\nUNRELATED_SECRET=ignored\n')
    monkeypatch.setenv('OPENAI_API_KEY', 'exported-key')
    load_image_env(env)
    import os
    assert os.environ['OPENAI_API_KEY'] == 'exported-key'
    assert os.environ['HVF_SCENE_IMAGE_SIZE'] == '1024x1024'
    assert 'UNRELATED_SECRET' not in os.environ


def test_bom_crlf_quoted_key_and_trailing_comment(tmp_path):
    env = tmp_path/'.env'
    env.write_bytes(b'\xef\xbb\xbfOPENAI_API_KEY="  test-key  " # pasted key\r\n')
    load_image_env(env)
    generator = SceneImageGenerator()
    assert generator.enabled and generator._key == 'test-key'


@pytest.mark.parametrize('value', ['test-key\nheader', 'test key', '한글키'])
def test_invalid_key_format_never_leaks_key(value):
    with pytest.raises(ValueError) as error:
        SceneImageGenerator(value)
    assert value not in str(error.value)


def test_key_is_only_required_setting_and_request_matches_images_api():
    calls = []
    def transport(body):
        calls.append(body)
        return {'data': [{'b64_json': base64.b64encode(PNG).decode()}]}
    generator = SceneImageGenerator('test-key', transport=transport)
    result = generator.generate(build_scene(prototype_scene()))
    assert result['png'] == PNG
    assert calls[0]['model'] == generator.model
    assert calls[0]['n'] == 1 and calls[0]['output_format'] == 'png'
    assert 'response_format' not in calls[0]
    assert 'test-key' not in json.dumps(calls)
    assert 'test-key' not in repr(result)


@pytest.mark.parametrize('response', [{}, {'data': []}, {'data': [{'b64_json': 'oops'}]},
                                    {'data': [{'url': 'https://untrusted.example/file'}]}])
def test_invalid_response_is_not_retried_or_downloaded(response):
    calls = []
    def transport(body):
        calls.append(body)
        return response
    generator = SceneImageGenerator('test-key', transport=transport)
    with pytest.raises(ImageError) as error:
        generator.generate(build_scene(prototype_scene()))
    assert error.value.code == 'invalid_provider_image'
    assert len(calls) == 1


def test_no_key_never_calls_transport():
    generator = SceneImageGenerator(transport=lambda _: pytest.fail('Unexpected paid call'))
    with pytest.raises(ImageError) as error:
        generator.generate(build_scene(prototype_scene()))
    assert error.value.code == 'image_not_configured'


@pytest.mark.parametrize('status,code', [(401, 'provider_authentication'), (403, 'provider_authentication'),
                                      (429, 'provider_rate_limited'), (500, 'provider_http_error'),
                                      (302, 'provider_http_error')])
def test_http_provider_errors_are_redacted(monkeypatch, status, code):
    class Opener:
        def open(self, request, timeout):
            assert request.full_url == 'https://api.openai.com/v1/images/generations'
            assert request.get_header('Authorization') == 'Bearer test-key'
            raise HTTPError(request.full_url, status, 'private test-key provider response', {}, None)
    monkeypatch.setattr('hidden_view_finder.scene_image.build_opener', lambda _: Opener())
    with pytest.raises(ImageError) as error:
        SceneImageGenerator('test-key').generate(build_scene(prototype_scene()))
    assert error.value.code == code
    assert 'test-key' not in str(error.value)


def test_network_errors_are_redacted(monkeypatch):
    class Opener:
        def open(self, request, timeout):
            raise URLError('private proxy details')
    monkeypatch.setattr('hidden_view_finder.scene_image.build_opener', lambda _: Opener())
    with pytest.raises(ImageError) as error:
        SceneImageGenerator('test-key').generate(build_scene(prototype_scene()))
    assert error.value.code == 'provider_network_error'
    assert 'private' not in str(error.value)


def test_real_transport_builds_json_and_reads_png_response(monkeypatch):
    from io import BytesIO
    class Response(BytesIO):
        headers = Message()
        headers['Content-Type'] = 'application/json'
    class Opener:
        def open(self, request, timeout):
            body = json.loads(request.data)
            assert body['n'] == 1 and body['size'] == '1536x1024'
            assert '북한산' in body['prompt']
            return Response(json.dumps({'data': [{'b64_json': base64.b64encode(PNG).decode()}]}).encode())
    monkeypatch.setattr('hidden_view_finder.scene_image.build_opener', lambda _: Opener())
    assert SceneImageGenerator('test-key').generate(build_scene(prototype_scene()))['png'] == PNG


def test_http_analysis_to_generation_to_png_without_blocking_native_worker():
    entered, release = threading.Event(), threading.Event()
    calls = []
    def transport(body):
        calls.append(body)
        entered.set()
        assert release.wait(5)
        return {'data': [{'b64_json': base64.b64encode(PNG).decode()}]}
    jobs = SceneImageJobs(SceneImageGenerator('test-key', transport=transport))
    with running(DemoService, image_jobs=jobs) as server:
        status, _, raw = http(server, body=json.dumps(default_request()))
        assert status == 200 and calls == []
        result = json.loads(raw)
        image = result['recommendations'][0]['image']
        key = image['generation']['scene_id']
        assert image['generation']['enabled'] is True
        assert b'test-key' not in raw
        status, _, raw = http(server, '/api/images', body=json.dumps({'scene_id': key}))
        assert status == 202
        try:
            assert entered.wait(2)
            assert http(server, '/api/images', body=json.dumps({'scene_id': key}))[0] == 202
            assert http(server, '/api/health', method='GET', body=None)[0] == 200
            assert http(server, body=json.dumps(default_request()))[0] == 200
        finally:
            release.set()
        eventually(lambda: jobs.status(key)['status'] == 'generated')
        status, _, raw = http(server, f'/api/images/{key}', method='GET', body=None)
        assert status == 200
        output = json.loads(raw)
        status, headers, content = http(server, output['url'], method='GET', body=None)
        assert status == 200 and content == PNG and headers['Content-Type'] == 'image/png'
        assert headers['Cache-Control'] == 'no-store'
        assert http(server, '/api/images', body=json.dumps({'scene_id': key}))[0] == 200
        assert len(calls) == 1
        assert http(server, '/api/images', body=json.dumps({'scene_id': key, 'prompt':'invent a lake'}))[0] == 422
        assert http(server, '/api/images', body='{"scene_id":"forged"}')[0] == 404
        assert http(server, '/api/images', body=json.dumps({'scene_id': key}),
                    headers={'Origin': 'https://attacker.example'})[0] == 403


def test_failure_is_redacted_and_duplicate_requests_do_not_rebill():
    calls = []
    def fail(body):
        calls.append(body)
        raise RuntimeError('test-key /private/file')
    jobs = SceneImageJobs(SceneImageGenerator('test-key', transport=fail))
    try:
        key = jobs.register(build_scene(prototype_scene()))
        jobs.submit(key)
        eventually(lambda: jobs.status(key)['status'] == 'failed')
        assert 'test-key' not in json.dumps(jobs.status(key))
        assert jobs.submit(key)['status'] == 'failed'
        assert len(calls) == 1
    finally:
        jobs.close()


def test_registry_expiry_and_eviction():
    jobs = SceneImageJobs(max_entries=1)
    try:
        scene = build_scene(prototype_scene())
        key = jobs.register(scene)
        assert jobs.register(scene) == key
        changed = copy.deepcopy(scene)
        changed['camera']['horizontal_fov_deg'] = 80
        other = jobs.register(changed)
        with pytest.raises(ImageError):
            jobs.status(key)
        jobs.entries[other]['expires'] = 0
        with pytest.raises(ImageError):
            jobs.status(other)
    finally:
        jobs.close()


def test_queue_capacity_and_image_cache_bound():
    release = threading.Event()
    def transport(body):
        assert release.wait(4)
        return {'data': [{'b64_json': base64.b64encode(PNG).decode()}]}
    jobs = SceneImageJobs(SceneImageGenerator('test-key', transport=transport))
    try:
        scene = build_scene(prototype_scene())
        keys = []
        for i in range(10):
            scene['view_at'] = f'fixture-{i}'
            keys.append(jobs.register(scene))
        for key in keys[:3]:
            jobs.submit(key)
        with pytest.raises(ImageError) as error:
            jobs.submit(keys[3])
        assert error.value.code == 'image_busy'
        release.set()
        eventually(lambda: all(jobs.status(key)['status'] == 'generated' for key in keys[:3]))
        for key in keys[3:]:
            jobs.submit(key)
            eventually(lambda: jobs.status(key)['status'] == 'generated')
        assert sum(entry['state'] == 'generated' for entry in jobs.entries.values()) == 8
    finally:
        release.set()
        jobs.close()


@pytest.mark.parametrize('configured', [False, True])
def test_fresh_server_with_only_env_key_and_no_site_packages(tmp_path, configured):
    """Launch the documented entry point; no injected provider or GIS packages."""
    if configured:
        (tmp_path/'.env').write_text('OPENAI_API_KEY=test-startup-key\n', encoding='utf-8')
    environment = {key: value for key, value in os.environ.items()
                   if key != 'OPENAI_API_KEY' and not key.startswith('HVF_')}
    script = Path(__file__).resolve().parents[1]/'scripts/demo/run.py'
    log = tmp_path/'server.log'
    with log.open('w') as output:
        process = subprocess.Popen([sys.executable, '-S', str(script), '--port', '0'],
            cwd=tmp_path, env=environment, stdout=output, stderr=output)
        try:
            deadline = time.monotonic() + 10
            match = None
            while match is None:
                assert process.poll() is None, log.read_text()
                assert time.monotonic() < deadline, 'Server did not start'
                match = re.search(r'http://127\.0\.0\.1:(\d+)', log.read_text())
                time.sleep(.02)
            connection = HTTPConnection('127.0.0.1', int(match[1]), timeout=5)
            connection.request('GET', '/api/bootstrap')
            response = connection.getresponse()
            raw = response.read()
            assert response.status == 200
            assert json.loads(raw)['scene_images']['enabled'] is configured
            assert b'test-startup-key' not in raw
            connection.close()
            connection = HTTPConnection('127.0.0.1', int(match[1]), timeout=5)
            connection.request('POST', '/api/recommend', json.dumps(default_request()),
                               {'Content-Type': 'application/json'})
            response = connection.getresponse()
            raw = response.read()
            assert response.status == 200
            image = json.loads(raw)['recommendations'][0]['image']
            assert image['generation']['enabled'] is configured
            assert image['scene']['targets'] and image['generation']['scene_id']
            assert b'test-startup-key' not in raw
            connection.close()
        finally:
            process.terminate()
            process.wait(timeout=5)
    assert 'test-startup-key' not in log.read_text()
