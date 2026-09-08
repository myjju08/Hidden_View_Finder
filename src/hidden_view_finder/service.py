"""User context -> landmarks -> spot evidence -> Top K -> descriptions/images."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import time

from .models import Request
from .providers import ForecastProvider
from .ranking import rank_candidates
from .scenarios import default_request, scenario_bundle
from .presentation import decorate
from .seoul import SeoulAdapter


class DemoService:
    def __init__(self, data_root: Path = Path('data'), *, manifest: Path | None = None, online_weather: bool = False):
        self.data_root = data_root.resolve()
        self.weather = ForecastProvider(enabled=online_weather)
        self.seoul = SeoulAdapter(manifest or self.data_root/'seoul/processed/central-gba-maximum/manifest.json',
                                  self.data_root/'demo/context.json', self.weather)

    def close(self) -> None:
        self.seoul.close()

    def bootstrap(self) -> dict:
        return {'version': '0.2.0', 'defaults': default_request(),
            'modes': [{'id':'scenario','name':'가상 시나리오','available':True,
                       'description':'데이터 없이 실행하는 고정 가상 사례. 실제 장소·날씨·가시성 아님.'},
                      {'id':'seoul','name':'서울 실제 자료','available':self.seoul.available,
                       'description':'남산 1.8 km · 실제 표면 계산과 OSM 보행 경로. 미검증 필수 조건은 별도 표시.'}],
            'seoul_start': {'lon':126.9854902,'lat':37.5607321,'name':'명동역 3번 출구 · OSM 지도 좌표'},
            'weather_enabled': self.weather.enabled,
            'sources': [{'name':'데이터와 검증 방법','url':'/api/about'}]}

    def recommend(self, payload: dict) -> dict:
        started = time.perf_counter()
        req = Request.from_dict(payload)
        canonical = req.to_dict()
        if req.mode == 'seoul' and not self.seoul.available:
            raise FileNotFoundError('서울 모드에는 준비 manifest, data/demo/context.json, GDAL Python 환경이 필요합니다. docs/demo.md의 취득·준비 절차를 실행하고 .venv/bin/python을 사용하세요.')
        bundle = scenario_bundle(req) if req.mode == 'scenario' else self.seoul.bundle(canonical)
        ranked = rank_candidates(req, bundle['candidates'])
        for group in ('recommendations','unverified','excluded'):
            ranked[group] = [decorate(c, canonical, bundle['landmarks'], selected=group=='recommendations') for c in ranked[group]]
        unknowns = ['현장 인원·기상과 실제 접근성은 가상 시나리오로 확인할 수 없습니다.'] if req.mode == 'scenario' else [
            '현장 인원 관측·예측 미연결', '완전한 보행 접근성과 현재 개방 상태 미검증',
            '나무·벽·공사 및 전체 풍경 구도 미모델링']
        hard = {'max_travel_minutes':req.max_travel_minutes,'max_walk_m':req.max_walk_m,
                'window':[req.visit_time.isoformat(),req.available_until.isoformat()], 'stay_minutes':req.stay_minutes,
                'wheelchair':req.wheelchair,'stroller':req.stroller,'no_stairs':req.no_stairs,'max_slope_percent':req.max_slope_percent}
        return {**ranked, 'mode':req.mode, 'generated_at':datetime.now(timezone.utc).isoformat(),
            'request_summary':{'request':canonical,'hard_constraints':hard,
                'soft_preferences':{'scenery':list(req.preferences),'crowd':req.crowd_preference},
                'assumptions':bundle.get('assumptions',[]),'unknowns':unknowns,
                'time_semantics':'출발 / 방문 가능 시작 + 경로 이동 시간 = 예상 도착. 체류 전체를 종료 시각과 비교.'},
            'landmarks':bundle['landmarks'],'sources':bundle.get('sources',[]),
            'map':bundle.get('map',{'kind':'scenario','paths':[], 'attribution':'가상 좌표 관계도 · 실제 지도 아님'}),
            'weather':bundle.get('weather',{'status':'unknown'}),
            'visibility_summary':bundle.get('visibility_summary'),
            'limitations':unknowns+['점 가시성은 건물 전체·경관 매력·출입 허가를 보장하지 않습니다.',
                '점수는 가중 휴리스틱입니다. 근거 비중이 다른 점수를 같은 확실성으로 비교하지 않습니다.',
                '추천 후 이미지를 생성하며 이미지는 검증·재순위의 근거가 아닙니다.'],
            'pipeline':[{'id':'context','label':'사용자 조건','status':'complete'},
                        {'id':'landmarks','label':'랜드마크 후보','status':'complete','count':len(bundle['landmarks'])},
                        {'id':'spots','label':'관측 위치·근거','status':'complete','count':len(bundle['candidates'])},
                        {'id':'ranking','label':'필수 조건·Top K','status':'complete','count':len(ranked['recommendations'])},
                        {'id':'presentation','label':'설명·예상 이미지','status':'complete'}],
            'timing_s':round(time.perf_counter()-started,6)}
