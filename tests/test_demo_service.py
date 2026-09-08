"""Integration contracts for demo providers, presentation and orchestration.

External responses and the viewshed are stubbed here. The suite does not require
downloaded Seoul datasets, network access or generated image assets.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import http.client
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import numpy as np
import pytest

from hidden_view_finder.models import Request
from hidden_view_finder.presentation import IMAGE_LABEL, decorate
from hidden_view_finder.providers import ForecastProvider, WalkingRouter, load_context
from hidden_view_finder.ranking import rank_candidates
from hidden_view_finder.scenarios import default_request, scenario_bundle
from hidden_view_finder.seoul import SeoulAdapter
from hidden_view_finder.service import DemoService
from hidden_view_finder.sunlight import solar_position


def graph():
    return {"nodes": [{"id": str(i), "lon": 127 + i * .0001, "lat": 37.5, "tags": {}} for i in range(4)],
            "edges": [{"u": "0", "v": "1", "length_m": 10, "tags": {"highway": "footway"}, "access_status": "verified"},
                      {"u": "1", "v": "2", "length_m": 10, "tags": {"highway": "steps"}, "steps": True},
                      {"u": "0", "v": "3", "length_m": 30, "tags": {"highway": "footway"}},
                      {"u": "3", "v": "2", "length_m": 30, "tags": {"highway": "footway"}}]}


def test_router_uses_graph_distance_and_avoids_stairs_for_all_mandatory_modes():
    router = WalkingRouter(graph())
    router.solve((127, 37.5), {})
    ordinary = router.route("2")
    assert ordinary["walking_m"] == 20
    assert ordinary["stairs"] is True
    for key in ("no_stairs", "wheelchair", "stroller"):
        router.solve((127, 37.5), {key: True})
        result = router.route("2")
        assert result["walking_m"] == 60
        assert result["stairs"] is False
        assert result["status"] == "estimated"
        assert result["wheelchair"] is None  # a mapped path does not prove curb/width/slope access


@pytest.mark.parametrize("tags", [{"foot": "no"}, {"foot": "private"}, {"access": "private"}])
def test_router_prohibited_edges_are_never_traversed(tags):
    value = graph()
    value["edges"] = [dict(value["edges"][0], tags=tags)]
    router = WalkingRouter(value)
    router.solve((127, 37.5), {})
    assert router.route("1")["status"] == "unknown"


@pytest.mark.parametrize("barrier", [
    {"barrier": "gate", "locked": "yes"},
    {"barrier": "gate", "foot": "no"},
    {"barrier": "gate", "access": "private"},
])
def test_router_explicitly_prohibited_barrier_nodes_are_not_ignored(barrier):
    edge = graph()["edges"][0]
    edge["barriers"] = [dict(barrier, node_id="1")]
    assert not WalkingRouter.permitted(edge, {})


def test_router_does_not_invent_connections_across_disconnected_map_parts():
    value = graph()
    value["edges"] = [value["edges"][0], value["edges"][3]]
    router = WalkingRouter(value)
    router.solve((127, 37.5), {})
    assert router.route("2")["status"] == "unknown"
    assert router.route("1", "transit")["travel_minutes"] is None
    router.solve((128, 38), {})
    assert router.route("1")["status"] == "unknown"


def test_pedestrian_oneway_is_respected_without_reusing_car_oneway():
    value = graph()
    edge = dict(value["edges"][0], tags={"oneway:foot": "yes"}, directed=True)
    value["edges"] = [edge]
    router = WalkingRouter(value)
    # Node 1 has no outbound edges. Starting 9m away is still a snapped connector,
    # so evaluate direct graph reachability from node 0 in the forward direction.
    router.solve((127, 37.5), {})
    assert router.route("1")["walking_m"] == 10
    assert "1" not in router.adjacency
    value["edges"] = [dict(edge, tags={"oneway": "yes"}, directed=False)]
    router = WalkingRouter(value)
    assert "1" in router.adjacency


def test_origin_on_oneway_sink_cannot_snap_upstream_to_bypass_direction():
    value = graph()
    value["edges"] = [dict(value["edges"][0], tags={"oneway:foot": "yes"}, directed=True)]
    router = WalkingRouter(value)
    router.solve((127.0001, 37.5), {})
    assert router.origin == "1"
    assert router.route("0")["status"] == "unknown"


def test_context_requires_schema_and_bounded_size(tmp_path):
    path = tmp_path / "context.json"
    path.write_text(json.dumps({"schema_version": 9, "graph": graph()}))
    with pytest.raises(ValueError, match="Unsupported"):
        load_context(path)
    with path.open("wb") as stream:
        stream.truncate(12 * 1024**2 + 1)
    with pytest.raises(ValueError, match="12 MiB"):
        load_context(path)


def test_scenario_service_runs_without_data_and_preserves_pipeline_and_unknowns(tmp_path):
    service = DemoService(data_root=tmp_path)
    try:
        assert service.bootstrap()["modes"][1]["available"] is False
        result = service.recommend(default_request())
        assert len(result["recommendations"]) == 3
        assert result["mode"] == "scenario"
        assert result["visibility_summary"] is None
        assert [item["id"] for item in result["pipeline"]] == ["context", "landmarks", "spots", "ranking", "presentation"]
        assert result["recommendations"][2]["criteria"]["crowd"] is None
        assert result["recommendations"][2]["evidence_coverage"] == .9
        assert result["weather"]["status"] == "scenario"
        assert result["map"]["kind"] == "scenario"
        assert all(item["image"]["label"] == IMAGE_LABEL for item in result["recommendations"])
    finally:
        service.close()


def test_missing_real_manifest_never_falls_back_to_fictional_recommendations(tmp_path):
    service = DemoService(data_root=tmp_path)
    data = dict(default_request(), mode="seoul")
    with pytest.raises(FileNotFoundError, match="manifest"):
        service.recommend(data)
    service.close()


def test_real_mode_unknown_hard_constraints_are_separate_with_stub_bundle(tmp_path, monkeypatch):
    service = DemoService(data_root=tmp_path)
    req = Request.from_dict(default_request())
    bundle = scenario_bundle(req)
    candidate = bundle["candidates"][0]
    candidate.update(evidence_kind="computed", scenic_features_status="computed", composition_status="unknown")
    candidate["route"]["status"] = "estimated"
    candidate["access"].update(public_status="unknown", opening_hours_status="unknown")
    candidate["crowd"] = {"status": "unknown", "level": None}
    candidate["weather"] = {"status": "unknown"}
    bundle.update(candidates=[candidate], weather={"status": "unknown"})
    service.seoul = SimpleNamespace(available=True, bundle=lambda request: bundle, close=lambda: None)
    result = service.recommend(dict(default_request(), mode="seoul"))
    assert not result["recommendations"]
    assert len(result["unverified"]) == 1
    assert result["unverified"][0]["hard_constraint_status"] == "unverified"
    assert result["unverified"][0]["image"]["status"] == "not_generated"
    assert result["unverified"][0]["criteria"]["time_weather"] is None
    service.close()


def _ranked_fixture(payload=None):
    req = Request.from_dict(payload or default_request())
    bundle = scenario_bundle(req)
    ranked = rank_candidates(req, bundle["candidates"])
    return deepcopy(ranked["recommendations"][0]), req.to_dict(), bundle["landmarks"]


def _fake_image_dir(tmp_path, monkeypatch):
    import hidden_view_finder.presentation as presentation
    monkeypatch.setattr(presentation, "__file__", str(tmp_path / "presentation.py"))
    image = tmp_path / "static/images/river_steps.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"This test only exercises asset existence, not image rendering")


def test_image_is_labeled_and_selected_only_after_ranking(tmp_path, monkeypatch):
    _fake_image_dir(tmp_path, monkeypatch)
    candidate, req, landmarks = _ranked_fixture()
    decorated = decorate(candidate, req, landmarks, selected=True)
    assert decorated["image"]["status"] == "generated"
    assert decorated["image"]["label"] == IMAGE_LABEL
    assert "not" in decorated["image"]["generated_for"].lower()
    assert decorate(deepcopy(candidate), req, landmarks, selected=False)["image"]["status"] == "not_generated"


@pytest.mark.parametrize("updates", [
    {"transport_mode": "transit"},
    {"eye_height_m": 1.2},
    {"visit_time": "2026-09-08T17:00:00+09:00"},
    {"start": {"lon": 126.9781, "lat": 37.571}},
])
def test_image_is_not_reused_for_changed_observer_or_arrival_context(updates, tmp_path, monkeypatch):
    _fake_image_dir(tmp_path, monkeypatch)
    candidate, req, landmarks = _ranked_fixture(dict(default_request(), **updates))
    candidate["id"] = "river_steps"
    result = decorate(candidate, req, landmarks, selected=True)
    assert result["image"]["status"] == "not_generated"
    assert result["image"]["url"] is None
    assert result["image"]["prompt"]


def test_missing_image_asset_emits_prompt_without_claiming_generation(tmp_path, monkeypatch):
    import hidden_view_finder.presentation as presentation
    monkeypatch.setattr(presentation, "__file__", str(tmp_path / "presentation.py"))
    candidate, req, landmarks = _ranked_fixture()
    result = decorate(candidate, req, landmarks, selected=True)
    assert result["image"]["status"] == "not_generated"
    assert "Observer lon=" in result["image"]["prompt"]
    assert "illustrative mood reference" in result["image"]["prompt"]


def test_real_description_discloses_effective_cell_and_does_not_assert_requested_node_los():
    candidate, req, landmarks = _ranked_fixture()
    req["mode"] = "seoul"
    candidate["visibility_evidence"] = {"effective_observer": {"lon": candidate["lon"] + .00001, "lat": candidate["lat"]}, "snap_distance_m": 1.2}
    result = decorate(candidate, req, landmarks, selected=False)
    assert "격자" in result["description"] or "셀" in result["description"]
    assert "1.2" in result["description"]


def test_offline_weather_never_uses_network_or_imputes_clear_sky(monkeypatch):
    import hidden_view_finder.providers as providers
    monkeypatch.setattr(providers, "urlopen", lambda *a, **k: pytest.fail("Network called with weather disabled"))
    result = ForecastProvider(enabled=False).get(127, 37.5, datetime.now(timezone.utc))
    assert result["status"] == "unknown"
    assert result["precipitation_mm"] is None and result["cloud_cover_percent"] is None


def _weather_payload(arrival):
    from zoneinfo import ZoneInfo
    local = arrival.astimezone(ZoneInfo("Asia/Seoul")).replace(minute=0, second=0, microsecond=0)
    return {"hourly": {"time": [(local + timedelta(hours=h)).replace(tzinfo=None).isoformat() for h in (-1, 0, 1)],
            "precipitation": [0, 2, None], "cloud_cover": [20, 80, None], "visibility": [12000, 4500, None],
            "wind_speed_10m": [2, 4, None]}, "daily": {"sunrise": ["06:00"], "sunset": ["19:00"]}}


def test_forecast_uses_seoul_timezone_and_preserves_reference_and_validity(monkeypatch):
    import hidden_view_finder.providers as providers
    arrival = (datetime.now(timezone.utc) + timedelta(hours=4)).replace(minute=0, second=0, microsecond=0)
    calls = []
    def open_response(request, timeout):
        calls.append(request.full_url)
        return io.BytesIO(json.dumps(_weather_payload(arrival)).encode())
    monkeypatch.setattr(providers, "urlopen", open_response)
    provider = ForecastProvider(enabled=True)
    result = provider.get(127, 37.5, arrival)
    assert result["status"] == "forecast"
    assert result["precipitation_mm"] == 2
    assert result["wind_m_s"] == 4
    assert result["model_run_time"] is None
    assert result["forecast_time"].endswith("+09:00")
    assert datetime.fromisoformat(result["valid_from"]) <= arrival <= datetime.fromisoformat(result["valid_until"])
    assert result["reference_time"] == result["retrieved_at"]
    params = parse_qs(urlparse(calls[0]).query)
    assert params["timezone"] == ["Asia/Seoul"] and params["wind_speed_unit"] == ["ms"]
    provider.get(127, 37.5, arrival)
    assert len(calls) == 1
    assert provider.get(127, 37.5, arrival + timedelta(days=30))["status"] == "unknown"


def test_forecast_ttl_expiry_refreshes_and_network_failure_stays_unknown(monkeypatch):
    import hidden_view_finder.providers as providers
    arrival = (datetime.now(timezone.utc) + timedelta(hours=4)).replace(minute=0, second=0, microsecond=0)
    clock = [100.]
    monkeypatch.setattr(providers.time, "monotonic", lambda: clock[0])
    calls = []
    def open_response(request, timeout):
        calls.append(request.full_url)
        return io.BytesIO(json.dumps(_weather_payload(arrival)).encode())
    monkeypatch.setattr(providers, "urlopen", open_response)
    provider = ForecastProvider(enabled=True)
    provider.get(127, 37.5, arrival)
    clock[0] += 901
    provider.get(127, 37.5, arrival)
    assert len(calls) == 2
    def failure(*args, **kwargs):
        raise TimeoutError("fixture timeout")
    clock[0] += 901
    monkeypatch.setattr(providers, "urlopen", failure)
    result = provider.get(127, 37.5, arrival)
    assert result["status"] == "unknown"
    assert result["visibility_m"] is None


def test_solar_position_timezone_equivalence_and_naive_rejection():
    a = solar_position(127, 37.5, datetime.fromisoformat("2026-09-08T12:00:00+09:00"))
    b = solar_position(127, 37.5, datetime.fromisoformat("2026-09-08T03:00:00+00:00"))
    assert a["altitude_deg"] == b["altitude_deg"]
    assert a["azimuth_deg"] == b["azimuth_deg"]
    assert a["phase"] == "daylight"
    with pytest.raises(ValueError, match="timezone-aware"):
        solar_position(127, 37.5, datetime(2026, 9, 8))


def test_seoul_adapter_keeps_source_nodes_and_reports_effective_cell_without_mutating_raster(tmp_path):
    pytest.importorskip('osgeo', reason='Optional native adapter requires the GIS environment')
    from seoul_visibility import State
    nodes = [{"id": str(i), "lon": 127 + dx / 1000, "lat": 37.5 + dy / 1000}
             for i, (dx, dy) in enumerate([(0, 0), (5, 0), (0, -5), (5, -5)])]
    edges = [{"u": "0", "v": str(i), "length_m": 100 * i, "tags": {"highway": "footway"},
              "access_status": "unknown", "geometry": [[127, 37.5], [nodes[i]["lon"], nodes[i]["lat"]]]} for i in range(1, 4)]
    context = {"graph": {"nodes": nodes, "edges": edges}, "landmarks": [{"id": "test-tower", "name": "Fixture tower", "lon": 127., "lat": 37.5, "height_m": 80.}],
               "sources": [{"id": "fixture", "data_timestamp": "2026-09-08T00:00:00Z"}]}
    states = np.array([[State.VISIBLE, State.BLOCKED], [State.EXCLUDED, State.UNKNOWN]], dtype=np.uint8)
    original = states.copy()
    result = SimpleNamespace(states=states, transform=(0, 5, 0, 10, 0, -5), timings={},
                             metadata={"target": {"absolute_elevation_m": 100, "effective": {"lon": 127., "lat": 37.5}}, "backend": "fixture"})
    calls = []
    def viewshed(target, **kwargs):
        calls.append(kwargs)
        return result
    engine = SimpleNamespace(visible_from_target=viewshed,
        to_xy=SimpleNamespace(transform=lambda lon, lat: ((lon - 127) * 1000 + 1, (lat - 37.5) * 1000 + 9)),
        to_lonlat=SimpleNamespace(transform=lambda x, y: (127 + (x - 1) / 1000, 37.5 + (y - 9) / 1000)))
    weather_calls = []
    class RecordingWeather:
        def get(self, lon, lat, arrival):
            weather_calls.append(arrival)
            return {"status": "unknown", "requested_arrival": arrival.isoformat()}
    weather = RecordingWeather()
    adapter = SeoulAdapter(tmp_path / "manifest.json", tmp_path / "context.json", weather)
    adapter.context, adapter.engine, adapter.router = context, engine, WalkingRouter(context["graph"])
    adapter._open = lambda: None
    payload = Request.from_dict(dict(default_request(), mode="seoul", start={"lon": 127, "lat": 37.5})).to_dict()
    bundle = adapter.bundle(payload)
    assert len(calls) == 1
    assert calls[0]["eye_height_m"] == 1.7 and calls[0]["radius_m"] == 1800
    assert np.array_equal(states, original)
    assert {candidate["visibility"] for candidate in bundle["candidates"]} == {"visible", "blocked", "excluded", "unknown"}
    for candidate in bundle["candidates"]:
        node = next(n for n in nodes if candidate["id"] == f"osm-{n['id']}")
        assert (candidate["lon"], candidate["lat"]) == (node["lon"], node["lat"])
        assert candidate["visibility_evidence"]["snap_distance_m"] > 0
        assert candidate["visibility_evidence"]["effective_observer"] != {"lon": node["lon"], "lat": node["lat"]}
        assert candidate["crowd"]["status"] == "unknown"
        assert candidate["composition"]["foreground"] == "unknown"
        travel = candidate["route"]["travel_minutes"]
        if travel is not None:
            expected = datetime.fromisoformat(payload["visit_time"]) + timedelta(minutes=travel)
            assert candidate["weather"]["requested_arrival"] == expected.isoformat()

    supplied = {'status': 'observed', 'reference_time': payload['visit_time'], 'precipitation_mm': 2}
    payload['weather'] = supplied
    previous_calls = len(weather_calls)
    supplied_bundle = adapter.bundle(payload)
    assert len(weather_calls) == previous_calls
    assert all(c['weather']['provenance'] == 'caller_provided_unverified' for c in supplied_bundle['candidates'])
    assert supplied_bundle['weather']['reference_time'] == payload['visit_time']

    context['query_radius_m'] = 900
    context['path_label'] = '석촌호수 보행 경로'
    adapter.bundle(payload)
    assert calls[-1]['radius_m'] == 900
    context['query_radius_m'] = 0
    with pytest.raises(ValueError, match='query_radius_m'):
        adapter.bundle(payload)


def test_custom_region_bootstrap_uses_mapped_origin_without_opening_raster(tmp_path, monkeypatch):
    import hidden_view_finder.seoul as seoul_module
    monkeypatch.setattr(seoul_module, 'find_spec', lambda name: object())
    manifest, context = tmp_path/'manifest.json', tmp_path/'jamsil.json'
    manifest.write_text('{}')
    start = {'lon':127.10, 'lat':37.51, 'name':'Mapped Jamsil entry'}
    context.write_text(json.dumps({'schema_version':1, 'graph':{'nodes':[{'id':'1'}]},
        'landmarks':[{'name':'Lotte World Tower'}], 'default_start':start}))
    service = DemoService(tmp_path, manifest=manifest, context=context)
    bootstrap = service.bootstrap()
    assert bootstrap['seoul_start'] == start
    assert 'Lotte World Tower' in bootstrap['modes'][1]['description']
    assert service.seoul.engine is None
    service.close()


@pytest.fixture
def http_demo(tmp_path):
    from hidden_view_finder.server import DemoHTTPServer
    service = DemoService(data_root=tmp_path)
    server = DemoHTTPServer(("127.0.0.1", 0), service)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
        service.close()


def _http(port, path, method="GET", body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def test_http_scenario_roundtrip_and_static_assets(http_demo):
    status, headers, body = _http(http_demo, "/api/bootstrap")
    assert status == 200
    assert json.loads(body)["defaults"]["mode"] == "scenario"
    assert headers["X-Content-Type-Options"] == "nosniff"
    status, _, body = _http(http_demo, "/api/recommend", "POST", json.dumps(default_request()), {"Content-Type": "application/json"})
    assert status == 200
    result = json.loads(body)
    assert len(result["recommendations"]) == 3
    status, headers, body = _http(http_demo, "/")
    assert status == 200 and "text/html" in headers["Content-Type"]
    assert b"Hidden View Finder" in body
    assert _http(http_demo, "/static/styles.css")[0] == 200


@pytest.mark.parametrize("path", ["/data/demo/context.json", "/static/../service.py", "/static/%2e%2e/service.py", "/static/%2fetc/passwd", "/not-found"])
def test_http_does_not_serve_source_data_or_escape_static_directory(http_demo, path):
    assert _http(http_demo, path)[0] == 404


def test_http_errors_missing_data_invalid_body_and_cross_origin(http_demo):
    json_header = {"Content-Type": "application/json"}
    status, _, body = _http(http_demo, "/api/recommend", "POST", json.dumps(dict(default_request(), mode="seoul")), json_header)
    assert status == 409 and json.loads(body)["code"] == "missing_prepared_data"
    assert _http(http_demo, "/api/recommend", "POST", "{}", {"Content-Type": "text/plain"})[0] == 415
    assert _http(http_demo, "/api/recommend", "POST", "{}", dict(json_header, Origin="https://unrelated.example"))[0] == 403
    assert _http(http_demo, "/api/recommend", "POST", "", json_header)[0] == 413
    assert _http(http_demo, "/api/recommend", "POST", "null", json_header)[0] == 422
    assert _http(http_demo, "/api/recommend", "POST", '{"k":NaN}', json_header)[0] == 422
    assert _http(http_demo, "/api/recommend", "POST", "{broken", json_header)[0] == 422
