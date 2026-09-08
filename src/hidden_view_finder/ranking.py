"""Evidence-aware hard filters and transparent scenic recommendation heuristics.

Scores are a comparison aid, never a probability of visitor satisfaction. A
scenario can satisfy fictional constraints only inside scenario mode. Estimated
routes and unknown access are always separate from confirmed real recommendations.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, time, timedelta
import math
from typing import Any, Iterable, Mapping

from .models import RecommendationRequest, RequestError, aware_datetime


WEIGHTS = {"preferences": 0.30, "visibility": 0.25, "time_weather": 0.20, "travel": 0.15, "crowd": 0.10}
SCORING_NOTE = "Heuristic comparison score, not a validated probability of visitor satisfaction. Unknown criteria are omitted and remaining weights are renormalized; compare evidence coverage as well as score."


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def evidence_current(evidence: Mapping[str, Any], at: datetime, request: RecommendationRequest) -> bool:
    """Validate temporal applicability; reference time alone is not a forecast period."""
    status = evidence.get("status", "unknown")
    if status == "scenario":
        return request.mode == "scenario"
    if status not in {"observed", "forecast", "estimated"} or evidence.get("basis") == "residential_population":
        return False
    if not evidence.get("reference_time"):
        return False
    try:
        reference = aware_datetime(evidence["reference_time"], request.timezone, "reference_time")
        # A validity interval does not turn an old observation into a forecast.
        # Enforce this horizon before either timestamp-handling branch.
        if status == "observed" and not timedelta(0) <= at - reference <= timedelta(hours=2):
            return False
        if evidence.get("valid_from") and evidence.get("valid_until"):
            begin = aware_datetime(evidence["valid_from"], request.timezone, "valid_from")
            end = aware_datetime(evidence["valid_until"], request.timezone, "valid_until")
            return begin <= at <= end and reference <= at
        # An observation is relevant for at most two hours; never reuse it for
        # tomorrow's visit or treat a future observation as available today.
        return status == "observed" and timedelta(0) <= at - reference <= timedelta(hours=2)
    except RequestError:
        return False


def _trusted(status: Any, request: RecommendationRequest) -> bool:
    return status == "verified" or (status == "scenario" and request.mode == "scenario")


def _hours_cover(hours: Any, arrival: datetime, departure: datetime) -> bool | None:
    """An entire stay must fit one declared daily interval, including overnight."""
    if not isinstance(hours, list):
        return None
    if not hours:
        return False
    intervals: list[tuple[datetime, datetime]] = []
    try:
        for day_offset in (-1, 0, 1):
            day = arrival.date() + timedelta(days=day_offset)
            for entry in hours:
                start_text, end_text = entry["start"], entry["end"]
                start = datetime.combine(day, time.fromisoformat(start_text), arrival.tzinfo)
                if end_text in {"24:00", "24:00:00"}:
                    end = datetime.combine(day + timedelta(days=1), time.min, arrival.tzinfo)
                else:
                    end = datetime.combine(day, time.fromisoformat(end_text), arrival.tzinfo)
                    if end <= start:
                        end += timedelta(days=1)
                intervals.append((start, end))
    except (KeyError, TypeError, ValueError):
        return None
    # Merge touching opening intervals, so 09:00–12:00 plus 12:00–18:00
    # correctly supports a visit across noon.
    merged: list[list[datetime]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(end, merged[-1][1])
        else:
            merged.append([start, end])
    return any(start <= arrival and departure <= end for start, end in merged)


def check_constraints(request: RecommendationRequest, candidate: Mapping[str, Any]) -> dict[str, Any]:
    failures: list[str] = []
    unknown: list[str] = []
    visibility = candidate.get("visibility", "unknown")
    if visibility in {"blocked", "excluded"}:
        failures.append(f"Point visibility state is {visibility}; this endpoint cannot be recommended for the selected target")
    elif visibility != "visible":
        unknown.append("Point visibility is unknown")
    if candidate.get("evidence_kind") == "scenario" and request.mode != "scenario":
        unknown.append("Fictional scenario evidence cannot verify a real Seoul visit")
    route = candidate.get("route") or {}
    access = candidate.get("access") or {}
    travel = _number(route.get("travel_minutes"))
    walk = _number(route.get("walking_m"))
    trusted_route = _trusted(route.get("status"), request)
    if route.get("mode") != request.transport_mode:
        unknown.append("A route for the selected transport mode is not verified")
        trusted_route = False
    if travel is None or travel < 0:
        unknown.append("Travel time is unknown")
        travel = None
    elif travel > request.max_travel_minutes:
        failures.append("Travel time exceeds the maximum")
    elif not trusted_route:
        unknown.append("Travel time is estimated or unverified")
    if walk is None or walk < 0:
        unknown.append("Walking distance is unknown")
    elif walk > request.max_walk_m:
        failures.append("Walking distance exceeds the maximum")
    elif not trusted_route:
        unknown.append("Walking distance is estimated or unverified")
    public = access.get("public")
    if public is False:
        failures.append("Public entry is prohibited")
    elif public is not True or not _trusted(access.get("public_status", access.get("status")), request):
        unknown.append("Public access is unverified")
    for required, key, label in ((request.wheelchair, "wheelchair", "Wheelchair access"), (request.stroller, "stroller", "Stroller access")):
        if required:
            value = route.get(key)
            if value is False or access.get("step_free") is False:
                failures.append(f"{label} requirement is not met")
            elif value is not True or access.get("step_free") is not True or not trusted_route:
                unknown.append(f"{label} is unverified for the complete route and standing location")
    if request.no_stairs:
        if route.get("stairs") is True or access.get("step_free") is False:
            failures.append("The route or standing location has stairs")
        elif route.get("stairs") is not False or access.get("step_free") is not True or not trusted_route:
            unknown.append("Absence of stairs is unverified")
    if request.max_slope_percent is not None:
        slope = _number(route.get("slope_percent"))
        if slope is None or not trusted_route:
            unknown.append("Maximum route slope is unverified")
        elif slope > request.max_slope_percent:
            failures.append("Route slope exceeds the maximum")
    arrival = request.visit_time + timedelta(minutes=travel) if travel is not None else None
    departure = arrival + timedelta(minutes=request.stay_minutes) if arrival else None
    if arrival and departure:
        if departure > request.available_until:
            failures.append("Arrival plus the requested stay exceeds the available window")
        covered = _hours_cover(access.get("opening_hours"), arrival, departure)
        if covered is False:
            failures.append("The spot is closed during part or all of the planned stay")
        elif covered is None or not _trusted(access.get("opening_hours_status"), request):
            unknown.append("Opening hours for the entire planned stay are unverified")
    else:
        unknown.append("Arrival time and opening hours cannot be checked without a route")
    return {"status": "failed" if failures else "unverified" if unknown else "passed", "reasons": failures + unknown,
            "failures": failures, "unknown": unknown,
            "arrival_at": arrival.isoformat() if arrival else None, "departure_at": departure.isoformat() if departure else None}


def score_candidate(request: RecommendationRequest, candidate: Mapping[str, Any], arrival_at: str | None = None) -> dict[str, Any]:
    criteria: dict[str, float | None] = dict.fromkeys(WEIGHTS)
    notes: dict[str, str] = {}
    features = set(candidate.get("scenic_features") or [])
    if features and candidate.get("scenic_features_status") in {"verified", "computed", "estimated", "scenario"} and request.preferences:
        if candidate.get("scenic_features_status") != "scenario" or request.mode == "scenario":
            criteria["preferences"] = 100 * len(features.intersection(request.preferences)) / len(request.preferences)
            notes["preferences"] = "Fraction of requested scene tags supported by the listed feature evidence; map estimates remain estimates."
    if candidate.get("visibility") == "visible":
        # A known single point alone supplies limited composition evidence.
        # Never award whole-building visibility from this state.
        criteria["visibility"] = 60.0
        notes["visibility"] = "60/100 means a visible target point only; it does not establish whole-scene composition."
        composition = _number(candidate.get("composition_score"))
        if composition is not None and _trusted(candidate.get("composition_status"), request):
            criteria["visibility"] = max(0., min(100., composition))
            notes["visibility"] = "Provider-supplied composition heuristic with explicit supporting evidence."
    route = candidate.get("route") or {}
    travel = _number(route.get("travel_minutes"))
    if travel is not None and travel >= 0 and route.get("mode") == request.transport_mode and route.get("status") in {"verified", "estimated", "scenario"}:
        if route.get("status") != "scenario" or request.mode == "scenario":
            criteria["travel"] = max(0., 100 * (1 - travel / request.max_travel_minutes))
            notes["travel"] = "Travel-time convenience within the user's limit; estimated routes remain unverified hard constraints."
    at = aware_datetime(arrival_at, request.timezone, "arrival_at") if arrival_at else request.visit_time
    weather = candidate.get("weather") or request.weather
    if evidence_current(weather, at, request):
        rain, wind, visibility = (_number(weather.get(key)) for key in ("precipitation_mm", "wind_m_s", "visibility_m"))
        penalties = []
        if rain is not None and rain >= 0:
            penalties.append(min(70., rain * 12.))
        if wind is not None and wind >= 0:
            penalties.append(min(50., max(0., wind - 3) * 6.))
        if visibility is not None and visibility >= 0:
            penalties.append(40 * max(0., 1 - visibility / 10000.))
        if all(value is not None and value >= 0 for value in (rain, wind, visibility)):
            criteria["time_weather"] = max(0., 100 - sum(penalties))
            notes["time_weather"] = "Weather heuristic at arrival: rain, wind and reported visibility; no inferred sunset lighting."
        else:
            notes["time_weather"] = "Rain, wind and visibility are all required for this weather heuristic; missing variables are not treated as zero penalties."
    time_suitability = candidate.get("time_suitability") or {}
    supplied_time = _number(time_suitability.get("score"))
    if supplied_time is not None and evidence_current(time_suitability, at, request):
        criteria["time_weather"] = max(0., min(100., supplied_time))
        notes["time_weather"] = str(time_suitability.get("explanation", "Explicit time/weather scenario or dated estimate"))
    crowd = candidate.get("crowd") or {}
    if request.crowd_preference != "any" and evidence_current(crowd, at, request):
        level = crowd.get("level")
        crowd_scores = {"quiet": {"low": 100., "medium": 45., "high": 0.}, "balanced": {"low": 70., "medium": 100., "high": 35.}, "lively": {"low": 20., "medium": 65., "high": 100.}}
        criteria["crowd"] = crowd_scores[request.crowd_preference].get(level)
        notes["crowd"] = "Time-relevant visitor crowd evidence; residential population is never substituted."
    # Explicit provider scores allow later evaluators to replace these simple
    # heuristics, while requiring the corresponding evidence to exist already.
    overrides = candidate.get("criteria") or {}
    for key in WEIGHTS:
        if key in overrides:
            value = _number(overrides[key])
            if overrides[key] is None:
                criteria[key] = None
            elif value is not None and criteria[key] is not None:
                if not 0 <= value <= 100:
                    raise ValueError(f"Criterion {key} must be between 0 and 100")
                criteria[key] = value
    coverage = sum(WEIGHTS[key] for key, score in criteria.items() if score is not None)
    score = sum(WEIGHTS[key] * value for key, value in criteria.items() if value is not None) / coverage if coverage else None
    return {"score": round(score, 2) if score is not None else None, "evidence_coverage": round(coverage, 4), "criteria": criteria, "criterion_notes": notes}


def distance_m(a: Mapping[str, Any], b: Mapping[str, Any]) -> float:
    lon1, lat1, lon2, lat2 = (math.radians(float(value)) for value in (a["lon"], a["lat"], b["lon"], b["lat"]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371008.8 * 2 * math.asin(min(1., math.sqrt(h)))


def same_experience(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    bearing_a, bearing_b = _number(a.get("bearing_deg")), _number(b.get("bearing_deg"))
    targets_a, targets_b = set(a.get("target_ids") or []), set(b.get("target_ids") or [])
    signature = a.get("composition_signature")
    if bearing_a is None or bearing_b is None or not targets_a or targets_a != targets_b or not signature or signature != b.get("composition_signature"):
        return False
    return abs((bearing_a - bearing_b + 180) % 360 - 180) <= 20 and distance_m(a, b) <= 120


def rank_candidates(request: RecommendationRequest | Mapping[str, Any], candidates: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    request = request if isinstance(request, RecommendationRequest) else RecommendationRequest.from_dict(request)
    groups: dict[str, list[dict[str, Any]]] = {"passed": [], "unverified": [], "failed": []}
    seen_ids: set[str] = set()
    for original in candidates:
        candidate = deepcopy(dict(original))
        candidate_id = str(candidate.get("id", ""))
        if not candidate_id or candidate_id in seen_ids:
            raise ValueError("Candidate ids must be nonempty and unique")
        seen_ids.add(candidate_id)
        if candidate.get("visibility", "unknown") not in {"visible", "blocked", "excluded", "unknown"}:
            raise ValueError(f"Candidate {candidate_id} has an invalid visibility state")
        for key, bound in (("lon", 180), ("lat", 90)):
            value = _number(candidate.get(key))
            if value is None or not -bound <= value <= bound:
                raise ValueError(f"Candidate {candidate_id} has invalid {key}")
        constraints = check_constraints(request, candidate)
        candidate.update(score_candidate(request, candidate, constraints["arrival_at"]))
        candidate.update(hard_constraints=constraints, hard_constraint_status=constraints["status"], arrival_at=constraints["arrival_at"], departure_at=constraints["departure_at"])
        groups[constraints["status"]].append(candidate)
    sort_key = lambda item: (-(item["score"] if item["score"] is not None else -1), -item["evidence_coverage"], str(item["id"]))
    duplicates: list[dict[str, Any]] = []
    def diverse(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        representatives: list[dict[str, Any]] = []
        for item in sorted(items, key=sort_key):
            representative = next((other for other in representatives if same_experience(item, other)), None)
            if representative:
                item["duplicate_of"] = representative["id"]
                duplicates.append(item)
            else:
                representatives.append(item)
        for rank, item in enumerate(representatives, 1):
            item["rank"] = rank
        return representatives
    qualified = diverse(groups["passed"])
    unverified = diverse(groups["unverified"])
    selected = qualified[:request.k]
    return {"recommendations": selected, "unverified": unverified, "excluded": sorted(groups["failed"], key=lambda item: str(item["id"])),
            "duplicates": duplicates, "qualified_count": len(qualified), "requested_k": request.k,
            "shortfall": max(0, request.k - len(selected)), "weights": WEIGHTS.copy(), "scoring_note": SCORING_NOTE,
            "relaxation_suggestions": (["Consider a longer travel/window allowance or different visiting hours; mandatory access and unknown evidence still require verification."] if len(selected) < request.k else [])}
