"""Synthetic fixtures: ranking/scene/request invariants, not field-view accuracy."""
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import json,math
import pytest
from shapely.geometry import Point,Polygon
from hidden_view_finder.prototype.models import Query,SceneEvidence
from hidden_view_finder.prototype.scenes import score_scene,diversify,schematic,Discovery,sample_points
from hidden_view_finder.prototype.runtime import config
from seoul_visibility.errors import ResourceBudgetError

def query(**kw):
 data={'origin':{'lon':127,'lat':37.55},'view_at':'2026-09-11T18:00:00','preferences':['mountain'],'radius_m':3000};data.update(kw);return Query.parse(data)
def scene(identity='a',category='mountain',x=0,bearing=10,span=15,visible=3,total=6,target='mountain1'):
 samples=[{'evidence_id':f'{identity}:{i}','target_id':target,'category':category,'name':'Synthetic fixture','bearing_deg':bearing+i,'distance_m':100,'state':'visible' if i<visible else 'unknown','target':{'lon':127+i/1e5,'lat':37.55,'z_m':100},'angular_elevation_deg':3} for i in range(total)]
 return SceneEvidence(identity,identity,'Synthetic fixture',{'x':x,'y':0,'lon':127,'lat':37.55},{'bearing_deg':bearing,'fov_deg':70,'supported_angular_span_deg':span},'2026-09-11T18:00:00+09:00',500,samples,[category],{'visible':visible,'blocked':0,'unknown':total-visible,'intended':total}, {'state':'map_supported'},{'geometry':'synthetic'}, {'elevation_deg':10},{},['synthetic fixture'])

def test_view_at_is_seeing_time_in_seoul_without_routes():
 q=query();assert q.view_at.utcoffset().total_seconds()==9*3600
 assert query(view_at='2026-09-11T09:00:00Z').view_at==q.view_at
 assert not hasattr(q,'departure_time')
 s=scene().to_dict();assert s['route_distance_m'] is None and s['estimated_travel_minutes'] is None
@pytest.mark.parametrize('fields',[{'estimated_travel_minutes':15},{'route_distance_m':3000},{'radius_m':10001},{'radius_m':True},{'origin':{'lon':math.nan,'lat':37}},{'view_at':'tomorrow'},{'preferences':['invented']},{'composition':'always_open'}])
def test_invalid_or_old_route_inputs_rejected(fields):
 with pytest.raises(ValueError):query(**fields)
def test_unknown_samples_keep_fixed_denominator_and_do_not_gain_score():
 q=query();a=scene(total=3);b=scene(total=6)
 assert score_scene(a,q)['value']>score_scene(b,q)['value']
 assert score_scene(b,q)['components']['sample_support']==12.5
 assert score_scene(b,q)['unknown_weather_crowd_adjustment']==0
 assert b.to_dict()['weather']['status']=='unknown'
def test_preferences_change_actual_ranking_and_no_unknown_weight_renormalization():
 mountain=scene(category='mountain');city=scene(category='city')
 assert score_scene(mountain,query())['value']>score_scene(city,query())['value']
 assert score_scene(mountain,query(preferences=['city']))['value']<score_scene(city,query(preferences=['city']))['value']
def test_framed_and_open_preferences_have_different_optima():
 narrow=scene(span=15);wide=scene(span=60)
 assert score_scene(narrow,query(composition='framed'))['value']>score_scene(wide,query(composition='framed'))['value']
 assert score_scene(narrow,query(composition='open'))['value']<score_scene(wide,query(composition='open'))['value']
def test_diverse_locations_and_directions_no_top_three_padding():
 a=scene('a');b=scene('b',x=20);c=scene('c',x=20,bearing=170,target='different');d=scene('d',x=2000)
 for i,s in enumerate([a,b,c,d]):s.score={'value':100-i}
 assert [s.view_id for s in diversify([a,b,c],3)]==['a','c']
 assert len(diversify([],3))==0
 assert len(diversify([a],3))==1
 assert len(diversify([a,b,c,d],3))==3

def test_schematic_does_not_invent_continuous_horizon_or_html_labels():
 s=scene();s.scene_samples[0]['name']='<script>alert(1)</script>'
 preview=schematic(s);assert 'not a photograph' in preview['label'];assert '<script>' not in preview['svg']
 assert 'Unknown context is not empty sky' in preview['svg']

def test_river_elevation_remains_unknown_and_spends_no_ray():
 d=Discovery.__new__(Discovery);d.config={'limits':{'maximum_sight_distance_m':10000}}
 class Work:
  stopped_reason=None
  def check(self):return True
 r=d.evaluate_sample({'target_id':'water/1','category':'river','name':'Synthetic water'},Point(200050,550000),0,[200000,550000,30],Work())
 assert r['state']=='unknown';assert r['target']['z_m'] is None
 assert r['reason']=='water_surface_elevation_not_independently_supported'

def test_concave_roof_points_remain_in_intended_footprint():
 g=Polygon([(0,0),(30,0),(30,5),(5,5),(5,30),(0,30)])
 points=sample_points({'geometry':g,'category':'city'},[40,40,1])
 assert len(points)==3 and all(g.covers(p) for p in points)

def test_no_task_config_can_raise_legacy20gib(tmp_path):
 original=json.loads(Path('configs/prototype.json').read_text());original['storage']['total_bytes']=20*1024**3
 p=tmp_path/'bad.json';p.write_text(json.dumps(original))
 with pytest.raises(ResourceBudgetError):config(p)
@pytest.mark.parametrize('name,value',[('task_additions_bytes',4000000001),('image_cache_bytes',250000001),('artifacts_bytes',100000001)])
def test_prototype_subbudget_ceiling(tmp_path,name,value):
 c=json.loads(Path('configs/prototype.json').read_text());c['storage'][name]=value;p=tmp_path/'bad.json';p.write_text(json.dumps(c))
 with pytest.raises(ResourceBudgetError):config(p)
