"""Bounded local investigation of failed OSM objects from the acquired PBF.

Native entity/ID filters recover only selected relation members and node
coordinates. No country-sized Python node dictionary, network access, source
mutation or automatic import of repaired public-space polygons is permitted.
Original failed objects remain quarantined. Administrative metadata without
geometry does not grant or withdraw access to unrelated mapped footpaths.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
from pathlib import Path
import resource
import sqlite3
import time

import osmium
from pyproj import Transformer
from shapely import LineString, box, is_valid_reason
from shapely.ops import polygonize_full, unary_union

VERSION='selected-osm-member-investigation-v1'
MAX_RELATIONS=128
MAX_WAYS=512
MAX_SELECTED_NODES=50000
MAX_MEMBERS=10000
MAX_SECONDS=90


def _sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()


def _check_deadline(deadline):
    if time.monotonic()>=deadline:raise ValueError('Selected OSM investigation deadline exceeded')


def classify(tags):
    if tags.get('boundary')=='administrative' or 'admin_level' in tags:return 'administrative'
    if tags.get('natural') in ('water','bay') or 'water' in tags or 'waterway' in tags:return 'water'
    if tags.get('man_made')=='bridge' or 'bridge' in tags:return 'bridge'
    if tags.get('natural') in ('wood','scrub') or tags.get('landuse')=='forest':return 'woodland'
    if tags.get('leisure') in ('park','garden','nature_reserve','recreation_ground') or tags.get('highway')=='pedestrian':return 'public_space'
    if tags.get('building') or tags.get('building:part'):return 'building'
    return 'other'


def recover_shape(ways, members, nodes):
    """Reconstruct only complete rings; no guessed closing segment or holes."""
    missing_nodes=sorted({ref for refs,role in members for ref in ways.get(refs,[]) if ref not in nodes})
    missing_ways=sorted({ref for ref,role in members if ref not in ways})
    if missing_nodes or missing_ways:
        return None,{'status':'missing_members','missing_way_ids':missing_ways,'missing_node_ids':missing_nodes}
    outer=[];inner=[]
    for way_id,role in members:
        coords=[nodes[n] for n in ways[way_id]]
        if len(coords)<2:
            return None,{'status':'degenerate_way','way_id':way_id,'node_count':len(coords)}
        (inner if role=='inner' else outer).append(LineString(coords))
    if not outer:return None,{'status':'no_outer_geometry'}
    # polygonize_full preserves open edges as explicit defects. It does not
    # create a polygon by silently connecting disconnected endpoints.
    outers,cuts,dangles,invalid=polygonize_full(outer)
    inners,icuts,idangles,iinvalid=polygonize_full(inner)
    defects=sum(len(x.geoms) for x in (cuts,dangles,invalid,icuts,idangles,iinvalid))
    if defects:return None,{'status':'ring_topology_invalid','unclosed_or_invalid_components':defects}
    polygon=unary_union(list(outers.geoms))
    if inner:
        holes=unary_union(list(inners.geoms))
        if not polygon.covers(holes):return None,{'status':'inner_ring_outside_outer'}
        polygon=polygon.difference(holes)
    if polygon.is_empty:return None,{'status':'empty_geometry'}
    original_valid=polygon.is_valid
    diagnostic={'status':'complete_valid_geometry' if original_valid else 'invalid_geometry',
        'original_valid':original_valid,'geometry_type':polygon.geom_type,'area_m2':polygon.area,
        'invalid_reason':None if original_valid else is_valid_reason(polygon),
        'repairs_written':0,'holes':sum(len(p.interiors) for p in polygon.geoms) if hasattr(polygon,'geoms') else len(polygon.interiors)}
    return polygon,diagnostic


def investigate(pbf_path,osm_gpkg,*,maximum_seconds=MAX_SECONDS):
    if not 1<=maximum_seconds<=MAX_SECONDS:raise ValueError('Investigation deadline exceeds bounded maximum')
    started=time.perf_counter();deadline=time.monotonic()+maximum_seconds
    path=Path(pbf_path).resolve(strict=True);before=path.stat()
    reader=osmium.io.Reader(str(path),osmium.osm.NOTHING)
    try:snapshot_timestamp=reader.header().get('osmosis_replication_timestamp') or None
    finally:reader.close()
    c=sqlite3.connect(Path(osm_gpkg).resolve().as_uri()+'?mode=ro&immutable=1',uri=True)
    c.execute('pragma query_only=ON')
    try:ids=[r[0] for r in c.execute('select source_id from quality')]
    finally:c.close()
    relation_ids={int(v.split('/')[1]) for v in ids if v.startswith('relation/')}
    way_ids={int(v.split('/')[1]) for v in ids if v.startswith('way/')}
    if len(relation_ids)>MAX_RELATIONS or len(way_ids)>MAX_WAYS:raise ValueError('Unbounded diagnostic source ID set')
    relations={};admin=[]
    for relation in osmium.FileProcessor(str(path),entities=osmium.osm.RELATION).with_filter(osmium.filter.IdFilter(relation_ids)):
        _check_deadline(deadline)
        if len(relation.members)>MAX_MEMBERS:raise ValueError('Relation member cap exceeded')
        tags=dict(relation.tags);category=classify(tags)
        record={'source_id':f'relation/{relation.id}','tags':tags,'version':relation.version,
            'source_timestamp':str(relation.timestamp),'category':category,
            'members':[(m.type,m.ref,m.role) for m in relation.members]}
        if category=='administrative':
            record['status']='administrative_source_metadata_retained; no public-space reconstruction'
            admin.append(record)
        else:
            relations[relation.id]=record
            way_ids.update(m.ref for m in relation.members if m.type=='w')
    original_relations=set(relations)
    nested_requested=set()
    for depth in range(3):
        pending={ref for r in relations.values() for typ,ref,role in r['members'] if typ=='r'}-set(relations)-nested_requested
        if not pending:break
        nested_requested.update(pending)
        if len(relations)+len(pending)>MAX_RELATIONS:raise ValueError('Nested relation cap exceeded')
        for relation in osmium.FileProcessor(str(path),entities=osmium.osm.RELATION).with_filter(osmium.filter.IdFilter(pending)):
            _check_deadline(deadline)
            if len(relation.members)>MAX_MEMBERS:raise ValueError('Nested relation member cap exceeded')
            relations[relation.id]={'source_id':f'relation/{relation.id}','tags':dict(relation.tags),
                'version':relation.version,'source_timestamp':str(relation.timestamp),
                'category':classify(dict(relation.tags)),
                'members':[(m.type,m.ref,m.role) for m in relation.members]}
            way_ids.update(m.ref for m in relation.members if m.type=='w')
    _check_deadline(deadline)
    if len(way_ids)>MAX_WAYS:raise ValueError('Selected way cap exceeded')
    ways={};way_records={};node_ids=set()
    for way in osmium.FileProcessor(str(path),entities=osmium.osm.WAY).with_filter(osmium.filter.IdFilter(way_ids)):
        _check_deadline(deadline)
        refs=[n.ref for n in way.nodes]
        if len(refs)>MAX_MEMBERS:raise ValueError('Selected way node cap exceeded')
        ways[way.id]=refs;node_ids.update(refs)
        way_records[way.id]={'source_id':f'way/{way.id}','tags':dict(way.tags),'version':way.version,
            'source_timestamp':str(way.timestamp),'category':classify(dict(way.tags))}
        if len(node_ids)>MAX_SELECTED_NODES:raise ValueError('Selected coordinate memory cap exceeded')
    _check_deadline(deadline)
    transform=Transformer.from_crs(4326,5186,always_xy=True);nodes={}
    for node in osmium.FileProcessor(str(path),entities=osmium.osm.NODE).with_filter(osmium.filter.IdFilter(node_ids)):
        _check_deadline(deadline)
        if node.location.valid():nodes[node.id]=transform.transform(node.location.lon,node.location.lat)
    _check_deadline(deadline)
    objects=[];regions=[];excluded_ids=set(ids)
    def flatten(rid,inner=False,parents=()):
        if rid not in relations or rid in parents or len(parents)>3:return [],False
        out=[];complete=True
        for typ,ref,role in relations[rid]['members']:
            effective_inner=inner^(role=='inner')
            if typ=='w':out.append((ref,'inner' if effective_inner else 'outer'))
            elif typ=='r':
                child,valid=flatten(ref,effective_inner,(*parents,rid));out.extend(child);complete &= valid
        return out,complete
    targets=[]
    for rid in original_relations:
        members,complete=flatten(rid)
        targets.append(({**relations[rid],'nested_members_resolved':complete},members))
    targets.extend((v,[(wid,'outer')]) for wid,v in way_records.items() if f'way/{wid}' in ids)
    city=None
    coverage=Path(osm_gpkg).resolve().parent/'coverage.gpkg'
    if coverage.exists():
        from .geometry_inspection import geometry
        c=sqlite3.connect(coverage.as_uri()+'?mode=ro&immutable=1',uri=True)
        try:city=geometry(c.execute("select geom from requested_extents where source_id='recommendation'").fetchone()[0])
        finally:c.close()
    for record,members in targets:
        polygon,diagnostic=recover_shape(ways,members,nodes)
        points=[nodes[ref] for wid,role in members for ref in ways.get(wid,[]) if ref in nodes]
        complete_nodes=all(ref in nodes for wid,role in members for ref in ways.get(wid,[]))
        missing_ways=any(wid not in ways for wid,role in members)
        bounds=None
        if points and complete_nodes and not missing_ways and record.get('nested_members_resolved',True):
            xs,ys=zip(*points);bounds=[min(xs),min(ys),max(xs),max(ys)]
        result={k:v for k,v in record.items() if k!='members'}
        result.update(diagnostic,coordinates_recovered=len(points),bounds_epsg5186=bounds,
            all_way_members_present=not missing_ways,all_node_members_present=complete_nodes,
            published_source_unchanged=True,application_status='quarantined_original_diagnostic')
        result['bounds_intersect_seoul']=None if bounds is None or city is None else box(*bounds).intersects(city)
        if bounds and record['category']!='administrative':
            regions.append({'bounds':[bounds[0]-5,bounds[1]-5,bounds[2]+5,bounds[3]+5],
                'source_id':record['source_id'],'category':record['category'],
                'reason':'osm_original_geometry_issue_locally_recovered_or_localized'})
        objects.append(result)
    if before.st_size!=path.stat().st_size or before.st_mtime_ns!=path.stat().st_mtime_ns:
        raise ValueError('OSM source changed during selected recovery')
    return {'schema_version':1,'processing_version':VERSION,'status':'local_member_investigation_complete',
        'source_pbf_sha256':_sha(path),'source_osm_gpkg_sha256':_sha(osm_gpkg),
        'source_date_not_download_date':snapshot_timestamp,
        'original_diagnostic_count':len(ids),'selected_relation_count':len(relation_ids),
        'selected_way_count':len(way_ids),'recovered_way_count':len(ways),
        'selected_node_count':len(node_ids),'recovered_node_count':len(nodes),
        'missing_selected_node_ids':sorted(node_ids-set(nodes)),
        'missing_selected_way_ids':sorted(way_ids-set(ways)),
        'maximum_selected_nodes':MAX_SELECTED_NODES,'objects':objects,
        'administrative_diagnostic_count':len(admin),'administrative_metadata':admin,
        'nested_relations_requested':sorted(nested_requested),'nested_relations_recovered':sorted(set(relations)-original_relations),
        'excluded_source_ids':sorted(excluded_ids),'quarantine_regions':regions,
        'unlocalized_nonadministrative_ids':[v['source_id'] for v in objects if v['bounds_epsg5186'] is None],
        'elapsed_s':time.perf_counter()-started,'peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
        'network_requests':0,'published_geometry_writes':0,'repairs_written':0,'global_readiness_changed':False,
        'policy':['Use complete recovered coordinates for localized quarantine only; do not invent or import public areas',
            'Exclude original failed source IDs and candidate lineage from supported recommendations',
            'Unresolved administrative geometry remains documented; it is not evidence about unrelated ordinary footpath access',
            'Recovered bounds do not validate standing elevation, current access, water level or terrain canopy']}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--pbf',required=True)
    parser.add_argument('--osm',required=True);parser.add_argument('--seconds',type=float,default=MAX_SECONDS)
    a=parser.parse_args();print(json.dumps(investigate(a.pbf,a.osm,maximum_seconds=a.seconds),indent=2,ensure_ascii=False))

if __name__=='__main__':main()
