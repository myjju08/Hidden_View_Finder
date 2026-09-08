"""Descriptions and image prompts are produced only after evidence-based ranking."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .sunlight import solar_position

IMAGE_LABEL = 'AI-generated anticipated view — actual scenery may differ.'
FEATURE_KO = {'nature':'자연', 'city':'도시', 'river':'강', 'forest':'숲', 'mountain':'산',
              'skyline':'스카이라인', 'night':'야경', 'sunset':'일몰', 'bridge':'다리',
              'park':'공원', 'water':'물', 'greenery':'녹지'}


def decorate(candidate: dict, request: dict, landmarks: list[dict], *, selected: bool) -> dict:
    names = {item['id']: item['name'] for item in landmarks}
    candidate['target_names'] = [names.get(t, t) for t in candidate.get('target_ids', [])]
    facing = float(candidate.get('bearing_deg', 0))
    direction = ['북', '북동', '동', '남동', '남', '남서', '서', '북서'][int((facing+22.5)//45) % 8]
    candidate['viewing_direction'] = f'{direction} · {facing:.0f}°'
    hypothetical = request['mode'] == 'scenario'
    travel = candidate.get('route', {}).get('travel_minutes')
    target_text = ', '.join(candidate['target_names'])
    visible = candidate.get('visibility') == 'visible'
    clause = ('가상 시나리오에서 ' if hypothetical else '준비된 5 m 표면의 근사 계산에서 ')
    clause += (f'{target_text}의 대표 점이 보이는 상태입니다.' if visible else '목표 점의 가시성을 확정하지 못했습니다.')
    matched = [FEATURE_KO.get(f, f) for f in request['preferences'] if f in candidate.get('scenic_features', [])]
    why = f"요청한 {', '.join(matched)} 요소와 일치합니다." if matched else '취향과 일치하는 장면 요소는 현재 근거로 확인되지 않았습니다.'
    route_text = f'이동 {travel:.1f}분' if isinstance(travel, (int, float)) else '이동 시간 미확인'
    if candidate.get('route', {}).get('status') == 'estimated':
        route_text += ' 추정'
    candidate['description'] = f"표시한 좌표에서 {direction}쪽({facing:.0f}°)을 바라보는 후보입니다. {clause} {why} {route_text}."
    if hypothetical:
        candidate['description'] = '실제 방문 장소가 아닌 기능 검증용 가상 장소입니다. ' + candidate['description']
    else:
        snap = candidate.get('visibility_evidence', {}).get('snap_distance_m')
        candidate['description'] = (
            f'표시 좌표는 실제 지도상의 보행 노드입니다. 가시성은 약 {snap} m 떨어진 '
            f'해당 5 m 셀 중심에서 계산했습니다. 지도 노드 자체의 시선은 확인하지 않았습니다. '
            f'{direction}쪽({facing:.0f}°) 목표에 대해 {clause} {why} {route_text}.')
    candidate['recommendation_rationale'] = why
    candidate.setdefault('uncertainties', [])
    if not hypothetical:
        candidate['map_url'] = f"https://www.openstreetmap.org/?mlat={candidate['lat']}&mlon={candidate['lon']}#map=18/{candidate['lat']}/{candidate['lon']}"
    at = datetime.fromisoformat(candidate.get('arrival_at') or request['visit_time'])
    candidate['sunlight'] = solar_position(candidate['lon'], candidate['lat'], at)
    if hypothetical:
        candidate['sunlight']['scope'] = 'Astronomy at fictional coordinates, not an actual location recommendation.'
    candidate['recommended_arrival_at'] = candidate.get('arrival_at')
    composition = candidate.get('composition', {}) or {}
    prompt = (
        f"Create an illustrative mood reference, NOT an accurate location reconstruction. "
        f"Observer lon={candidate['lon']}, lat={candidate['lat']}, eye height={request['eye_height_m']}m, "
        f"bearing={facing} degrees, assumed field of view=60 degrees. "
        f"Visit/estimated arrival={at.isoformat()}, timezone={request['timezone']}. "
        f"Target(s)={target_text}; target visibility state={candidate.get('visibility','unknown')}, "
        f"scope={'fictional scenario' if hypothetical else 'one approximate point only'}. "
        f"Relative landmark arrangement is unknown. Scene tags={candidate.get('scenic_features',[])}; "
        f"foreground={composition.get('foreground','unknown')}; middle={composition.get('middle_ground','unknown')}; "
        f"background={composition.get('background','unknown')}. Weather evidence={candidate.get('weather', {'status':'unknown'})}. "
        "Geometry is insufficient for structural conditioning; use soft non-photoreal illustration. "
        "Do not invent unverified objects, reflections, lighting, clear sightlines or actual crowd conditions. "
        f"Always label: {IMAGE_LABEL}"
    )
    image = {'status': 'not_generated', 'kind': 'illustrative_mood_reference', 'url': None,
             'label': IMAGE_LABEL, 'prompt': prompt,
             'reason': 'No runtime image-generation connector. Prompt emitted after ranking; no image used as evidence.'}
    # Only the exact published fixture context can reuse generated illustrations.
    from .scenarios import default_request
    from .models import Request
    preset = Request.from_dict(default_request()).to_dict()
    match = request == preset
    asset = Path(__file__).parent/'static/images'/f"{candidate['id']}.png"
    if selected and hypothetical and match and asset.is_file():
        image.update(status='generated', url=f"/static/images/{candidate['id']}.png",
            reason='Pre-generated after the default scenario ranking; illustration only.',
            generated_for='Fixed 2026-09-08 scenario; not live weather or a measured location',
            structural_conditioning=False,
            original_generation_bearing_deg={'river_steps':82, 'forest_window':315, 'garden_frame':200}.get(candidate['id']),
            prompt_scope='Current request prompt; historical generation prompts retained in docs/image-prompts.md')
    candidate['image'] = image
    return candidate
