"""API-only synthetic mocks; fixtures never read or mutate real city data."""
from copy import deepcopy
import asyncio
import json
import threading
import time
from hashlib import sha256

import httpx
from fastapi.testclient import TestClient
import pytest

from hidden_view_finder.prototype import api
from seoul_visibility.acquisition_safety import Budget


@pytest.fixture
def application(tmp_path, monkeypatch):
    manifest = tmp_path/'manifest.json'
    manifest.write_text(json.dumps({'package_id': 'synthetic-api-fixture', 'readiness': {'normalized_ready': False, 'visibility_ready': False}}))
    tiles = tmp_path/'tiles.json'
    tiles.write_text(json.dumps({'tiles': []}))
    c = {'package_manifest': str(manifest), 'tile_manifest': str(tiles), 'runtime_root': str(tmp_path/'runtime'),
         'ai': {'enabled': False, 'image_model': None, 'text_model': None, 'daily_usd': 0},
         'limits': {'memory_scene_entries': 3, 'map_features': 5}, 'public_deployment_status': 'blocked_pending_actual_layer_licence_review'}
    monkeypatch.setattr(api, 'config', lambda *args: deepcopy(c))
    monkeypatch.setattr(api, 'budget', lambda *args: Budget(tmp_path, limit=200_000_000, stage_root=tmp_path/'staging'))
    monkeypatch.setattr(api, 'public_storage', lambda *args: {'status': 'available', 'synthetic_fixture': True})
    class FakeDiscovery:
        def __init__(self, settings):
            self.thread = threading.get_ident()
            self.queries = []
            self.started, self.release = threading.Event(), threading.Event()
            self.block = False
        def recommend(self, query):
            assert threading.get_ident() == self.thread
            self.queries.append(query)
            if self.block:
                self.started.set()
                self.release.wait(3)
            proximity = round(abs(query.lon-127)*100000,1)+10
            scene = {'view_id': 'a'*24, 'candidate_id': 'fixture-candidate', 'name': 'Synthetic API fixture only',
                     'standing': {'lon': 127.0, 'lat': 37.5, 'effective_x': 200002.5, 'effective_y': 500002.5},
                     'orientation': {'bearing_deg': 120, 'fov_deg': 60}, 'view_at': query.view_at.isoformat(),
                     'versions': {'geometry': 'fixture-geometry', 'package_id': 'synthetic-api-fixture'},
                     'scene_samples': [{'evidence_id': 'fixture-evidence-1', 'category': 'city', 'target_id': 'fixture-building',
                                        'bearing_deg': 120, 'distance_m': 500, 'state': 'visible'}],
                     'coverage': {'visible': 1, 'blocked': 0, 'unknown': 1, 'intended': 2},
                     'route_distance_m': None, 'estimated_travel_minutes': None, 'proximity_m': proximity,
                     'score': {'value': proximity}, 'description': f'Synthetic distance context: {proximity}',
                     'solar': {'azimuth_deg': 270, 'elevation_deg': 15}, 'field_verified': False}
            return {'status': 'partial', 'views': [scene], '_all_scenes': [scene], 'search': {'sampled': True}}
        def close(self):
            assert threading.get_ident() == self.thread
    monkeypatch.setattr(api, 'Discovery', FakeDiscovery)
    class FakeData:
        def __init__(self, *args): pass
        def close(self): pass
        def places(self, q): return [{'name': 'Synthetic place', 'query': q, 'lon': 127, 'lat': 37.5}]
        def map(self, bbox, zoom, limit):
            return {'type': 'FeatureCollection', 'features': [], 'synthetic_fixture': True, 'limit': limit}
    monkeypatch.setattr(api, 'Data', FakeData)
    app = api.create_app()
    with TestClient(app) as client:
        yield app, client


def request_body(**kwargs):
    return {'origin': {'lon': 127.0, 'lat': 37.5}, 'radius_m': 3000,
            'view_at': '2026-09-11T18:00:00+09:00', 'preferences': ['city'], **kwargs}


def test_distance_only_request_and_false_global_readiness(application):
    app, client = application
    result = client.post('/api/recommendations', json=request_body())
    assert result.status_code == 200
    value = result.json()
    assert value['status'] == 'partial' and len(value['views']) == 1
    assert value['views'][0]['route_distance_m'] is None
    assert value['views'][0]['estimated_travel_minutes'] is None
    assert value['presentation']['method'] == 'deterministic_template'
    assert value['presentation']['reason'] == 'disabled'
    assert app.state.service.geometry.queries[0].view_at.utcoffset().total_seconds() == 9*3600
    caps = client.get('/api/capabilities').json()
    assert caps['local_exploration_available'] is True
    assert caps['geographic_ready'] is False and caps['visibility_ready'] is False
    assert caps['source_readiness']['visibility_ready'] is False
    assert caps['public_deployment_status'].startswith('blocked')
    assert 'runtime' not in json.dumps(caps)


@pytest.mark.parametrize('change', [
    {'radius_m': 10001}, {'radius_m': -1}, {'origin': {'lon': 999, 'lat': 37}},
    {'origin': {'lon': 127, 'lat': 37, 'path': '/etc/passwd'}},
    {'route_distance_m': 5}, {'estimated_travel_minutes': 10},
    {'view_at': 'not-a-date'}, {'preferences': ['private_cafe']},
    {'source_url': 'https://example.com'}, {'limit': 4},
])
def test_request_validation_rejects_unbounded_or_routing_fields(application, change):
    app, client = application
    assert client.post('/api/recommendations', json=request_body(**change)).status_code == 422
    assert not app.state.service.geometry.queries


@pytest.mark.parametrize('path', [
    '/static/images/river_steps.png', '/static/../../.env', '/static/vendor/../../../../etc/passwd',
    '/data/citywide/packages/manifest.json', '/api/image-assets/../../etc/passwd/display',
    '/api/views/not-a-valid-id', '/api/views/'+'b'*24,
])
def test_sources_secrets_old_fictional_art_and_unknown_views_not_exposed(application, path):
    _, client = application
    assert client.get(path).status_code == 404


def test_host_origin_csp_and_map_limits(application):
    _, client = application
    assert client.get('/api/health', headers={'Host': 'evil.example'}).status_code == 403
    assert client.get('/api/health', headers={'Origin': 'https://evil.example'}).status_code == 403
    healthy = client.get('/api/health')
    assert healthy.status_code == 200
    assert "frame-ancestors 'none'" in healthy.headers['content-security-policy']
    assert healthy.headers['x-content-type-options'] == 'nosniff'
    assert client.get('/api/map', params={'bbox': '0,0,180,90'}).status_code == 422
    assert client.get('/api/map', params={'bbox': '126,37,127,38', 'zoom': 99}).status_code == 422
    assert client.get('/api/map').json()['limit'] == 5
    assert client.get('/api/places', params={'q': 'x'*81}).status_code == 422


def test_no_key_image_job_for_retained_real_schema_and_stale_views(application):
    app, client = application
    result = client.post('/api/recommendations', json=request_body()).json()
    view_id = result['views'][0]['view_id']
    assert client.get('/api/views/'+view_id).json()['field_verified'] is False
    image = client.post('/api/images', json={'view_id': view_id})
    assert image.status_code == 200
    assert image.json()['reason'] == 'disabled'
    assert client.post('/api/images', json={'view_id': 'b'*24}).status_code == 404
    assert client.post('/api/images', json={'view_id': view_id, 'prompt': 'arbitrary'}).status_code == 422
    assert client.get('/api/images/not-a-key').status_code == 422
    assert client.get('/api/images/'+'0'*64+'.jpg').status_code == 404
    assert not app.state.service.provider.ledger.path.exists()


def test_geometry_queue_refuses_second_job_without_abandoning_active_worker(application):
    app, _ = application
    geometry = app.state.service.geometry
    geometry.block = True
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
            first = asyncio.create_task(client.post('/api/recommendations', json=request_body()))
            await asyncio.to_thread(geometry.started.wait, 1)
            second = await client.post('/api/recommendations', json=request_body())
            assert second.status_code == 429 and second.json()['code'] == 'geometry_busy'
            geometry.release.set()
            assert (await first).status_code == 200
    try:
        asyncio.run(exercise())
    finally:
        geometry.release.set()
    assert len(geometry.queries) == 1
    assert app.state.service.busy is False


def test_body_limit_does_not_trust_content_length_or_read_unbounded_stream(application):
    app, _ = application
    received = 0
    responses = []
    async def receive():
        nonlocal received
        received += 1
        return {'type': 'http.request', 'body': b'x'*9000, 'more_body': received < 10}
    async def send(message):
        responses.append(message)
    scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1', 'method': 'POST',
             'scheme': 'http', 'path': '/api/recommendations', 'raw_path': b'/api/recommendations',
             'query_string': b'', 'server': ('localhost', 80), 'client': ('127.0.0.1', 1),
             'headers': [(b'host', b'localhost'), (b'content-type', b'application/json'), (b'content-length', b'1')]}
    asyncio.run(app(scope, receive, send))
    assert received == 1
    assert next(message for message in responses if message['type'] == 'http.response.start')['status'] in (400, 413)


def test_body_header_limits_and_missing_content_type(application):
    _, client = application
    assert client.post('/api/recommendations', content=b'{}', headers={'Content-Length': '9000', 'Content-Type': 'application/json'}).status_code == 413
    assert client.post('/api/recommendations', content=b'{}', headers={'Content-Length': '2'}).status_code == 415


def test_per_client_rate_limit_does_not_create_geometry_jobs(application):
    app, client = application
    for _ in range(10):
        assert client.post('/api/images', json={'view_id': 'b'*24}).status_code == 404
    limited = client.post('/api/images', json={'view_id': 'b'*24})
    assert limited.status_code == 429
    assert limited.headers['retry-after'] == '60'
    assert not app.state.service.geometry.queries


def test_unavailable_data_does_not_pad_or_crash_service(application):
    app, client = application
    saved = app.state.service.geometry
    app.state.service.geometry = None
    try:
        result = client.post('/api/recommendations', json=request_body())
        assert result.status_code == 409
        assert result.json()['views'] == []
        assert client.get('/api/health').json()['status'] == 'data_unavailable'
        assert client.get('/api/map').status_code == 200
    finally:
        app.state.service.geometry = saved


def test_cancelled_request_keeps_geometry_slot_until_owned_work_finishes(application):
    app, _ = application
    geometry = app.state.service.geometry
    geometry.block = True
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
            first = asyncio.create_task(client.post('/api/recommendations', json=request_body()))
            await asyncio.to_thread(geometry.started.wait, 1)
            first.cancel()
            await asyncio.sleep(.05)
            second = await client.post('/api/recommendations', json=request_body())
            assert second.status_code == 429 and second.json()['code'] == 'geometry_busy'
            geometry.release.set()
            try:
                await first
            except asyncio.CancelledError:
                pass
            for _ in range(50):
                if not app.state.service.busy:
                    break
                await asyncio.sleep(.01)
            assert app.state.service.busy is False
    try:
        asyncio.run(exercise())
    finally:
        geometry.release.set()
    assert len(geometry.queries) == 1


def test_capability_dates_and_coverage_come_from_verified_metadata(tmp_path):
    sources={'terrain':{'result':{'source_year':'2021','source_file_updated':'2022-03-04','licence':'fixture-licence','path':'/private/source'}},
             'osm':{'result':{'source_timestamp':'2024-01-02T00:00:00Z'}},
             'buildings':{'result':{'source_date':'fixture source date','height_reference':'AGL','licences':['fixture-licence']}}}
    blob=json.dumps(sources).encode();(tmp_path/'sources.json').write_bytes(blob)
    package={'package_id':'synthetic-metadata','artifacts':[{'path':'sources.json','bytes':len(blob),'sha256':sha256(blob).hexdigest()}],
             'readiness':{'visibility_ready':False}}
    (tmp_path/'manifest.json').write_text(json.dumps(package))
    (tmp_path/'tiles.json').write_text(json.dumps({'tiles':[{},{}],'resolution_m':5,
        'cell_counts':{'inside_seoul_cells':10,'terrain_valid_seoul_cells':7,'requested_support_cells':20,'terrain_valid_support_cells':8}}))
    result=api.public_input_metadata(tmp_path/'manifest.json',tmp_path/'tiles.json')
    assert result['data']['terrain_source_year']=='2021'
    assert result['data']['osm_snapshot']=='2024-01-02T00:00:00Z'
    assert result['data']['building_imagery_years']=='fixture source date'
    assert result['data']['terrain_seoul_fraction']==.7
    assert result['data']['terrain_support_fraction']==.4
    assert result['data']['tile_count']==2
    assert '/private/' not in json.dumps(result)
    assert result['readiness']['visibility_ready'] is False
    (tmp_path/'sources.json').write_bytes(blob+b' ')
    with pytest.raises(ValueError,match='published package'):
        api.public_input_metadata(tmp_path/'manifest.json',tmp_path/'tiles.json')


def test_api_schema_is_local_without_cdn_documentation_pages(application):
    _,client=application
    assert client.get('/api/docs').status_code==404
    assert client.get('/redoc').status_code==404
    assert client.get('/openapi.json').status_code==200


def test_anonymous_sessions_isolate_same_view_distance_rank_and_images(application):
    app,first=application
    app.state.service.provider.compare=lambda scenes:{'status':'synthetic','annotations':[
        {'view_id':scenes[0]['view_id'],'evidence_ids':['fixture-evidence-1'],
         'focus':'direction' if scenes[0]['proximity_m']==10 else 'partial_evidence'}]}
    second=TestClient(app)
    stranger=TestClient(app)
    try:
        one=first.post('/api/recommendations',json=request_body()).json()['views'][0]
        response=second.post('/api/recommendations',json=request_body(origin={'lon':127.01,'lat':37.5}))
        two=response.json()['views'][0]
        assert one['view_id']==two['view_id']
        assert one['proximity_m']!=two['proximity_m']
        assert first.get('/api/views/'+one['view_id']).json()['proximity_m']==one['proximity_m']
        assert second.get('/api/views/'+two['view_id']).json()['proximity_m']==two['proximity_m']
        assert first.get('/api/views/'+one['view_id']).json()['score']==one['score']
        assert second.get('/api/views/'+two['view_id']).json()['description']==two['description']
        assert first.get('/api/views/'+one['view_id']).json()['ai_presentation']['focus']=='direction'
        assert second.get('/api/views/'+two['view_id']).json()['ai_presentation']['focus']=='partial_evidence'
        assert stranger.get('/api/views/'+one['view_id']).status_code==404
        assert stranger.post('/api/images',json={'view_id':one['view_id']}).status_code==404
        assert first.post('/api/images',json={'view_id':one['view_id']}).json()['reason']=='disabled'
        app.state.service.jobs.submit=lambda scene:{'status':'synthetic','proximity_m':scene['proximity_m']}
        assert first.post('/api/images',json={'view_id':one['view_id']}).json()['proximity_m']==one['proximity_m']
        assert second.post('/api/images',json={'view_id':two['view_id']}).json()['proximity_m']==two['proximity_m']
        cookie=response.headers['set-cookie'].lower()
        assert 'httponly' in cookie and 'samesite=strict' in cookie and 'path=/' in cookie
        assert first.cookies.get(api.SESSION_COOKIE)!=second.cookies.get(api.SESSION_COOKIE)
        assert len(app.state.service.views)==2
        assert all(isinstance(key,tuple) and len(key)==2 for key in app.state.service.views)
        assert all('origin' not in scene for scene in app.state.service.views.values())
    finally:
        second.close();stranger.close()


def test_unknown_and_expired_session_cookie_cannot_recover_prior_context(application):
    app,client=application
    view=client.post('/api/recommendations',json=request_body()).json()['views'][0]
    original_cookie=client.cookies.get(api.SESSION_COOKIE)
    unknown=client.get('/api/views/'+view['view_id'],headers={'Cookie':api.SESSION_COOKIE+'='+'x'*43})
    assert unknown.status_code==404
    assert 'set-cookie' in unknown.headers
    # Restore the previous server-issued cookie, then expire only its registry.
    identity=sha256(original_cookie.encode()).hexdigest()
    app.state.service.sessions[identity]=time.monotonic()-api.SESSION_TTL_SECONDS-1
    expired=client.get('/api/views/'+view['view_id'],headers={'Cookie':api.SESSION_COOKIE+'='+original_cookie})
    assert expired.status_code==404
    assert all(key[0]!=identity for key in app.state.service.views)


def test_session_registry_and_views_keep_global_memory_bounds(application):
    app,_=application
    service=app.state.service
    first,_=service.session(None)
    service.remember(first,[{'view_id':'a'*24,'proximity_m':1}])
    for i in range(api.MAX_SESSIONS):
        current,_=service.session(None)
        service.remember(current,[{'view_id':str(i).zfill(24),'proximity_m':i}])
    assert len(service.sessions)==api.MAX_SESSIONS
    assert first not in service.sessions
    assert len(service.views)==service.config['limits']['memory_scene_entries']
    assert all(key[0]!=first for key in service.views)
