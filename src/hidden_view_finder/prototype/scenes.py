"""Bounded sparse scene inference. All claims derive from one SceneEvidence.

Raster columns are an approximation, not 5m survey accuracy. Unknown samples
remain in each intended target sample set, including unevaluated work.
"""
from __future__ import annotations
import math,time,hashlib,json,html
from collections import Counter
from shapely.geometry import Point
from shapely.ops import nearest_points
from .data import Data,TO_M,TO_LL
from .models import Query,SceneEvidence
from .tiles import Tiles,WorkBudget,inspect_uncertainty
from .runtime import ROOT
from hidden_view_finder.sunlight import solar_position
LABELS={'city':'건물 윤곽','mountain':'봉우리 주변 지형','greenery':'지도상의 녹지 지형','river':'수면','skyline':'여러 건물 윤곽'}

def bearing(dx,dy): return math.degrees(math.atan2(dx,dy))%360

def angle_delta(a,b): return abs((a-b+180)%360-180)

def sample_points(group,observer):
 g=group['geometry'];p=g.representative_point()
 if group['category']=='city':
  near=nearest_points(Point(observer[:2]),g.boundary)[1]
  boundary=g.exterior if g.geom_type=='Polygon' else max(g.geoms,key=lambda x:x.area).exterior
  at=boundary.project(near);points=[boundary.interpolate((at+offset)%boundary.length) for offset in (-8,0,8)]
  return [probe if g.covers(probe) else pt for pt in points for probe in [Point(pt.x*.9+p.x*.1,pt.y*.9+p.y*.1)]]
 if g.geom_type=='Point': return [g,Point(g.x+10,g.y),Point(g.x,g.y+10)]
 if g.geom_type in {'Polygon','MultiPolygon'}:
  minx,miny,maxx,maxy=g.bounds;out=[p]
  for frac in (.3,.7):
   probe=Point(minx+(maxx-minx)*frac,miny+(maxy-miny)*frac)
   out.append(probe if g.covers(probe) else nearest_points(probe,g)[1])
  return out
 return [g.interpolate(f,normalized=True) for f in (.25,.5,.75)]

def score_scene(scene,q):
 visible=scene.coverage['visible'];total=scene.coverage['intended']
 match=len(set(q.preferences)&set(scene.supported_categories))/max(1,len(q.preferences)) if q.preferences else 1
 # Fixed weights: unknown evidence never receives renormalized credit.
 support=visible/max(1,total)
 span=scene.orientation.get('supported_angular_span_deg',0)
 framed=max(0,1-abs(span-15)/30) if visible>=2 else 0
 open_value=min(1,span/55) if visible>=3 else 0
 composition=framed if q.composition=='framed' else open_value if q.composition=='open' else min(1,visible/6)
 distance=max(0,1-scene.proximity_m/q.radius_m)
 # Astronomical daytime is only a bounded contextual suitability adjustment.
 daylight=1 if scene.solar.get('elevation_deg',-90)>0 else .3
 value=45*match+25*support+15*composition+10*distance+5*daylight
 return {'value':round(value,2),'kind':'heuristic_not_probability','components':{'preference_match':round(45*match,2),'sample_support':round(25*support,2),'composition':round(15*composition,2),'straight_line_convenience':round(10*distance,2),'astronomical_time_context':round(5*daylight,2)},'unknown_weather_crowd_adjustment':0}

def diversify(scenes,k=3):
 chosen=[]
 for s in sorted(scenes,key=lambda s:(-s.score['value'],s.view_id)):
  targets={x['target_id'] for x in s.scene_samples if x['state']=='visible'}
  redundant=False
  for old in chosen:
   distance=math.hypot(s.standing['x']-old.standing['x'],s.standing['y']-old.standing['y'])
   previous={x['target_id'] for x in old.scene_samples if x['state']=='visible'}
   overlap=len(targets&previous)/max(1,len(targets|previous))
   if distance<180 and angle_delta(s.orientation['bearing_deg'],old.orientation['bearing_deg'])<40 or overlap>.75 and distance<500:
    redundant=True;break
  if not redundant: chosen.append(s)
  if len(chosen)>=k: break
 return chosen

def schematic(scene):
 samples=scene.scene_samples;center=scene.orientation['bearing_deg'];fov=scene.orientation['fov_deg'];parts=[]
 parts.append('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 230" role="img" aria-label="Geometry schematic, not a photograph"><rect width="640" height="230" fill="#edf1e9"/><path d="M24 160H616" stroke="#aeb8aa" stroke-dasharray="5 5"/>')
 for s in samples:
  rel=(s['bearing_deg']-center+180)%360-180;x=320+rel/fov*580
  a=s.get('angular_elevation_deg');y=max(28,min(192,160-(a or 0)*4))
  color={'visible':'#225c48','blocked':'#9e7061','unknown':'#8d9091','excluded':'#8d9091'}[s['state']]
  parts.append(f'<path d="M{x:.1f} 180V{y:.1f}" stroke="{color}" stroke-width="3"/><circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{color}"/>')
 parts.append('<text x="24" y="215" font-size="12" fill="#506157">Geometry schematic — not a photograph. Unknown context is not empty sky.</text></svg>')
 return {'svg':''.join(parts),'label':'Geometry schematic — not a photograph.','method':'bearing-aligned sample markers; no fabricated continuous horizon'}

class Discovery:
 def __init__(self,c):
  self.config=c;self.data=Data(c['package_manifest']);self.data.validate(hashes=True)
  overlap=ROOT/'reports/citywide/raster-overlap-anomalies.json'
  uncertainty=inspect_uncertainty(self.data.root/'buildings.gpkg',overlap)
  self.uncertainty=uncertainty['report'];self.tiles=Tiles(c['tile_manifest'],uncertainty['regions'],overlap_anomalies_path=overlap)
  self.version=hashlib.sha256(json.dumps({'tiles':self.tiles.metadata['evidence_version'],'osm_quality':self.data.quality_version,'implementation':{name:hashlib.sha256((ROOT/'src/hidden_view_finder/prototype'/name).read_bytes()).hexdigest() for name in ('data.py','scenes.py','models.py')},'limits':c['limits']},sort_keys=True).encode()).hexdigest()
 def evaluate_sample(self,group,p,i,obs,work):
  lon,lat=TO_LL.transform(p.x,p.y);dx=p.x-obs[0];dy=p.y-obs[1];distance=math.hypot(dx,dy)
  record={'evidence_id':f'{group["target_id"]}:{i}','target_id':group['target_id'],'category':group['category'],'name':group['name'][:120],'bearing_deg':bearing(dx,dy),'distance_m':round(distance,1),'state':'unknown','target':{'lon':lon,'lat':lat,'z_m':None},'angular_elevation_deg':None}
  if not work.check(): return {**record,'reason':work.stopped_reason}
  if time.monotonic()>getattr(work,'scene_deadline',float('inf')): return {**record,'reason':'scene_phase_cap_preserves_horizon_budget'}
  if group['category']=='river': return {**record,'reason':'water_surface_elevation_not_independently_supported'}
  if distance>self.config['limits']['maximum_sight_distance_m']: return {**record,'reason':'outside_modeled_sight_distance'}
  if group['category']=='city':
   if not group['geometry'].covers(p):return {**record,'reason':'sample_not_in_source_footprint'}
   cell=self.tiles.sample(p.x,p.y)
   if cell['state']!='supported' or not group['geometry'].covers(Point(cell['cell_center_xy'])):return {**record,'reason':'roof_cell_center_not_identified_with_source_footprint'}
   target=self.tiles.roof_boundary_target(obs[:2],(p.x,p.y))
  else:
   target=self.tiles.surface_boundary_target(obs[:2],(p.x,p.y)) if hasattr(self.tiles,'surface_boundary_target') else self.tiles.target(p.x,p.y)
   if target.get('occupancy'): return {**record,'reason':'mapped_terrain_target_occupied_by_building'}
  if target['state']!='supported': return {**record,'reason':target.get('reason'),'state':'unknown'}
  xyz=target['xyz'];distance=math.hypot(xyz[0]-obs[0],xyz[1]-obs[1]);lon,lat=TO_LL.transform(*xyz[:2]);angle=math.degrees(math.atan2(xyz[2]-obs[2],max(.001,distance)))
  result=self.tiles.ray(obs,xyz,work=work)
  record.update(state=result['state'],reason=result.get('reason'),target={'lon':lon,'lat':lat,'z_m':xyz[2]},bearing_deg=bearing(xyz[0]-obs[0],xyz[1]-obs[1]),distance_m=round(distance,1),angular_elevation_deg=round(angle,3),geometry_method=target.get('target_geometry_method',target.get('height_method')),inspected_cells=result.get('cells_checked'),target_height_reference='absolute model surface from terrain + estimated AGL buildings; horizontal reprojection is not vertical conversion')
  return record
 def recommend(self,q):
  started=time.monotonic();limits=self.config['limits'];retrieval_deadline=started+1.5
  candidates,counts=self.data.candidates(q,limits['candidate_representatives'],retrieval_deadline)
  retrieval=time.monotonic()-started
  if not candidates:
   return {'status':'empty','views':[],'map_results':[],'_all_scenes':[],'search':{'sampled':True,'radius_m':q.radius_m,'distance_method':'ellipsoidal_straight_line','view_at':q.view_at.isoformat(),'candidates':counts,'supported_observers':0,'refined_observers':0,'target_groups':0,'refined_views':0,'work':{'rays':0,'cells':0,'stopped_reason':None},'timings_seconds':{'retrieval':round(retrieval,4),'target_retrieval':0,'geometry':0,'end_to_end':round(time.monotonic()-started,4)}},'limitations':['선택한 직선 반경에서 추천 가능한 지도상 보행 위치를 찾지 못했습니다. 반경은 자동으로 바꾸지 않습니다.','지형·접근 증거가 없는 위치를 결과로 대체하지 않습니다.']}
  x,y=TO_M.transform(q.lon,q.lat);target_pool_radius=q.radius_m+limits['maximum_sight_distance_m']
  groups=self.data.targets(x,y,target_pool_radius,limits['target_groups'])
  # User preferences change ordering, not support tests or unknown denominator.
  groups.sort(key=lambda g:(g['category'] not in q.preferences,g['category']=='river'))
  target_seconds=time.monotonic()-started-retrieval
  work=WorkBudget(limits['sparse_rays'],limits['traversed_cells'],time.monotonic()+limits['geometry_seconds'])
  eligible=[];endpoint_counts=Counter()
  for r in candidates:
   if not work.check(): break
   access=self.data.endpoint(r)
   if access['state']!='map_supported': endpoint_counts[access['reason']]+=1;continue
   obs=self.tiles.observer(r['x'],r['y'])
   if obs['state']!='supported': endpoint_counts[obs.get('reason','unsupported')]+=1;continue
   eligible.append((r,obs,[]))
  supported_observer_count=len(eligible)
  # Refine at most20 spatial representatives. Within each origin-bearing stratum,
  # high supported ground is a geometric prospect heuristic, not scenic evidence.
  strata={}
  for item in eligible:
   r,obs,_=item;sector=int(bearing(r['x']-x,r['y']-y)//45)
   strata.setdefault(sector,[]).append(item)
  for bucket in strata.values():bucket.sort(key=lambda item:(-item[1]['dtm_m'],item[0]['proximity_m']))
  refined=[]
  for i in range(20):
   for sector in sorted(strata):
    if i<len(strata[sector]) and len(refined)<limits['refined_views']:refined.append(strata[sector][i])
  eligible=refined
  # Round-robin candidates AND category/target strata; preserve full intended sets.
  work.scene_deadline=work.deadline-.7
  preferred=[g for g in groups if g['category'] in q.preferences or g['category']=='city' and 'skyline' in q.preferences]
  other=[g for g in groups if g not in preferred]
  for gi in range(len(groups)):
   for ci,(r,obs,samples) in enumerate(eligible):
    pool=preferred if gi<len(preferred) else other
    j=gi if gi<len(preferred) else gi-len(preferred)
    ordered=sorted(pool,key=lambda group:group['geometry'].distance(Point(obs['xyz'][:2])))
    g=ordered[j]
    for i,p in enumerate(sample_points(g,obs['xyz'])):
     samples.append(self.evaluate_sample(g,p,i,obs['xyz'],work))
  geometry_seconds=time.monotonic()-started-retrieval-target_seconds
  scenes=[];solar_base=solar_position(q.lon,q.lat,q.view_at);solar_base['elevation_deg']=solar_base['altitude_deg']
  for r,obs,samples in eligible:
   visible=[s for s in samples if s['state']=='visible']
   # Only requested categories with supported components can produce a card.
   anchors=[s for s in visible if not q.preferences or s['category'] in q.preferences or s['category']=='city' and 'skyline' in q.preferences]
   bearings=[]
   for a in anchors:
    if all(angle_delta(a['bearing_deg'],b)>50 for b in bearings): bearings.append(a['bearing_deg'])
   for center in bearings[:2]:
    subset=[s for s in samples if angle_delta(s['bearing_deg'],center)<=35]
    support=Counter(s['state'] for s in subset);categories=sorted({s['category'] for s in subset if s['state']=='visible'})
    building_ids={s['target_id'] for s in subset if s['state']=='visible' and s['category']=='city'}
    if len(building_ids)>=2: categories.append('skyline')
    if q.preferences and not set(categories)&set(q.preferences): continue
    # A single surviving sample cannot substantiate an attractive broad view.
    if support['visible']<2 or len({(round(s['target']['lon'],8),round(s['target']['lat'],8),s['target']['z_m']) for s in subset if s['state']=='visible'})<2: continue
    rel=[(s['bearing_deg']-center+180)%360-180 for s in subset if s['state']=='visible'];span=max(rel)-min(rel) if rel else 0
    coverage={k:support[k] for k in ('visible','blocked','unknown','excluded')};coverage.update(intended=len(subset),panorama_supported=False,sector_support='partial_sample_set',terrain_area_fraction_is_not_visibility_probability=True)
    key={'candidate':r['source_id'],'xy':[r['x'],r['y']],'bearing':round(center,2),'fov':70,'geometry':self.version,'view_at':q.view_at.isoformat(),'samples':[(s['evidence_id'],s['state']) for s in subset]}
    view_id=hashlib.sha256(json.dumps(key,sort_keys=True).encode()).hexdigest()[:24]
    solar=solar_position(r['lon'],r['lat'],q.view_at);solar['elevation_deg']=solar.pop('altitude_deg');solar['directional_horizon']={'state':'unknown','reason':'not_evaluated_within_request_budget','maximum_modeled_range_m':10000}
    scene=SceneEvidence(view_id,r['source_id'],r.get('name') or '지도상의 보행 위치',{'lon':r['lon'],'lat':r['lat'],'x':r['x'],'y':r['y'],'effective_x':r['x'],'effective_y':r['y'],'effective_lon':r['lon'],'effective_lat':r['lat'],'snap_displacement_m':0,'cell_center_xy':obs['cell_center_xy'],'cell_center_displacement_m':obs['cell_center_displacement_m'],'observer_z_m':obs['xyz'][2],'eye_height_m':1.7,'district':self.data.district(r['geometry']),'source_ids':r['lineage'],'coordinate_method':obs['observer_coordinate_method']},{'bearing_deg':round(center,2),'fov_deg':70,'supported_angular_span_deg':round(span,2)},q.view_at.isoformat(),round(r['proximity_m'],1),subset,categories,coverage,r['access'],{'package_id':self.data.version,'geometry':self.version,'scene_method':'sparse-column-scenes-v1'},solar,work.as_dict(),['실험용 지도·모델 추정이며 현장 검증이 아닙니다.','직선거리이며 경로·이동시간·현재 개방 여부는 확인하지 않았습니다.','주변 지형 및 미평가 방향은 알 수 없습니다. 전체 파노라마를 보증하지 않습니다.','건물 높이는 추정값이며 나무 수관·계절 잎·대기 시정·혼잡은 미확인입니다.','불완전한 OSM 관계 36건과 건물 오류 지역은 별도 품질 제한으로 유지됩니다.'])
    scene.composition={'method':'angular spread of supported discrete samples','supported_angular_span_deg':round(span,2),'foreground_blocked_samples':support['blocked'],'preference':q.composition,'open_panorama_verified':False}
    scene.score=score_scene(scene,q)
    labels=' · '.join(LABELS[c] for c in categories)
    scene.description=f"{scene.orientation['bearing_deg']:.0f}° 방향에서 {labels}의 일부 모델 표본이 지지됩니다. 직선 {scene.proximity_m/1000:.2f} km, 표본 {support['visible']}/{len(subset)}개가 모델상 보입니다. 열린 전망·실제 조명·조용함은 확인되지 않았습니다."
    scene.preview=schematic(scene);scenes.append(scene)
  scenes=sorted(scenes,key=lambda s:-s.score['value'])[:limits['refined_views']]
  chosen=diversify(scenes,q.limit)
  # Horizon is supplementary; never spend beyond the same request deadline.
  if hasattr(self.tiles,'horizon'):
   for scene in chosen:
    if work.check():
     obs=[scene.standing['x'],scene.standing['y'],scene.standing['observer_z_m']]
     try:
      horizon=self.tiles.horizon(obs,scene.solar['azimuth_deg'],10000,work=work)
      scene.solar['directional_horizon']=horizon
      scene.solar['local_solar_direction_relation']=('above_supported_modeled_horizon' if scene.solar['elevation_deg']>horizon['maximum_angle_deg'] else 'below_supported_modeled_horizon') if horizon.get('state')=='supported' else 'unknown'
     except (ValueError,TypeError): pass
  total=time.monotonic()-started
  return {'status':'partial' if chosen else 'empty','views':[s.to_dict() for s in chosen],'map_results':[{'view_id':s.view_id,'name':s.name,'standing':s.standing,'orientation':s.orientation,'score':s.score['value']} for s in scenes], 'search':{'sampled':True,'radius_m':q.radius_m,'distance_method':'ellipsoidal_straight_line','view_at':q.view_at.isoformat(),'candidates':counts,'endpoint_exclusions':dict(endpoint_counts),'supported_observers':supported_observer_count,'refined_observers':len(eligible),'target_groups':len(groups),'target_inventory_radius_m':target_pool_radius,'maximum_ray_distance_m':limits['maximum_sight_distance_m'],'target_inventory_policy':'near quadrants plus outer band; sampled, not complete scene coverage','refined_views':len(scenes),'work':work.as_dict(),'timings_seconds':{'retrieval':round(retrieval,4),'target_retrieval':round(target_seconds,4),'geometry':round(geometry_seconds,4),'end_to_end':round(total,4)}},'limitations':['전역 지리·가시성 준비 상태는 false입니다. 지원되는 개별 광선만 평가합니다.','상위 3개를 채우기 위해 반경이나 증거 기준을 바꾸지 않습니다.']+(['지원되는 장면 표본이 부족합니다. 다른 출발점·선호를 직접 선택해 보세요.'] if not chosen else []),'_all_scenes':[s.to_dict() for s in scenes]}
 def close(self): self.tiles.close();self.data.close()
