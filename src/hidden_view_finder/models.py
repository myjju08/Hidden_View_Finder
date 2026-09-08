"""Validated, dependency-free request models for the recommendation demo."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import math
from typing import Any, Literal, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class RequestError(ValueError):
    """A request cannot be evaluated without correcting its explicit inputs."""


def finite_number(value: Any, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        raise RequestError(f"{name} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RequestError(f"{name} must be a finite number") from exc
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise RequestError(f"{name} must be between {minimum} and {maximum}")
    return number


def aware_datetime(value: Any, timezone: str, name: str) -> datetime:
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, TypeError) as exc:
        raise RequestError(f"Unknown timezone: {timezone!r}") from exc
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise RequestError(f"{name} must be an ISO date and time") from exc
    if parsed.tzinfo is None:
        # Reject nonexistent and ambiguous local clock times rather than guessing.
        first = parsed.replace(tzinfo=zone, fold=0)
        second = parsed.replace(tzinfo=zone, fold=1)
        if first.utcoffset() != second.utcoffset():
            raise RequestError(f"{name} is ambiguous or nonexistent; provide an explicit UTC offset")
        parsed = first
    return parsed.astimezone(zone)


def _boolean(data: Mapping[str, Any], key: str) -> bool:
    value = data.get(key, False)
    if not isinstance(value, bool):
        raise RequestError(f"{key} must be true or false")
    return value


SCENERY = frozenset({"nature", "city", "river", "forest", "mountain", "skyline", "night", "sunset", "bridge", "park", "water", "greenery"})


@dataclass(frozen=True)
class Location:
    lon: float
    lat: float
    name: str = "Starting point"


@dataclass(frozen=True)
class RecommendationRequest:
    start: Location
    visit_time: datetime
    available_until: datetime
    timezone: str = "Asia/Seoul"
    mode: Literal["scenario", "seoul"] = "scenario"
    stay_minutes: float = 30.0
    transport_mode: Literal["walking", "transit", "driving"] = "walking"
    max_travel_minutes: float = 45.0
    max_walk_m: float = 2500.0
    wheelchair: bool = False
    stroller: bool = False
    no_stairs: bool = False
    max_slope_percent: float | None = None
    preferences: tuple[str, ...] = ("nature", "river")
    crowd_preference: Literal["quiet", "balanced", "lively", "any"] = "quiet"
    k: int = 3
    purpose: str = ""
    group: str = ""
    activity: str = "walking"
    eye_height_m: float = 1.7
    weather: Mapping[str, Any] = field(default_factory=lambda: {"status": "unknown"})

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RecommendationRequest":
        if not isinstance(data, Mapping):
            raise RequestError("The request must be a JSON object")
        start = data.get("start")
        if not isinstance(start, Mapping) or "lon" not in start or "lat" not in start:
            raise RequestError("start.lon and start.lat are required in WGS84 longitude, latitude order")
        timezone = str(data.get("timezone", "Asia/Seoul"))
        if "visit_time" not in data:
            raise RequestError("visit_time is required (the earliest departure / available window start)")
        visit = aware_datetime(data["visit_time"], timezone, "visit_time")
        until = aware_datetime(data.get("available_until", visit + timedelta(hours=3)), timezone, "available_until")
        if until <= visit or until - visit > timedelta(days=7):
            raise RequestError("available_until must follow visit_time by no more than seven days")
        mode = data.get("mode", "scenario")
        transport = data.get("transport_mode", "walking")
        crowd = data.get("crowd_preference", "quiet")
        if mode not in {"scenario", "seoul"}:
            raise RequestError("mode must be scenario or seoul")
        if transport not in {"walking", "transit", "driving"}:
            raise RequestError("transport_mode must be walking, transit, or driving")
        if crowd not in {"quiet", "balanced", "lively", "any"}:
            raise RequestError("crowd_preference must be quiet, balanced, lively, or any")
        preferences = data.get("preferences", ["nature", "river"])
        if not isinstance(preferences, (list, tuple)) or any(not isinstance(p, str) or p not in SCENERY for p in preferences):
            raise RequestError(f"preferences must be a list drawn from {', '.join(sorted(SCENERY))}")
        k = finite_number(data.get("k", 3), "k", 1, 10)
        if not k.is_integer():
            raise RequestError("k must be an integer")
        stay = finite_number(data.get("stay_minutes", 30), "stay_minutes", 1, 1440)
        if visit + timedelta(minutes=stay) > until:
            raise RequestError("The available window is shorter than stay_minutes")
        weather = data.get("weather", {"status": "unknown"})
        if not isinstance(weather, Mapping):
            raise RequestError("weather must be an object")
        return cls(
            start=Location(finite_number(start["lon"], "start.lon", -180, 180), finite_number(start["lat"], "start.lat", -90, 90), str(start.get("name", start.get("label", "Starting point")))[:200]),
            visit_time=visit, available_until=until, timezone=timezone, mode=mode,
            stay_minutes=stay, transport_mode=transport,
            max_travel_minutes=finite_number(data.get("max_travel_minutes", 45), "max_travel_minutes", 1, 1440),
            max_walk_m=finite_number(data.get("max_walk_m", 2500), "max_walk_m", 0, 100000),
            wheelchair=_boolean(data, "wheelchair"), stroller=_boolean(data, "stroller"), no_stairs=_boolean(data, "no_stairs"),
            max_slope_percent=None if data.get("max_slope_percent") is None else finite_number(data["max_slope_percent"], "max_slope_percent", 0, 100),
            preferences=tuple(dict.fromkeys(preferences)), crowd_preference=crowd, k=int(k),
            purpose=str(data.get("purpose", ""))[:1000], group=str(data.get("group", ""))[:200], activity=str(data.get("activity", "walking"))[:200],
            eye_height_m=finite_number(data.get("eye_height_m", 1.7), "eye_height_m", 0.1, 5), weather=dict(weather),
        )

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict
        result = asdict(self)
        result["visit_time"] = self.visit_time.isoformat()
        result["available_until"] = self.available_until.isoformat()
        result["preferences"] = list(self.preferences)
        return result


# Short alias for callers building providers.
Request = RecommendationRequest
