"""Synthetic bounded retrieval and access-evidence regression fixtures."""
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest
from shapely.geometry import Point,box

from hidden_view_finder.prototype.data import Data,access_policy,TO_M


def reader():
    d=Data.__new__(Data)
    d.excluded_source_ids=set();d.quarantine_regions=[]
    return d


def record(fid=1,*,x=None,y=None,tags=None,source_id=None):
    cx,cy=TO_M.transform(127,37.5)
    x=cx+100 if x is None else x;y=cy+100 if y is None else y
    tags=tags or {'highway':'footway'}
    return {'fid':fid,'geometry':Point(x,y),'tags':tags,'evidence':{'source_id':source_id or f'way/{fid}'},
        'source_id':f'candidate/{fid}','access':access_policy(tags,{})}


def test_known_private_duplicate_cannot_be_dropped_before_position_dedup():
    d=reader();allowed=record();restricted=record(2,tags={'highway':'footway','access':'private'})
    d.rows=lambda table,bounds=None,limit=100,**kwargs:[dict(allowed),dict(restricted)]
    result,counts=d.candidates(SimpleNamespace(lon=127,lat=37.5,radius_m=3000),cap=10)
    assert result==[]
    assert counts['coincident_restriction_veto']>0


def test_public_duplicate_retains_stable_lineage_and_one_position():
    d=reader();a=record();b=record(2)
    d.rows=lambda table,bounds=None,limit=100,**kwargs:[dict(a),dict(b)]
    result,counts=d.candidates(SimpleNamespace(lon=127,lat=37.5,radius_m=3000),cap=10)
    assert len(result)==1
    assert set(result[0]['lineage'])=={'way/1','way/2'}
    assert counts['duplicate_positions']>0


def test_sqlite_deadline_keeps_prior_buckets_and_does_not_become_server_error():
    d=reader();calls=[]
    def rows(*args,**kwargs):
        calls.append(1)
        if len(calls)==2:raise sqlite3.OperationalError('interrupted')
        return [record()]
    d.rows=rows
    result,counts=d.candidates(SimpleNamespace(lon=127,lat=37.5,radius_m=3000),cap=10,deadline=time.monotonic()+10)
    assert len(result)==1
    assert counts['retrieval_deadline']==1
    assert counts['strata_completed']==1


def test_endpoint_exclusion_cap_returns_unknown_before_assuming_absence():
    d=reader()
    d.rows=lambda table,bounds=None,limit=100,**kwargs:[{'geometry':box(0,0,1,1)}]*limit if table=='buildings' else []
    result=d.endpoint(record())
    assert result['state']=='unknown'
    assert result['reason']=='endpoint_exclusion_query_limit'
    assert result['layer']=='buildings'


def test_endpoint_reads_coincident_restriction_even_outside_retrieval_sample():
    d=reader();original=record();private=record(2,tags={'highway':'footway','access':'private'})
    d.rows=lambda table,bounds=None,limit=100,**kwargs:[private] if table=='candidates' else []
    assert d.endpoint(original)['reason']=='coincident_mapped_access_restriction'


def test_geometry_quarantine_and_failed_lineage_do_not_become_public():
    d=reader();r=record();d.excluded_source_ids={'way/1'}
    assert d.endpoint(r)['reason']=='quarantined_source_geometry'
    d.excluded_source_ids=set();x,y=r['geometry'].x,r['geometry'].y
    d.quarantine_regions=[(box(x-5,y-5,x+5,y+5),'relation/99')]
    assert d.endpoint(r)['state']=='unknown'


def test_target_quadrants_get_separate_bounded_queries_and_nearby_material_city():
    d=reader();calls=[]
    def rows(table,bounds=None,limit=100,**kwargs):
        calls.append((table,bounds,limit))
        if table!='buildings':return []
        sx=-1 if bounds[2]==0 else 1;sy=-1 if bounds[3]==0 else 1
        index={(1,1):1,(1,-1):2,(-1,1):3,(-1,-1):4}[(sx,sy)]
        return [{'fid':index,'geometry':box(sx*300-5,sy*300-5,sx*300+5,sy*300+5),'height_m':20,'source_id':f'building/{index}'},
                {'fid':index+10,'geometry':box(sx*6000-5,sy*6000-5,sx*6000+5,sy*6000+5),'height_m':150,'source_id':f'building/{index+10}'}]
    d.rows=rows
    targets=d.targets(0,0)
    assert len(calls)==16
    assert all(c[2]==500 for c in calls)
    assert {g['fid'] for g in targets[:4]}=={1,2,3,4}


def test_twenty_km_target_pool_keeps_near_strata_and_selects_outside_user_radius():
    d=reader();calls=[]
    def rows(table,bounds=None,limit=100,where='',params=(),**kwargs):
        calls.append((table,bounds,limit,params))
        if table!='buildings':return []
        sx=-1 if bounds[2]==0 else 1;sy=-1 if bounds[3]==0 else 1
        index={(1,1):1,(1,-1):2,(-1,1):3,(-1,-1):4}[(sx,sy)]
        if max(abs(v) for v in bounds)>10000:
            return [{'fid':99,'geometry':box(14995,995,15005,1005),'height_m':80,'source_id':'building/99'}]
        return [{'fid':index,'geometry':box(sx*300-5,sy*300-5,sx*300+5,sy*300+5),'height_m':20,'source_id':f'building/{index}'}]
    d.rows=rows
    groups=d.targets(0,0,20000)
    assert {g['fid'] for g in groups}=={1,2,3,4,99}
    target=next(g for g in groups if g['fid']==99)
    assert target['inventory_origin_distance_m']>10000
    assert target['geometry'].representative_point().distance(Point(9000,1000))==6000
    assert all(g['inventory_radius_m']==20000 for g in groups)
    assert len(calls)==32 and sum(c[2] for c in calls)==8000
    assert all(c[2]==250 for c in calls)
    assert any(c[3] for c in calls)
    with pytest.raises(ValueError):d.targets(0,0,20001)


def test_recovery_report_must_match_actual_manifest_osm_identity(tmp_path):
    d=reader();d.manifest={'artifacts':[{'path':'osm.gpkg','sha256':'a'*64}]}
    path=tmp_path/'report.json'
    path.write_text(json.dumps({'schema_version':1,'source_osm_gpkg_sha256':'b'*64,'excluded_source_ids':[],'quarantine_regions':[]}))
    with pytest.raises(ValueError,match='different source version'):d._load_recovery(path)
    m=json.loads(path.read_text());m['source_osm_gpkg_sha256']='a'*64;m['excluded_source_ids']=['way/10'];path.write_text(json.dumps(m))
    d._load_recovery(path)
    assert d.excluded_source_ids=={'way/10'}
    assert len(d.quality_version)==64


def test_viewport_map_uses_all_quadrants_priorities_and_deduplicates_features():
    d=reader();calls=[]
    def rows(table,bounds=None,limit=100,where='',order_by=None,**kwargs):
        calls.append((table,bounds,limit,order_by))
        if table!='water':return []
        x0,y0,x1,y1=bounds;index=len([c for c in calls if c[0]=='water'])
        # A broad river feature appears in every quadrant; localized patches
        # remain independently represented after source-ID deduplication.
        common={'fid':100,'geometry':box(180000,545000,220000,547000),'source_id':'way/river','name':'Synthetic river'}
        local={'fid':index,'geometry':box(x0+1,y0+1,x1-1,y1-1),'source_id':f'way/patch{index}','name':'Synthetic patch'}
        return [common,local]
    d.rows=rows
    result=d.map([126.8,37.45,127.15,37.65],12,cap=100)
    water=[f for f in result['features'] if f['properties']['layer']=='water']
    assert len(water)==5
    assert len({f['properties']['source_id'] for f in water})==5
    assert len(calls)==16
    assert all(c[3]=='largest_envelope' for c in calls if c[0]=='water')
    assert all(c[3]=='major_roads' for c in calls if c[0]=='paths')
    assert len(result['features'])<=100
    assert len(json.dumps(result).encode())<1_000_000


def test_rows_rejects_unknown_sort_policy_before_sql_execution():
    d=reader()
    d.connection=lambda table:None
    with pytest.raises(ValueError,match='Unapproved internal feature ordering'):
        d.rows('paths',bounds=(0,0,1,1),order_by='a.fid; DROP TABLE paths')


@pytest.mark.parametrize('tags,state',[
    ({'highway':'footway'},'map_supported'),
    ({'highway':'path'},'exploratory_only'),
    ({'highway':'path','foot':'yes'},'map_supported'),
    ({'highway':'footway','access':'private'},'excluded'),
    ({'highway':'footway','bridge':'yes'},'excluded'),
    ({'highway':'footway','opening_hours':'10:00-20:00'},'exploratory_only'),
    ({'leisure':'park'},'exploratory_only'),
])
def test_source_aware_access_no_absent_tag_permission(tags,state):
    assert access_policy(tags,{})['state']==state
