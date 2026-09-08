#!/usr/bin/env python3
"""Run the real Lotte Tower example and select provisional lake-path illustrations.

This never promotes unverified access to confirmed recommendations. It records
native and independent column LOS, plus map relations; lake visibility itself
remains unknown. Image generation and rendering occur after this script.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'src'))
import numpy as np
from pyproj import Transformer
from shapely.geometry import shape, Point, LineString
from shapely.ops import transform
from hidden_view_finder.providers import distance_m
from hidden_view_finder.service import DemoService
from hidden_view_finder.scenarios import default_request
from seoul_visibility import State, TargetPoint


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT/'data/demo/lotte-run')
    parser.add_argument('--visit-time', default=(datetime.now(ZoneInfo('Asia/Seoul')) + timedelta(days=1)).replace(hour=16,minute=0,second=0,microsecond=0).isoformat())
    parser.add_argument('--offline-weather', action='store_true')
    args = parser.parse_args()
    context_path = ROOT/'data/demo/jamsil/context.json'
    context_hash = hashlib.sha256(context_path.read_bytes()).hexdigest()
    with_context = json.loads(context_path.read_text())
    service = DemoService(ROOT/'data', manifest=ROOT/'data/seoul/processed/jamsil-gba-maximum/manifest.json',
                          context=context_path, online_weather=not args.offline_weather)
    try:
        bootstrap = service.bootstrap()
        visit = datetime.fromisoformat(args.visit_time)
        request = dict(default_request(), mode='seoul', start=bootstrap['seoul_start'],
            visit_time=visit.isoformat(), available_until=(visit+timedelta(hours=2,minutes=30)).isoformat(),
            preferences=['city'], crowd_preference='any')
        result = service.recommend(request)
        xy = Transformer.from_crs(4326,5186,always_xy=True)
        lake_record = next(a for a in with_context['areas'] if a['id']=='relation/9856886')
        lake = transform(xy.transform, shape(lake_record['geometry']))
        landmark = with_context['landmarks'][0]
        target = TargetPoint(landmark['lon'], landmark['lat'], landmark['height_m'], 'agl')
        target_xy = Point(xy.transform(target.lon,target.lat))
        filtered = []
        for candidate in result['recommendations']+result['unverified']:
            point = Point(xy.transform(candidate['lon'],candidate['lat']))
            if candidate['visibility'] != 'visible' or point.distance(lake)>45 or lake.contains(point):
                continue
            target_distance = point.distance(target_xy)
            water_intersection = LineString([point,target_xy]).intersection(lake).length
            # Explicit case-study framing restriction, not an inferred score:
            # a more distant tower point and water in the map's plan direction.
            if target_distance<400 or water_intersection<100:
                continue
            candidate.update(target_distance_m=round(target_distance,2), lake_distance_m=round(point.distance(lake),2),
                map_water_crossing_m=round(water_intersection,2), water_visibility='unknown')
            filtered.append(candidate)
        if not filtered:
            raise RuntimeError('No visible, suitably separated mapped lake-path cases; no substitutes invented')
        assert service.seoul.engine is not None
        reference = service.seoul.engine.check_observers(target, np.array([[c['lon'],c['lat']] for c in filtered]))
        selected = []
        for candidate, state in zip(filtered,reference.states):
            candidate['reference_visibility'] = State(int(state)).name.lower()
            if state != State.VISIBLE:
                continue
            if any(distance_m((candidate['lon'],candidate['lat']),(other['lon'],other['lat']))<200 for other in selected):
                continue
            direction = candidate['bearing_deg']
            sector = '서호 남측' if 25<=direction<90 else '동호 동측' if 250<=direction<330 else '동호 남측'
            candidate['case_label'] = f'석촌호수 {sector} 산책로'
            candidate['case_label_basis'] = 'Descriptive map position, not an official named observation deck'
            candidate['selection_status'] = 'provisional_example_not_confirmed_recommendation'
            selected.append(candidate)
            if len(selected)==3:
                break
        if not selected:
            raise RuntimeError('Reference LOS did not support any selected map coordinates')
        assert hashlib.sha256(context_path.read_bytes()).hexdigest()==context_hash, 'Context changed during the example'
        report = {'request':request, 'context_sha256':context_hash, 'service_result':result,
            'selected_examples':selected, 'lake_filtered_count':len(filtered),
            'reference_states':[State(int(s)).name.lower() for s in reference.states],
            'reference_metadata':reference.metadata,
            'selection_policy':'Ranked provisional visible candidates on mapped paths, outside water but <=45m from lake; >=400m from tower; plan sightline crosses >=100m water; independent column LOS visible; >=200m separation. These are illustration cases, not confirmed TopK. Map water relation is not a water-visibility calculation.',
            'limitations':['GBA tower obstruction is about137m versus published555m structuralheight; physical visibility and the full tower silhouette remain unverified.',
                'Trees/railings/construction and exact lake visibility/composition are not modeled.',
                'Travel times are map estimates; full route access/current opening/crowds are unverified.']}
        args.output.mkdir(parents=True,exist_ok=True)
        destination=args.output/'lotte-example.json'
        destination.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps({'report':str(destination),'confirmed':len(result['recommendations']),
            'unverified':len(result['unverified']),'selected':[{'id':c['id'],'name':c['case_label'],'lon':c['lon'],'lat':c['lat'],
            'score':c['score'],'travel_minutes':c['route']['travel_minutes'],'bearing':c['bearing_deg'],
            'distance_m':c['target_distance_m']} for c in selected]},ensure_ascii=False))
    finally:
        service.close()


if __name__=='__main__':
    main()
