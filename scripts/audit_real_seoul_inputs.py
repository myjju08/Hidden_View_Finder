#!/usr/bin/env python3
"""Independently audit acquired Seoul GBA inputs with bounded HTTP source checks.

Run after acquisition: .venv/bin/python scripts/audit_real_seoul_inputs.py
Reads source metadata plus one row group (~2.5 MB); never downloads a full tile.
Writes reports/real_seoul_input_audit.json. No source/prepared data are edited.
"""
from pathlib import Path
import json,hashlib,importlib.util,collections,time,math
import numpy as np
from osgeo import ogr
from shapely import from_wkb,is_valid,has_z,is_empty,intersects,area,make_valid
from shapely.geometry import shape,box,Point
from shapely.ops import transform,unary_union
from pyproj import Transformer
import pyarrow.parquet as pq
ogr.UseExceptions()
started=time.perf_counter();root=Path(__file__).resolve().parents[1]; p=root/'data/acquisition/buildings/gba_seoul_2025.gpkg'
source=json.loads(p.with_suffix('.source.json').read_text());spec=importlib.util.spec_from_file_location('acq',root/'scripts/acquire_buildings.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
r=mod.Ranges(source['source_url'],10*1024**2);par=pq.ParquetFile(r,pre_buffer=False);metadata=par.metadata;geo=json.loads(metadata.metadata[b'geo']);west,south,east,north=source['bbox_lon_lat'];selected=[];nulls=[];rg_bounds=[]
for n in range(metadata.num_row_groups):
 row=metadata.row_group(n);stats={row.column(j).path_in_schema:row.column(j).statistics for j in range(row.num_columns)}
 keys=['bbox.xmin','bbox.ymin','bbox.xmax','bbox.ymax'];
 for k in keys:
  s=stats[k]
  if s is None or not s.has_min_max or s.null_count or s.num_values!=row.num_rows:nulls.append([n,k,None if s is None else {'has_min_max':s.has_min_max,'null_count':s.null_count,'num_values':s.num_values,'rows':row.num_rows}])
 bounds=(stats[keys[0]].min,stats[keys[1]].min,stats[keys[2]].max,stats[keys[3]].max)
 if not (bounds[2]<west or bounds[3]<south or bounds[0]>east or bounds[1]>north):selected.append(n)
 rg_bounds.append(bounds)
assert selected==source['row_groups_selected'];assert not nulls
http={'etag_matches_acquisition':r.etag==source['http_etag'],'version_matches_acquisition':r.version==source['object_version'],'bytes_read':r.bytes_read,'selected_groups_independent':selected,'total_groups':metadata.num_row_groups,'bounds_statistics_null_or_missing':nulls,'selection_audit':'All excluded row-group envelope unions disjoint from requested bounding box; all bbox statistic columns cover every row; exact intersection selection is conservative.'}
print('metadata audit',http,flush=True)
# Inspect full retained input in bounded batches, leaving raw geometries untouched.
ds=ogr.Open(str(p));layer=ds.GetLayer(0);schema=[(f.GetName(),f.GetTypeName()) for f in layer.schema];sr=layer.GetSpatialRef();dims=collections.Counter();sources=collections.Counter();invalid=[];outside=empty=z=count=unresolved=unestimated=0;ids=set();duplicates=0;heights=[];selection_region=box(*source['bbox_lon_lat']);batch=[]
def check(batch):
 global count,outside,empty,z,duplicates,unresolved,unestimated
 geoms=from_wkb([x[1] for x in batch]);valid=is_valid(geoms)
 for i in np.flatnonzero(~valid):invalid.append({'fid':batch[i][0],'source_id':batch[i][2],'source':batch[i][3],'reason':'invalid geometry; raw retained'})
 outside+=int(np.count_nonzero(~intersects(geoms,selection_region)));empty+=int(np.count_nonzero(is_empty(geoms)));z+=int(np.count_nonzero(has_z(geoms)))
 for fid,wkb,sourceid,origin,height,estimated,bad in batch:
  count+=1;sources[origin]+=1;unresolved+=int(bad!=0 or height is None or not math.isfinite(height) or height<=0);unestimated+=int(estimated!=1)
  key=(origin,sourceid)
  if key in ids:duplicates+=1
  ids.add(key)
  if height is not None and math.isfinite(height):heights.append(height)
for feature in layer:
 geometry=feature.GetGeometryRef();dims[geometry.GetGeometryName()]+=1
 batch.append((feature.GetFID(),bytes(geometry.ExportToWkb()),feature['source_id'],feature['source'],feature['height_m'],feature['estimated'],feature['unresolved']))
 if len(batch)==10000:check(batch);batch=[]
if batch:check(batch)
raw={'path':str(p.relative_to(root)),'sha256_matches_provenance':hashlib.sha256(p.read_bytes()).hexdigest()==source['sha256'],'count':count,'schema':schema,'crs_authority':sr.GetAuthorityCode(None),'coordinate_dimensions_Z_count':z,'geometry_types':dict(dims),'geometry_empty_count':empty,'invalid_geometry':invalid,'all_features_intersect_requested_bbox':outside==0,'outside_requested_bbox_count':outside,'all_marked_estimated':unestimated==0,'unresolved_height_count':unresolved,'duplicate_source_and_id_count':duplicates,'sources':dict(sources),'height_quantiles_0_1_5_25_50_75_95_99_100_m':np.quantile(heights,[0,.01,.05,.25,.5,.75,.95,.99,1]).tolist(),'height_below_0_1_1_1_7_3m':{str(v):int(np.count_nonzero(np.array(heights)<v)) for v in [.1,1,1.7,3]}}
print('retained features audited',raw['count'],flush=True)
xy=Transformer.from_crs(4326,5186,always_xy=True);ll=Transformer.from_crs(5186,4326,always_xy=True)
regions={}
for name,bounds in {'working':[192500,547500,204000,559000],'pilot':[195500,550500,200500,555500]}.items():
 poly=box(*bounds);geographic=transform(ll.transform,poly);envelope=ll.transform_bounds(*bounds,densify_pts=21);layer.SetSpatialFilterRect(*envelope);stats={'bounds_epsg5186':bounds,'bounds_lonlat_densified':envelope,'inside_acquisition_bbox':selection_region.covers(geographic),'count_intersecting':0,'fully_contained_count':0,'sources':collections.Counter(),'heights':[],'invalid_fids':[],'low_height_features':[]}
 for feature in layer:
  g=from_wkb(bytes(feature.GetGeometryRef().ExportToWkb()));gxy=transform(xy.transform,g)
  if not gxy.intersects(poly):continue
  stats['count_intersecting']+=1;stats['fully_contained_count']+=int(poly.covers(gxy));stats['sources'][feature['source']]+=1;h=feature['height_m'];stats['heights'].append(h)
  if not gxy.is_valid:stats['invalid_fids'].append(feature.GetFID())
  if h<1:stats['low_height_features'].append({'fid':feature.GetFID(),'source_id':feature['source_id'],'height_m':h,'area_m2':gxy.area})
 hs=np.array(stats.pop('heights'));stats['height_quantiles_0_1_5_25_50_75_95_99_100_m']=np.quantile(hs,[0,.01,.05,.25,.5,.75,.95,.99,1]).tolist();stats['height_below_m']={str(v):int(np.count_nonzero(hs<v)) for v in [.1,1,1.7,3]};regions[name]=stats
layer.SetSpatialFilter(None)
# Validate administrative coverage using explicitly repaired historical features.
bp=root/'data/acquisition/boundary';osm=transform(xy.transform,shape(json.loads((bp/'seoul_boundary_osm_20260907.geojson').read_text())['features'][0]['geometry']))
old=ogr.Open(str(bp/'official_2014/TL_SCCO_SIG_W.shp'));oldlayer=old.GetLayer(0);repaired=[];oldgs=[]
for f in oldlayer:
 g=from_wkb(bytes(f.GetGeometryRef().ExportToWkb()))
 if not g.is_valid:
  before=g.area;g=make_valid(g);repaired.append({'fid':f.GetFID(),'method':'GEOS make_valid in memory only; original unchanged','result_type':g.geom_type,'area_change_degrees2':g.area-before})
 oldgs.append(g)
off=transform(xy.transform,unary_union(oldgs));target_xy=xy.transform(126.9777,37.578);windows={'radius_5000_plus_25m_square':box(target_xy[0]-5025,target_xy[1]-5025,target_xy[0]+5025,target_xy[1]+5025),'radius_5000_plus_25m_circle':Point(*target_xy).buffer(5025,quad_segs=256),'prepared_working':box(192500,547500,204000,559000),'pilot':box(195500,550500,200500,555500)}
boundaries={'target_lonlat':[126.9777,37.578],'target_xy_epsg5186':target_xy,'historical_repairs':repaired,'osm_valid':osm.is_valid,'official_union_valid':off.is_valid,'windows':{name:{'osm_covers':osm.covers(win),'official_2014_covers':off.covers(win),'osm_outside_area_m2':win.difference(osm).area,'official_outside_area_m2':win.difference(off).area,'osm_boundary_clearance_m':win.distance(osm.boundary),'official_boundary_clearance_m':win.distance(off.boundary),'bounds_epsg5186':win.bounds}for name,win in windows.items()}}
report={'audit_utc':'2026-09-07','status':'APPROXIMATE research screening inputs; not a certified complete current Seoul survey','http_rowgroup_audit':http,'geoparquet_geometry_metadata':geo['columns']['geometry'],'retained_gpkg_audit':raw,'regions':regions,'boundary_audit':boundaries,'age_and_semantics':{'height_reference':'estimated AGL metres; not absolute elevations','observation_age':'Paper Section4.2: PlanetScope primary2019, fallback2018; footprint sources mixed newer dates. September2025 sourceobject date is release/upload, not survey date.','primary_source':'https://essd.copernicus.org/articles/17/6647/2025/','no_mesh':'WKB Polygon only; zeroZ geometries; height is scalarattribute','licences':['ODbL for OSM/Microsoft footprints','CC BY-NC4.0 other footprints/heightproduct; mixed-source conversion does not override upstream licences']},'limitations':['Sub-metre positive heights are numerically resolved source estimates but physically suspect for ordinary buildings; do not silently clamp or label surveyed.','No blanket assumption of realworld footprint completeness: source explicitly warns missingbuildings/height errors.','Administrative boundary coverage is not proof of building survey completeness.','WesternSeoul beyond126.82longitude was not acquired; working/pilot bboxcoverage explicitly audited.','2015 official inventory comparisons are historical discrepancies, not groundtruth validation of current2026 GBA.','Rowgroupbounds completeness relies on GeoParquet bboxstatistics truth; independently sampledrowchecks can test consistency, not allmissingrealworldbuildings.'],'elapsed_s':time.perf_counter()-started}
(root/'reports/real_seoul_input_audit.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8');print(json.dumps({'audit_json':str(root/'reports/real_seoul_input_audit.json'),'elapsed_s':report['elapsed_s']},ensure_ascii=False),flush=True)

# Independent source-row consistency and exact GeoPackage matches.
from pathlib import Path
import json,importlib.util
import numpy as np
import pyarrow.parquet as pq
from shapely import from_wkb,bounds,intersects
from shapely.geometry import box
from osgeo import ogr
root=Path(__file__).resolve().parents[1];prov=json.loads((root/'data/acquisition/buildings/gba_seoul_2025.source.json').read_text());spec=importlib.util.spec_from_file_location('acq',root/'scripts/acquire_buildings.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod);remote=mod.Ranges(prov['source_url'],8*1024**2);file=pq.ParquetFile(remote,pre_buffer=False);group=prov['row_groups_selected'][len(prov['row_groups_selected'])//2];rows=file.read_row_group(group,use_threads=False).to_pylist();geoms=from_wkb([r['geometry'] for r in rows]);actual=bounds(geoms);declared=np.array([[r['bbox'][k] for k in ['xmin','ymin','xmax','ymax']]for r in rows]);delta=np.abs(actual-declared);assert np.all(delta<=1e-12)
eligible=np.flatnonzero(intersects(geoms,box(*prov['bbox_lon_lat'])));rng=np.random.default_rng(20260907);chosen=rng.choice(eligible,min(64,len(eligible)),replace=False);ogr.UseExceptions();ds=ogr.Open(str(root/'data/acquisition/buildings/gba_seoul_2025.gpkg'));layer=ds.GetLayer(0);matches=[]
for ix in chosen:
 r=rows[ix];g=geoms[ix];layer.SetSpatialFilterRect(*g.bounds);found=[]
 for f in layer:
  fg=from_wkb(bytes(f.GetGeometryRef().ExportToWkb()))
  if fg.equals_exact(g,0) and f['height_m']==r['height'] and f['source']==r['source'] and f['source_id']==str(r['id']):found.append(f.GetFID())
 matches.append({'source_row':int(ix),'matched_fids':found})
assert all(x['matched_fids'] for x in matches)
report={'row_group':group,'source_rows':len(rows),'bbox_exact_max_abs_error_degrees':float(delta.max()),'bbox_all_match':True,'random_seed':20260907,'source_rows_randomly_checked':len(matches),'geometry_height_origin_id_matches':sum(bool(x['matched_fids']) for x in matches),'read_bytes':remote.bytes_read,'source_version_still_matches':remote.version==prov['object_version'],'sample_matches':matches}
p=root/'reports/real_seoul_input_audit.json';j=json.loads(p.read_text());j['independent_source_row_audit']=report;p.write_text(json.dumps(j,ensure_ascii=False,indent=2)+'\n');print(json.dumps({k:v for k,v in report.items() if k!='sample_matches'},indent=2))
