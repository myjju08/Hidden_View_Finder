"""Recommendation contract tests using only explicitly fictional evidence."""
from copy import deepcopy
from datetime import timedelta

import pytest

from hidden_view_finder.models import Request, RequestError
from hidden_view_finder.ranking import check_constraints, evidence_current, rank_candidates, same_experience, score_candidate
from hidden_view_finder.scenarios import default_request, scenario_bundle


def request(**updates):
    data = default_request()
    data.update(updates)
    return Request.from_dict(data)


def spot(req=None):
    return scenario_bundle(req or request())["candidates"][0]


def test_default_ranking_is_diverse_deterministic_and_explicitly_fictional():
    req = request()
    bundle = scenario_bundle(req)
    first = rank_candidates(req, bundle["candidates"])
    assert first == rank_candidates(req, reversed(bundle["candidates"]))
    assert [item["id"] for item in first["recommendations"]] == ["river_steps", "forest_window", "garden_frame"]
    assert len(first["recommendations"]) == 3
    assert [item["id"] for item in first["duplicates"]] == ["river_nearby"]
    assert {item["visibility"] for item in bundle["candidates"]} == {"visible", "blocked", "excluded", "unknown"}
    assert all(item["evidence_kind"] == "scenario" for item in first["recommendations"])
    assert first["recommendations"][2]["evidence_coverage"] == .9
    assert "not a validated probability" in first["scoring_note"]


def test_ranker_does_not_mutate_provider_records():
    req = request()
    candidates = scenario_bundle(req)["candidates"]
    before = deepcopy(candidates)
    rank_candidates(req, candidates)
    assert candidates == before


def test_unknown_scores_are_omitted_not_replaced_with_midpoint():
    req = request()
    candidate = spot(req)
    candidate["criteria"] = {"preferences": 80, "visibility": 40, "time_weather": None, "travel": None, "crowd": None}
    result = score_candidate(req, candidate)
    assert result["evidence_coverage"] == .55
    assert result["score"] == pytest.approx((.30 * 80 + .25 * 40) / .55, abs=.005)
    assert result["criteria"]["time_weather"] is None


def test_no_evidence_means_no_score_and_overrides_cannot_invent_evidence():
    req = request()
    candidate = {"visibility": "unknown", "criteria": {key: 50 for key in ("preferences", "visibility", "time_weather", "travel", "crowd")}}
    result = score_candidate(req, candidate)
    assert result["score"] is None
    assert result["evidence_coverage"] == 0
    assert all(value is None for value in result["criteria"].values())


def test_partial_weather_does_not_assume_missing_wind_and_visibility_are_favorable():
    req = request(mode='seoul')
    weather = {'status': 'observed', 'reference_time': req.visit_time.isoformat(), 'precipitation_mm': 0}
    result = score_candidate(req, {'weather': weather})
    assert result['criteria']['time_weather'] is None
    assert result['evidence_coverage'] == 0
    weather.update(wind_m_s=2, visibility_m=12000)
    complete = score_candidate(req, {'weather': weather})
    assert complete['criteria']['time_weather'] == 100
    assert complete['evidence_coverage'] == .2


def test_point_visibility_is_not_whole_composition_score():
    candidate = spot()
    candidate.pop("composition_score")
    score = score_candidate(request(), candidate)
    assert score["criteria"]["visibility"] == 60
    assert "does not establish whole-scene" in score["criterion_notes"]["visibility"]


@pytest.mark.parametrize("key,value", [("public", False), ("opening_hours", [])])
def test_hard_access_failure_never_enters_recommendations(key, value):
    candidate = spot()
    candidate["access"][key] = value
    result = rank_candidates(request(), [candidate])
    assert not result["recommendations"]
    assert result["excluded"][0]["hard_constraint_status"] == "failed"
    assert result["shortfall"] == 3


@pytest.mark.parametrize("field", ["public", "opening_hours"])
def test_unknown_access_is_separate_from_confirmed(field):
    candidate = spot()
    candidate["access"][field] = None
    result = rank_candidates(request(), [candidate])
    assert not result["recommendations"] and not result["excluded"]
    assert result["unverified"][0]["hard_constraint_status"] == "unverified"


@pytest.mark.parametrize("state", ["blocked", "excluded"])
def test_blocked_and_excluded_remain_distinct_and_never_recommended(state):
    candidate = spot()
    candidate["visibility"] = state
    result = rank_candidates(request(), [candidate])
    assert result["excluded"][0]["visibility"] == state
    assert not result["recommendations"]


def test_unknown_visibility_preserved():
    candidate = spot()
    candidate["visibility"] = "unknown"
    result = rank_candidates(request(), [candidate])
    assert result["unverified"][0]["visibility"] == "unknown"
    assert result["unverified"][0]["criteria"]["visibility"] is None


def test_estimated_route_does_not_verify_hard_travel_constraint():
    candidate = spot()
    candidate["route"]["status"] = "estimated"
    result = rank_candidates(request(), [candidate])
    assert not result["recommendations"]
    assert result["unverified"][0]["criteria"]["travel"] is not None


@pytest.mark.parametrize("requirement", ["wheelchair", "stroller", "no_stairs"])
def test_mandatory_accessibility_checks_entire_route_and_endpoint(requirement):
    req = request(**{requirement: True})
    candidate = spot(req)
    candidate["access"]["step_free"] = False
    assert check_constraints(req, candidate)["status"] == "failed"
    candidate["access"]["step_free"] = None
    assert check_constraints(req, candidate)["status"] == "unverified"


def test_stairs_and_slope_are_hard_constraints():
    req = request(no_stairs=True, max_slope_percent=3)
    candidate = spot(req)
    candidate["route"]["stairs"] = True
    candidate["route"]["slope_percent"] = 10
    constraints = check_constraints(req, candidate)
    assert constraints["status"] == "failed"
    assert len(constraints["failures"]) == 2


def test_different_transport_route_not_reused_as_confirmed():
    candidate = spot()
    result = rank_candidates(request(transport_mode="transit"), [candidate])
    assert result["unverified"][0]["criteria"]["travel"] is None


def test_limit_checks_and_entire_stay_not_just_arrival():
    req = request(visit_time="2026-09-08T17:25:00+09:00", available_until="2026-09-08T18:00:00+09:00", stay_minutes=30)
    candidate = spot(req)
    candidate["route"]["travel_minutes"] = 10
    candidate["access"]["opening_hours"] = [{"start": "09:00", "end": "18:00"}]
    constraints = check_constraints(req, candidate)
    assert constraints["arrival_at"].startswith("2026-09-08T17:35")
    assert constraints["departure_at"].startswith("2026-09-08T18:05")
    assert len(constraints["failures"]) == 2


def test_exact_closing_boundary_is_allowed_but_one_minute_later_is_not():
    req = request(visit_time="2026-09-08T17:20:00+09:00", available_until="2026-09-08T19:00:00+09:00")
    candidate = spot(req)
    candidate["route"]["travel_minutes"] = 10
    candidate["access"]["opening_hours"] = [{"start": "09:00", "end": "18:00"}]
    assert check_constraints(req, candidate)["status"] == "passed"
    candidate["route"]["travel_minutes"] = 11
    assert check_constraints(req, candidate)["status"] == "failed"


def test_overnight_hours_and_adjacent_intervals():
    req = request(visit_time="2026-09-08T23:30:00+09:00", available_until="2026-09-09T02:00:00+09:00")
    candidate = spot(req)
    candidate["route"]["travel_minutes"] = 10
    candidate["access"]["opening_hours"] = [{"start": "20:00", "end": "02:00"}]
    assert check_constraints(req, candidate)["status"] == "passed"
    candidate["access"]["opening_hours"] = [{"start": "20:00", "end": "24:00"}, {"start": "00:00", "end": "02:00"}]
    assert check_constraints(req, candidate)["status"] == "passed"


def test_missing_route_cannot_check_arrival_or_hours():
    candidate = spot()
    candidate["route"]["travel_minutes"] = None
    constraints = check_constraints(request(), candidate)
    assert constraints["arrival_at"] is None
    assert constraints["status"] == "unverified"


def test_actual_real_mode_never_accepts_fictional_evidence():
    result = rank_candidates(request(mode="seoul"), [spot()])
    assert not result["recommendations"]
    assert len(result["unverified"]) == 1
    with pytest.raises(ValueError, match="only in scenario mode"):
        scenario_bundle(request(mode="seoul"))


def test_stale_weather_crowds_and_residential_density_stay_unknown():
    req = request(mode="seoul")
    candidate = spot()
    candidate.pop("time_suitability")
    candidate["weather"] = {"status": "observed", "reference_time": "2026-09-07T16:00:00+09:00", "precipitation_mm": 0}
    candidate["crowd"] = {"status": "estimated", "reference_time": req.visit_time.isoformat(), "valid_from": req.visit_time.isoformat(),
                          "valid_until": req.available_until.isoformat(), "basis": "residential_population", "level": "low"}
    result = score_candidate(req, candidate)
    assert result["criteria"]["time_weather"] is None
    assert result["criteria"]["crowd"] is None


def test_forecast_needs_validity_interval_and_reference_time_not_future_observation():
    req = request(mode="seoul")
    evidence = {"status": "forecast", "reference_time": "2026-09-08T10:00:00+09:00"}
    assert not evidence_current(evidence, req.visit_time, req)
    evidence.update(valid_from="2026-09-08T15:00:00+09:00", valid_until="2026-09-08T18:00:00+09:00")
    assert evidence_current(evidence, req.visit_time, req)
    assert not evidence_current(evidence, req.visit_time + timedelta(days=1), req)
    evidence["reference_time"] = "2026-09-08T17:00:00+09:00"
    assert not evidence_current(evidence, req.visit_time, req)


@pytest.mark.parametrize("age_hours,expected", [(-1, False), (0, True), (2, True), (2.001, False), (24, False)])
def test_observed_validity_interval_cannot_bypass_two_hour_freshness(age_hours, expected):
    req = request(mode="seoul")
    evidence = {"status": "observed", "reference_time": (req.visit_time - timedelta(hours=age_hours)).isoformat(),
                "valid_from": (req.visit_time - timedelta(days=2)).isoformat(),
                "valid_until": (req.visit_time + timedelta(days=2)).isoformat()}
    assert evidence_current(evidence, req.visit_time, req) is expected


def test_scenario_view_directions_point_at_the_declared_target_coordinates():
    import math
    bundle = scenario_bundle(request())
    landmarks = {landmark["id"]: landmark for landmark in bundle["landmarks"]}
    for candidate in bundle["candidates"]:
        target = landmarks[candidate["target_ids"][0]]
        # Independent local equirectangular check is ample at these sub-3 km
        # fictional distances: the map arrow must point at the target marker.
        dx = (target["lon"] - candidate["lon"]) * math.cos(math.radians((target["lat"] + candidate["lat"]) / 2))
        dy = target["lat"] - candidate["lat"]
        expected = math.degrees(math.atan2(dx, dy)) % 360
        assert abs((candidate["bearing_deg"] - expected + 180) % 360 - 180) < .1
        assert candidate["bearing_evidence"]["status"] == "computed"
    selected = rank_candidates(request(), bundle["candidates"])["recommendations"]
    assert [(candidate["id"], candidate["score"]) for candidate in selected] == [("river_steps", 92.13), ("forest_window", 74.68), ("garden_frame", 70.7)]


def test_no_crowd_preference_omits_weight_instead_of_perfect_unknown_score():
    result = score_candidate(request(crowd_preference="any"), spot())
    assert result["criteria"]["crowd"] is None
    assert result["evidence_coverage"] == .9


def test_dedup_requires_distance_target_direction_and_composition_and_handles_north_wrap():
    a = spot()
    b = deepcopy(a)
    b["lon"] += .0001
    a["bearing_deg"], b["bearing_deg"] = 355, 5
    assert same_experience(a, b)
    for key, value in (("bearing_deg", 40), ("target_ids", ["different"]), ("composition_signature", "different"), ("lon", a["lon"] + .01)):
        changed = dict(b, **{key: value})
        assert not same_experience(a, changed)


def test_less_than_k_returns_only_qualifying_without_promoting_unverified():
    req = request(k=10)
    result = rank_candidates(req, scenario_bundle(req)["candidates"])
    assert len(result["recommendations"]) == 5
    assert result["shortfall"] == 5
    assert all(item["hard_constraint_status"] == "passed" for item in result["recommendations"])


def test_preferences_and_time_change_result_and_close_daytime_spots():
    req = request(preferences=["city", "skyline", "night"], crowd_preference="lively", visit_time="2026-09-08T20:00:00+09:00", available_until="2026-09-08T23:00:00+09:00")
    result = rank_candidates(req, scenario_bundle(req)["candidates"])
    assert result["recommendations"][0]["id"] == "skyline_terrace"
    assert {"garden_frame", "forest_window"}.issubset({item["id"] for item in result["excluded"]})


def test_small_travel_budget_and_changed_start_affect_routes():
    req = request(max_travel_minutes=1)
    result = rank_candidates(req, scenario_bundle(req)["candidates"])
    assert not result["recommendations"]
    original = spot()
    moved = spot(request(start={"lon": 127.1, "lat": 37.6}))
    assert moved["route"]["travel_minutes"] > original["route"]["travel_minutes"]


@pytest.mark.parametrize("updates", [
    {"start": {"lon": float("nan"), "lat": 37.5}}, {"start": {"lon": 37.5, "lat": 127}},
    {"visit_time": "not-a-time"}, {"timezone": "Not/AZone"}, {"k": 0}, {"k": 2.5}, {"k": True},
    {"max_walk_m": -1}, {"max_travel_minutes": float("inf")}, {"wheelchair": "false"},
    {"mode": "real"}, {"transport_mode": "helicopter"}, {"preferences": ["made_up"]},
    {"crowd_preference": "empty"}, {"available_until": "2026-09-08T15:00:00+09:00"},
    {"stay_minutes": 9999}, {"eye_height_m": 0}, {"weather": "sunny"},
])
def test_request_validation(updates):
    with pytest.raises(RequestError):
        request(**updates)


def test_timezone_axis_order_defaults_and_serialization_roundtrip():
    req = request(visit_time="2026-09-08T07:00:00Z")
    assert req.visit_time.hour == 16
    assert req.start.lon == 126.978 and req.start.lat == 37.571
    assert Request.from_dict(req.to_dict()) == req
    local = request(visit_time="2026-09-08T16:00:00")
    assert local.visit_time == req.visit_time


def test_duplicate_ids_invalid_coordinates_and_visibility_rejected():
    candidate = spot()
    with pytest.raises(ValueError, match="unique"):
        rank_candidates(request(), [candidate, candidate])
    with pytest.raises(ValueError, match="invalid lon"):
        rank_candidates(request(), [dict(candidate, lon=float("nan"))])
    with pytest.raises(ValueError, match="visibility state"):
        rank_candidates(request(), [dict(candidate, visibility="probably_visible")])
