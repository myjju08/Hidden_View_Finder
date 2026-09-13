"""Post-ranking point-direction previews, with no invented scene composition.

These values describe one modeled target endpoint. They are neither a camera
render nor evidence for whole-building, water, or skyline visibility.
"""
from __future__ import annotations

import math

from .providers import bearing_deg, distance_m


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _lonlat(value: dict) -> tuple[float, float] | None:
    lon, lat = _number(value.get('lon')), _number(value.get('lat'))
    if lon is None or lat is None or not -180 <= lon <= 180 or not -90 <= lat <= 90:
        return None
    return lon, lat


def target_direction_preview(candidate: dict, landmarks: list[dict], eye_height_m: float) -> dict:
    """Return honest endpoint metadata; omit vertical angle without DTM evidence.

    Native candidates use the effective pixel centers and projected metre
    distances. The vertical angle applies the same d²/D curvature correction
    from the observer endpoint. It is not the angular size of an entire tower.
    """
    target = next((item for item in landmarks if item.get('id') in candidate.get('target_ids', [])), None)
    unknown = {'status': 'unknown', 'kind': 'target_direction',
               'note': '목표점 좌표 근거가 부족하여 방향 미리보기를 계산하지 않았습니다.'}
    if target is None or target.get('supported') is False:
        return unknown
    evidence = candidate.get('visibility_evidence') or {}
    target_info = evidence.get('target') or {}
    effective_observer = evidence.get('effective_observer') or {}
    effective_target = target_info.get('effective') or {}
    use_effective = _lonlat(effective_observer) is not None and _lonlat(effective_target) is not None
    observer_position = effective_observer if use_effective else candidate
    target_position = effective_target if use_effective else target
    observer_lonlat, target_lonlat = _lonlat(observer_position), _lonlat(target_position)
    if observer_lonlat is None or target_lonlat is None:
        return unknown
    projected = [_number(item.get(axis)) for item in (observer_position, target_position) for axis in ('x', 'y')]
    if use_effective and all(value is not None for value in projected):
        ox, oy, tx, ty = projected
        distance = math.hypot(tx-ox, ty-oy)
        facing = math.degrees(math.atan2(tx-ox, ty-oy)) % 360
        distance_method, bearing_reference = 'projected_metres', 'prepared_crs_grid_north'
    else:
        distance = distance_m(observer_lonlat, target_lonlat)
        facing = bearing_deg(observer_lonlat, target_lonlat)
        distance_method, bearing_reference = 'spherical_map_distance', 'true_north_spherical_approximation'
    if distance <= 1e-9:
        return dict(unknown, target_name=target['name'], distance_m=0,
                    note='관측점과 목표점의 수평 위치가 같아 바라볼 방향을 정할 수 없습니다.')
    preview = {'status': 'computed', 'kind': 'target_direction', 'target_name': target['name'],
        'target_id': target['id'], 'distance_m': round(distance, 1), 'bearing_deg': round(facing, 1),
        'bearing_reference': bearing_reference, 'distance_method': distance_method,
        'coordinate_basis': 'effective_pixel_centers' if use_effective else 'provided_map_coordinates',
        'observer': {'lon': observer_lonlat[0], 'lat': observer_lonlat[1]},
        'target': {'lon': target_lonlat[0], 'lat': target_lonlat[1]},
        'visibility_state': candidate.get('visibility', 'unknown'),
        'note': ('5 m 계산 셀 중심 사이의 단일 목표점 방향·거리입니다. ' if use_effective else
                 '지도 좌표 사이의 단일 목표점 방향·거리입니다. ')
                + '방향 표시는 보인다는 증거가 아니며, 건물 전체·호수·나무·구도를 재현하지 않습니다.'}
    ground = _number(evidence.get('observer_ground_elevation_m'))
    absolute_target = _number(target_info.get('absolute_elevation_m'))
    coefficient = _number(evidence.get('curvature_coefficient'))
    diameter = _number(evidence.get('earth_diameter_m'))
    eye_height = _number(eye_height_m)
    vertical_reference = target_info.get('vertical_reference')
    valid_ground = (use_effective and distance_method == 'projected_metres'
        and evidence.get('observer_ground_elevation_status') == 'computed'
        and candidate.get('visibility') in ('visible', 'blocked')
        and ground is not None and absolute_target is not None
        and eye_height is not None and eye_height >= 0
        and coefficient is not None and 0 <= coefficient <= 1
        and diameter is not None and diameter > 0
        and bool(vertical_reference)
        and evidence.get('observer_ground_vertical_reference') == vertical_reference)
    if valid_ground:
        observer_z = ground + eye_height
        corrected_difference = absolute_target - observer_z - coefficient * distance**2 / diameter
        preview.update(target_elevation_angle_deg=round(math.degrees(math.atan2(corrected_difference, distance)), 1),
            observer_eye_elevation_m=round(observer_z, 2), target_elevation_m=round(absolute_target, 2),
            vertical_reference=vertical_reference, curvature_coefficient=coefficient,
            angle_scope='One modeled target point above the observer horizontal; not full-object angular size.',
            note=preview['note']+' 고도각은 지형·눈높이·근사 목표고도 및 설정된 곡률로 계산했습니다.')
    else:
        preview['elevation_angle_status'] = 'unknown'
    return preview
