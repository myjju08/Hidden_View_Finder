"""Reproducible bounded read-only geometry smoke; prints a small JSON report.

Run with the repository Python wrapper. This is geometric inspection of real
source records, not candidate access approval or field-verified recommendations.
No source or raster files are changed. Limit/deadline prevent citywide pairing.
"""
from __future__ import annotations
import argparse
from collections import Counter
import json
from pathlib import Path
import resource
import sqlite3
import time

from shapely import from_wkb
from .tiles import Tiles, WorkBudget, inspect_uncertainty


def geometry(blob):
    if bytes(blob[:2]) != b"GP":
        raise ValueError("Expected GeoPackage geometry")
    skip=8+{0:0,1:32,2:48,3:48,4:64}[(blob[3]>>1)&7]
    return from_wkb(blob[skip:])


def inspect(manifest,buildings,candidates,overlap,*,maximum_observers=40,seconds=30):
    if not 1<=maximum_observers<=100 or not 1<=seconds<=60:
        raise ValueError("Read-only geometry smoke exceeds its bounded scope")
    start=time.perf_counter()
    uncertainty=inspect_uncertainty(buildings,overlap)
    tiles=Tiles(manifest,uncertainty['regions'],overlap_anomalies_path=overlap)
    connections=[sqlite3.connect(Path(p).resolve().as_uri()+'?mode=ro&immutable=1',uri=True) for p in (buildings,candidates)]
    for c in connections:
        c.execute('pragma query_only=ON');c.execute('pragma cache_size=-4096')
    bc,cc=connections
    stats=Counter();examples=[];tested=[]
    work=WorkBudget(max_rays=1000,max_cells=2_000_000,deadline=time.monotonic()+seconds)
    started=time.perf_counter()
    try:
        maximum_fid=cc.execute('select max(fid) from candidates').fetchone()[0]
        stride=max(1,maximum_fid//maximum_observers)
        for fid in range(stride,maximum_fid+1,stride):
            if not work.check() or stats['candidate_records_inspected']>=maximum_observers:
                break
            record=cc.execute('select source_id,geom from candidates where fid=?',(fid,)).fetchone()
            if not record:continue
            sid,blob=record;p=geometry(blob);x,y=p.x,p.y
            stats['candidate_records_inspected']+=1
            observer=tiles.observer(x,y)
            stats['observer_'+observer['state']]+=1
            if observer['state']!='supported':continue
            for building_id,blob in bc.execute('select b.fid,b.geom from rtree_buildings_geom r join buildings b on b.fid=r.id '
                    'where r.minx<=? and r.maxx>=? and r.miny<=? and r.maxy>=? and b.invalid_geometry=0 '
                    'order by b.height_m desc limit 4',(x+1000,x-1000,y+1000,y-1000)):
                if not work.check():break
                shape=geometry(blob);point=shape.representative_point()
                target=tiles.roof_boundary_target((x,y),(point.x,point.y))
                if target['state']!='supported':
                    stats['target_'+target['state']]+=1;continue
                result=tiles.ray(observer['xyz'],target['xyz'],work=work)
                stats['ray_'+result['state']]+=1
                tested.append((observer['xyz'],target['xyz']))
                if len(examples)<10 and (result['state']=='visible' or len(examples)<2):
                    examples.append({'candidate_source_id':sid,'building_fid':building_id,
                        'standing_lonlat':list(tiles.to_lonlat.transform(x,y)),
                        'target_lonlat':list(tiles.to_lonlat.transform(*target['effective_xy'])),
                        'observer':observer,'target':target,'ray':result,
                        'access_eligibility_assessed':False,'field_verified':False})
        cold_seconds=time.perf_counter()-started
        warm_started=time.perf_counter();warm_states=Counter()
        warm_work=WorkBudget(max_rays=1000,max_cells=2_000_000,deadline=time.monotonic()+seconds)
        for a,b in tested:
            if not warm_work.check():break
            warm_states[tiles.ray(a,b,work=warm_work)['state']]+=1
        warm_seconds=time.perf_counter()-warm_started
        if tested:
            horizon=tiles.horizon(tested[0][0],270,10000,work=WorkBudget(deadline=time.monotonic()+3))
        else:horizon=None
        return {'schema_version':1,'kind':'real-data bounded geometry smoke; not access-approved recommendations',
            'source_versions':tiles.metadata,'uncertainty':uncertainty['report'],
            'sampling':'evenly distributed candidate primary keys; up to4 nearby tallest source buildings each',
            'scope':{'maximum_observers':maximum_observers,'maximum_nearby_buildings_each':4,'target_search_halfwidth_m':1000},
            'counts':dict(stats),'geometry_work':work.as_dict(),'warm_states':dict(warm_states),
            'timings_s':{'adapter_hash_and_metadata_open':tiles.metadata['open_seconds'],
                'cold_retrieval_and_geometry':cold_seconds,'warm_geometry_same_pairs':warm_seconds,
                'total':time.perf_counter()-start},'examples':examples,'directional_horizon_example':horizon,
            'reader_stats':tiles.stats,'peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
            'network_requests':0,'source_data_writes':0,'global_readiness_changed':False,
            'limitations':['Primary-key sampling is a smoke test, not a spatial probability sample',
                'Candidate access and requested/effective standing footprint exclusion are evaluated by the application, not this geometry smoke',
                'Roof samples are model-column top edges, not complete facade or skyline coverage']}
    finally:
        tiles.close()
        for c in connections:c.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',required=True);parser.add_argument('--buildings',required=True)
    parser.add_argument('--candidates',required=True);parser.add_argument('--overlap',required=True)
    parser.add_argument('--maximum-observers',type=int,default=40);parser.add_argument('--seconds',type=float,default=30)
    args=parser.parse_args()
    print(json.dumps(inspect(args.manifest,args.buildings,args.candidates,args.overlap,
        maximum_observers=args.maximum_observers,seconds=args.seconds),ensure_ascii=False,indent=2))

if __name__=='__main__':main()
