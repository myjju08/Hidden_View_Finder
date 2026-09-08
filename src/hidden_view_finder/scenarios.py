"""Small fictional scene fixtures: executable without Seoul files or external APIs.

These coordinates locate a demonstration diagram. Names, terrain, routes,
opening hours, weather, crowd levels and visibility states are invented fixtures,
not evidence about the corresponding locations in Seoul. No native viewshed is
claimed or run by this module; the separate real-data provider uses that engine.
"""
from __future__ import annotations

from datetime import timedelta
import math
from typing import Any

from .models import RecommendationRequest
from .providers import bearing_deg
from .ranking import distance_m


def default_request() -> dict[str, Any]:
    return {"mode": "scenario", "start": {"lon": 126.978, "lat": 37.571, "name": "가상 출발점 · 실제 방문 장소 아님"},
            "visit_time": "2026-09-08T16:00:00+09:00", "available_until": "2026-09-08T19:30:00+09:00", "timezone": "Asia/Seoul",
            "stay_minutes": 30, "purpose": "풍경을 보며 산책하고 사진 찍기", "group": "성인 2명", "activity": "photography",
            "preferences": ["nature", "river"], "crowd_preference": "quiet", "transport_mode": "walking",
            "max_travel_minutes": 45, "max_walk_m": 2500, "wheelchair": False, "stroller": False, "no_stairs": False,
            "max_slope_percent": None, "eye_height_m": 1.7, "k": 3}


def _location(dx: float, dy: float) -> tuple[float, float]:
    return 126.978 + dx / (111320 * math.cos(math.radians(37.571))), 37.571 + dy / 111320


def scenario_bundle(request: RecommendationRequest | dict[str, Any]) -> dict[str, Any]:
    request = request if isinstance(request, RecommendationRequest) else RecommendationRequest.from_dict(request)
    if request.mode != "scenario":
        raise ValueError("Fictional scenario fixtures are available only in scenario mode")
    definitions = [
        ("river_steps", "가상 물가 데크", 500, 100, ["river_point"], ["nature", "river", "water", "bridge"], "water-bridge-horizon", "visible", 92, "low", False, 2, "24:00"),
        ("river_nearby", "가상 물가 데크 옆", 545, 110, ["river_point"], ["nature", "river", "water", "bridge"], "water-bridge-horizon", "visible", 86, "low", False, 2, "24:00"),
        ("forest_window", "가상 숲 전망 쉼터", -700, 600, ["ridge_point"], ["nature", "forest", "mountain", "greenery"], "forest-ridge", "visible", 91, "low", False, 4, "19:00"),
        ("garden_frame", "가상 정원 전망대", -500, -550, ["garden_point"], ["nature", "park", "greenery"], "garden-water", "visible", 83, None, False, 2, "18:00"),
        ("skyline_terrace", "가상 도시 테라스", 1000, 650, ["tower_point"], ["city", "skyline", "night"], "terrace-tower-city", "visible", 94, "high", True, 10, "23:00"),
        ("bridge_walk", "가상 다리 보행쉼터", 900, -700, ["bridge_point"], ["city", "bridge", "river", "sunset"], "bridge-river", "visible", 79, "medium", False, 3, "24:00"),
        ("wall_shadow", "가상 벽 뒤 후보", 200, 700, ["tower_point"], ["city", "skyline"], "wall-tower", "blocked", 0, "low", False, 2, "24:00"),
        ("unknown_lane", "가상 미확인 골목", -200, 500, ["ridge_point"], ["nature", "mountain"], "lane-ridge", "unknown", None, None, False, 2, "24:00"),
        ("roof_cell", "가상 건물 점유 셀", 300, -150, ["tower_point"], ["city", "skyline"], "roof-tower", "excluded", None, "low", False, 2, "24:00"),
        ("private_court", "가상 출입금지 마당", 50, -700, ["tower_point"], ["city", "skyline", "nature"], "court-tower", "visible", 95, "low", False, 1, "24:00"),
    ]
    candidates: list[dict[str, Any]] = []
    for identifier, name, dx, dy, targets, features, signature, visibility, composition, crowd, stairs, slope, closing in definitions:
        lon, lat = _location(dx, dy)
        # Deterministic invented path lengths, explicitly not a real pedestrian
        # route. Changing the starting point still changes this demo's outcome.
        path_m = distance_m({"lon": request.start.lon, "lat": request.start.lat}, {"lon": lon, "lat": lat}) * 1.2 + 80
        walking_m = path_m if request.transport_mode == "walking" else min(450, path_m * .2)
        travel = path_m / 75 if request.transport_mode == "walking" else 8 + path_m / (250 if request.transport_mode == "transit" else 400)
        arrival = request.visit_time + timedelta(minutes=travel)
        evening = arrival.hour >= 18 or arrival.hour < 6
        time_score = 45 if evening and "forest" in features else 92 if evening and "night" in features else 86
        weather = {"status": "scenario", "reference_time": request.visit_time.isoformat(), "valid_from": request.visit_time.isoformat(),
                   "valid_until": request.available_until.isoformat(), "precipitation_mm": 0, "cloud_cover_percent": 35, "visibility_m": 12000, "wind_m_s": 2,
                   "source": "Fictional fixture, not an observed or forecast Seoul weather report"}
        candidates.append({"id": identifier, "name": name, "lon": lon, "lat": lat, "field_of_view_deg": 60,
            "target_ids": targets, "scenic_features": features, "scenic_features_status": "scenario", "composition_signature": signature,
            "visibility": visibility, "visibility_description": f"Fictional scenario state: {visibility}; no native Seoul viewshed was run",
            "evidence_kind": "scenario", "composition_score": composition, "composition_status": "scenario",
            "route": {"status": "scenario", "mode": request.transport_mode, "travel_minutes": round(travel, 2), "walking_m": round(walking_m),
                      "wheelchair": not stairs, "stroller": not stairs, "stairs": stairs, "slope_percent": slope,
                      "geometry": [[request.start.lon, request.start.lat], [(request.start.lon + lon) / 2, request.start.lat], [lon, lat]],
                      "source": "Invented scenario route; not a real navigation instruction"},
            "access": {"public": identifier != "private_court", "public_status": "scenario", "opening_hours": [{"start": "00:00" if closing == "24:00" else "09:00", "end": closing}],
                       "opening_hours_status": "scenario", "step_free": not stairs},
            "crowd": {"level": crowd, "status": "scenario" if crowd is not None else "unknown", "reference_time": request.visit_time.isoformat(),
                      "valid_from": request.visit_time.isoformat(), "valid_until": request.available_until.isoformat(), "basis": "fictional_visitors", "source": "Scenario visitor counts, not residential population"},
            "weather": weather, "time_suitability": {"score": time_score, "status": "scenario", "reference_time": request.visit_time.isoformat(),
                      "explanation": "Fictional evening/daytime suitability; not an astronomical or weather forecast"},
            "features": [{"name": feature, "status": "scenario", "confidence": "fictional", "source_id": "scenario-v1"} for feature in features],
            "uncertainties": ["모든 장소명·장면·경로·개방시간·혼잡·날씨는 가상 시나리오입니다. 실제 서울 방문에 사용하지 마세요.", "A visible point does not establish an entire scene; the real model omits trees, walls and construction unless represented."],
            "sources": ["scenario-v1"], "standing_instruction": "가상 표시 지점의 중심에 서는 시나리오입니다. 실제 보행 안내가 아닙니다."})
    # This candidate demonstrates access uncertainty separately from unknown LOS.
    candidates[-3]["access"]["public"] = None
    landmarks = []
    for identifier, name, kind, dx, dy, features in [
        ("river_point", "가상 강 수면의 대표 점", "river", 1300, 150, ["river", "nature"]),
        ("ridge_point", "가상 산 능선의 한 점", "mountain", -1100, 1400, ["mountain", "forest"]),
        ("garden_point", "가상 정원의 대표 점", "park", -900, -1000, ["park", "greenery"]),
        ("tower_point", "가상 타워 꼭대기 점", "building", 1800, 1000, ["city", "skyline"]),
        ("bridge_point", "가상 다리의 대표 점", "bridge", 1100, -300, ["river", "bridge"]),
    ]:
        lon, lat = _location(dx, dy)
        landmarks.append({"id": identifier, "name": name, "type": kind, "lon": lon, "lat": lat, "features": features,
                          "target": {"lon": lon, "lat": lat, "height_m": 80 if kind == "building" else 1, "height_reference": "agl"},
                          "engine_support": "fictional_point_fixture", "geometry_status": "scenario", "source_ids": ["scenario-v1"],
                          "uncertainties": ["Fictional coordinates and heights; no actual mapped landmark is asserted. A representative point cannot prove an entire river, ridge or skyline visible."]})
    targets = {landmark["id"]: landmark for landmark in landmarks}
    for candidate in candidates:
        target = targets[candidate["target_ids"][0]]
        candidate["bearing_deg"] = round(bearing_deg((candidate["lon"], candidate["lat"]), (target["lon"], target["lat"])), 2)
        candidate["bearing_evidence"] = {"status": "computed", "method": "Initial great-circle bearing to the first target's fictional coordinates",
                                         "scope": "Consistent scenario diagram direction, not actual Seoul viewing geometry"}
    return {"landmarks": landmarks, "candidates": candidates,
            "weather": candidates[0]["weather"], "mode": "scenario",
            "assumptions": ["This is an explicitly fictional scenario with invented scenes, paths, heights, opening hours, crowds and weather; no real recommendation is asserted.",
                            "visit_time is earliest departure; arrival includes route travel, and the entire stay must fit available_until and opening hours.",
                            "A requested return journey is not modeled; available_until means departure from the viewing spot.",
                            "The real-data mode is separate and does not inherit fictional evidence."],
            "sources": [{"id": "scenario-v1", "name": "Deterministic fictional fixtures included in scenarios.py", "kind": "scenario", "url": None,
                         "reference_time": request.visit_time.isoformat(), "retrieved_at": None}],
            "limitations": ["Scenario visibility states are fixtures, not computed Seoul visibility results.", "Any illustrated view is not a reconstruction of a real location."]}
