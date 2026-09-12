"""Read-only GeoPackage/RTree adapter; bounded source-aware spatial retrieval.

No whole-city export, mutable GIS handles, implicit CRS, or copied city package.
"""
from __future__ import annotations
from pathlib import Path
import sqlite3,json,math,time,hashlib
from collections import Counter
from shapely import from_wkb
from shapely.geometry import Point,box,mapping
from shapely.ops import transform
from pyproj import Transformer,Geod
from seoul_visibility.acquisition_safety import sha256
TO_M=Transformer.from_crs(4326,5186,always_xy=True)
TO_LL=Transformer.from_crs(5186,4326,always_xy=True)
GEOD=Geod(ellps='GRS80')
TABLES={'candidates','buildings','districts','requested_extents','terrain_support','coverage_grid','water','green_space','peaks_ridges','public_spaces','landmarks','paths','barriers','boundaries','bridges','quality'}
OSM_TABLES={'water','green_space','peaks_ridges','public_spaces','landmarks','paths','barriers','boundaries','bridges','quality'}
ROAD_TYPES={'motorway','trunk','primary','secondary','tertiary','residential','service','unclassified','living_street','motorway_link','trunk_link','primary_link','secondary_link','tertiary_link'}

def geometry(blob):
 if blob[:2]!=b'GP': raise ValueError('Expected GeoPackage geometry')
 flags=blob[3];code=(flags>>1)&7
 if code not in {0,1,2,3,4}: raise ValueError('Unknown GeoPackage envelope')
 return from_wkb(blob[8+{0:0,1:32,2:48,3:48,4:64}[code]:])

def access_policy(tags,evidence):
 """Dedicated public pedestrian use is positive map evidence, never live access."""
 why=[]
 if tags.get('access') in {'no','private','customers','permit','military'} or tags.get('foot') in {'no','private','use_sidepath'}:
  return {'state':'excluded','reason':'mapped_access_restriction'}
 if tags.get('highway')=='construction' or tags.get('construction') or tags.get('disused')=='yes' or tags.get('indoor')=='yes':
  return {'state':'excluded','reason':'unusable_or_indoor'}
 if (evidence.get('observer_elevation_status')=='unsupported_structure' or tags.get('bridge') not in {None,'no'} or tags.get('tunnel') not in {None,'no'} or tags.get('layer') not in {None,'0'} or tags.get('highway')=='steps'):
  return {'state':'excluded','reason':'unsupported_structure_or_step_elevation'}
 if tags.get('opening_hours') or tags.get('access:conditional') or tags.get('foot:conditional') or tags.get('barrier') in {'gate','lift_gate','turnstile'}:
  return {'state':'exploratory_only','reason':'schedule_or_gate_unresolved'}
 h=tags.get('highway')
 if h in ROAD_TYPES or h=='track': return {'state':'excluded','reason':'road_carriageway_or_track_centerline'}
 positive=h in {'footway','pedestrian'} or h in {'path','cycleway'} and tags.get('foot') in {'yes','designated','permissive'}
 if not positive: return {'state':'exploratory_only','reason':'pedestrian_access_unresolved'}
 if tags.get('access') not in {None,'yes','permissive','designated'}: return {'state':'exploratory_only','reason':'uninterpreted_access'}
 return {'state':'map_supported','reason':'dedicated_mapped_pedestrian_use','evidence':{k:v for k,v in tags.items() if k in {'highway','foot','access','surface','incline','wheelchair','lit'}},'currently_open':None,'route_accessibility':'not_checked','field_verified':False}

class Data:
 def __init__(self,manifest,recovery_report=None):
  self.path=Path(manifest);self.manifest=json.loads(self.path.read_text());self.root=self.path.parent;self.connections={}
  if self.manifest.get('schema_version')!=1 or not self.manifest.get('crs','').startswith('EPSG:5186'): raise ValueError('Unsupported package schema/CRS')
  self.version=self.manifest['package_id']
  self._load_recovery(Path(recovery_report) if recovery_report else Path(__file__).resolve().parents[3]/'reports/prototype/osm-recovery.json')
  for file in ('candidates.gpkg','buildings.gpkg','osm.gpkg','coverage.gpkg'):
   if not (self.root/file).is_file(): raise FileNotFoundError(str(self.root/file))
  self.districts=self.rows('districts',limit=30)
  ex=json.loads((self.root/'extents.geojson').read_text())
  self.extents=ex
 def _load_recovery(self,path):
  if not path.is_file() or path.stat().st_size>2_000_000: raise ValueError('Missing/bounded local OSM member investigation report; run prototype osm_recovery inspection')
  report=json.loads(path.read_text());expected=next((a['sha256'] for a in self.manifest['artifacts'] if a['path']=='osm.gpkg'),None)
  if report.get('schema_version')!=1 or report.get('source_osm_gpkg_sha256')!=expected: raise ValueError('OSM member investigation belongs to a different source version')
  self.quality_version=sha256(path)
  self.excluded_source_ids=set(report['excluded_source_ids'])
  self.quarantine_regions=[]
  for row in report['quarantine_regions']:
   bounds=row['bounds']
   if len(bounds)!=4 or not all(math.isfinite(v) for v in bounds) or bounds[0]>bounds[2] or bounds[1]>bounds[3]: raise ValueError('Unlocalized OSM quarantine region')
   self.quarantine_regions.append((box(*bounds),row['source_id']))
  self.unlocalized_osm_ids=report.get('unlocalized_nonadministrative_ids',[])
 def source_excluded(self,row):
  return row.get('source_id') in self.excluded_source_ids or row.get('evidence',{}).get('source_id') in self.excluded_source_ids
 def connection(self,table):
  if table not in TABLES: raise ValueError('Unapproved layer')
  file='osm.gpkg' if table in OSM_TABLES else ('candidates.gpkg' if table=='candidates' else 'buildings.gpkg' if table=='buildings' else 'coverage.gpkg')
  if file not in self.connections:
   c=sqlite3.connect('file:'+str(self.root/file)+'?mode=ro&immutable=1',uri=True);c.row_factory=sqlite3.Row;c.execute('pragma query_only=ON');c.execute('pragma cache_size=-2048');c.execute('pragma temp_store=MEMORY')
   self.connections[file]=c
  c=self.connections[file]
  info=c.execute('select srs_id from gpkg_geometry_columns where table_name=?',(table,)).fetchone()
  if not info or info[0]!=5186: raise ValueError('Missing/incompatible source CRS')
  return c
 def rows(self,table,bounds=None,limit=100,where='',params=(),deadline=None,order_by=None):
  c=self.connection(table);limit=min(10000,int(limit));sql=f'select a.* from "{table}" a';values=[]
  if bounds is not None:
   x0,y0,x1,y1=bounds;sql+=f' join "rtree_{table}_geom" r on a.fid=r.id where r.maxx>=? and r.minx<=? and r.maxy>=? and r.miny<=?';values=[x0,x1,y0,y1]
  else: sql+=' where 1'
  if where: sql+=' and ('+where+')';values.extend(params)
  if order_by is not None:
   # Fixed internal policies only; no request-supplied SQL or identifiers.
   orders={'largest_envelope':'(r.maxx-r.minx)*(r.maxy-r.miny) DESC,a.fid',
       'major_roads':"CASE json_extract(a.tags_json,'$.highway') WHEN 'motorway' THEN 0 WHEN 'trunk' THEN 1 WHEN 'primary' THEN 2 WHEN 'secondary' THEN 3 WHEN 'tertiary' THEN 4 WHEN 'pedestrian' THEN 5 ELSE 6 END,a.fid"}
   if order_by not in orders or order_by=='largest_envelope' and bounds is None:raise ValueError('Unapproved internal feature ordering')
   sql+=' ORDER BY '+orders[order_by]
  sql+=' limit ?';values.append(limit)
  if deadline: c.set_progress_handler(lambda:int(time.monotonic()>deadline),1000)
  try:
   result=[]
   for row in c.execute(sql,values):
    d=dict(row);d['geometry']=geometry(d.pop('geom'))
    for k in ('tags','evidence'):
     if k+'_json' in d: d[k]=json.loads(d.pop(k+'_json'))
    result.append(d)
   return result
  finally: c.set_progress_handler(None,0)
 def district(self,p):
  return next((d['name'] for d in self.districts if d['geometry'].covers(p)),'boundary_unassigned')
 def candidates(self,q,cap=160,deadline=None):
  x,y=TO_M.transform(q.lon,q.lat);radius=q.radius_m;seen={};counts=Counter();side=8
  if not math.isfinite(x+y):return [],{'representatives':0,'origin_projected_domain_unsupported':1}
  restricted_positions=set();stop=False
  # 64 geographic strata, interleaved positions. SQL filters broad pedestrian
  # categories; Python applies restrictions before selecting representatives.
  buckets=[]
  for iy in range(side):
   if stop: break
   for ix in range(side):
    if deadline and time.monotonic()>deadline: counts['retrieval_deadline']=1;stop=True;break
    x0=x-radius+2*radius*ix/side;y0=y-radius+2*radius*iy/side
    try: rows=self.rows('candidates',(x0,y0,x0+2*radius/side,y0+2*radius/side),limit=120,where="json_extract(a.tags_json,'$.highway') in ('footway','pedestrian','path','cycleway')",deadline=deadline)
    except sqlite3.OperationalError as exc:
     if 'interrupt' not in str(exc).lower(): raise
     counts['retrieval_deadline']=1;stop=True;break
    counts['strata_completed']+=1
    eligible=[]
    for r in rows:
     counts['inspected_records']+=1;p=r['geometry']
     if p.geom_type!='Point' or not p.is_valid: counts['invalid']+=1;continue
     if self.source_excluded(r): counts['quarantined_source']+=1;continue
     lon,lat=TO_LL.transform(p.x,p.y);distance=GEOD.inv(q.lon,q.lat,lon,lat)[2]
     if distance>q.radius_m: continue
     access=access_policy(r['tags'],r['evidence']);counts[access['state']]+=1
     if access['reason']=='mapped_access_restriction': restricted_positions.add((round(p.x,3),round(p.y,3)))
     if access['state']!='map_supported': continue
     r.update(x=p.x,y=p.y,lon=lon,lat=lat,proximity_m=distance,access=access)
     eligible.append(r)
    # Deterministic geometric spread along paths; stable IDs break ties.
    eligible.sort(key=lambda r:(hashlib.sha256(r['source_id'].encode()).hexdigest()))
    buckets.append(eligible)
  for i in range(120):
   for bucket in buckets:
    if i>=len(bucket): continue
    r=bucket[i];key=(round(r['x'],3),round(r['y'],3))
    if key in restricted_positions: counts['coincident_restriction_veto']+=1;continue
    if key in seen:
     lineage=r['evidence'].get('source_id')
     if lineage not in seen[key]['lineage']:seen[key]['lineage'].append(lineage)
     counts['duplicate_positions']+=1;continue
    if len(seen)>=cap: continue
    r['lineage']=[r['evidence'].get('source_id')];seen[key]=r
  counts['representatives']=len(seen)
  return list(seen.values()),dict(counts)
 def endpoint(self,r):
  p=r['geometry'];x,y=p.x,p.y;bounds=(x-15,y-15,x+15,y+15)
  if self.source_excluded(r): return {'state':'excluded','reason':'quarantined_source_geometry'}
  if any(g.covers(p) for g,sid in self.quarantine_regions): return {'state':'unknown','reason':'osm_geometry_quarantine_region'}
  # A hard query cap is not proof that additional adverse evidence is absent.
  # Read one sentinel record, and retain unknown if exclusion work is truncated.
  class IncompleteEndpointQuery(Exception): pass
  def bounded(table,cap,query_bounds=bounds):
   records=self.rows(table,query_bounds,cap+1)
   if len(records)>cap: raise IncompleteEndpointQuery(table)
   return records
  try: return self._endpoint_checked(r,p,x,y,bounded)
  except IncompleteEndpointQuery as exc: return {'state':'unknown','reason':'endpoint_exclusion_query_limit','layer':str(exc)}
 def _endpoint_checked(self,r,p,x,y,bounded):
  for duplicate in bounded('candidates',64,(x-.002,y-.002,x+.002,y+.002)):
   if duplicate['geometry'].distance(p)<=.001 and access_policy(duplicate['tags'],duplicate['evidence'])['reason']=='mapped_access_restriction':
    return {'state':'excluded','reason':'coincident_mapped_access_restriction'}
  for b in bounded('buildings',100):
   if b['geometry'].covers(p): return {'state':'excluded','reason':'inside_mapped_building'}
  for w in bounded('water',100):
   if w['geometry'].covers(p): return {'state':'excluded','reason':'inside_mapped_water'}
  for space in bounded('public_spaces',100):
   if space['geometry'].covers(p):
    a=space['tags']
    if a.get('access') in {'private','no','customers','permit'}: return {'state':'excluded','reason':'restricted_containing_area'}
    if a.get('opening_hours') or a.get('access:conditional'): return {'state':'exploratory_only','reason':'containing_area_schedule_unresolved'}
  for b in bounded('barriers',60):
   if b['geometry'].distance(p)<3 and b['tags'].get('barrier') not in {'kerb','bollard','cycle_barrier'}:
    return {'state':'exploratory_only','reason':'near_mapped_barrier_access_unresolved'}
  for road in bounded('paths',100):
   if road['tags'].get('highway') in ROAD_TYPES:
    width=road['tags'].get('width');margin=3.0
    try: margin=max(3,min(15,float(width)/2))
    except (ValueError,TypeError): pass
    if road['geometry'].distance(p)<margin: return {'state':'excluded','reason':'conservative_road_carriageway_vicinity'}
  return r['access']
 def targets(self,x,y,range_m=10000,cap=20):
  """Sample an origin-centred *inventory* pool, distinct from per-ray limits.

  The caller supplies standing radius + modeled sight distance (at most20km).
  This broad phase does not certify any ray beyond the independent10km cap.
  Four near quadrants and an outer band share the same2000-row/category cap.
  """
  if not math.isfinite(range_m) or not 1<=range_m<=20000 or not 1<=cap<=20:raise ValueError('Target inventory bounds exceed prototype limits')
  groups=[];near_radius=min(10000,range_m);has_outer=range_m>near_radius
  for table,category in [('peaks_ridges','mountain'),('green_space','greenery'),('water','river'),('buildings','city')]:
   where='a.invalid_geometry=0 and a.unresolved=0 and a.height_m>=12' if table=='buildings' else ''
   # Reserve nearby reads before examining the larger pool. A distant dense
   # RTree region cannot consume all nearby feature-read or target slots.
   rows=[];seen=set()
   for outer in ((False,True) if has_outer else (False,)):
    radius=range_m if outer else near_radius
    for sx,sy in ((-1,-1),(-1,1),(1,-1),(1,1)):
     quadrant=(x-radius if sx<0 else x,y-radius if sy<0 else y,x if sx<0 else x+radius,y if sy<0 else y+radius)
     band_where=where;parameters=()
     if outer:
      # Keep boxes that could reach the outer radial band in this quadrant.
      # Exact representative distances below classify near versus outer; this
      # SQL prefilter must not turn boundary-crossing polygons into omissions.
      edge_x='r.minx' if sx<0 else 'r.maxx';edge_y='r.miny' if sy<0 else 'r.maxy'
      radial=f'(({edge_x}-?)*({edge_x}-?)+({edge_y}-?)*({edge_y}-?))>?'
      band_where=f'({where}) AND ({radial})' if where else radial
      parameters=(x,x,y,y,near_radius*near_radius)
     for r in self.rows(table,quadrant,limit=250 if has_outer else 500,where=band_where,params=parameters):
      if r['fid'] not in seen:rows.append(r);seen.add(r['fid'])
   scored=[]
   for r in rows:
    if self.source_excluded(r): continue
    g=r['geometry']
    if g.is_empty or not g.is_valid: continue
    if any(region.intersects(g) for region,sid in self.quarantine_regions): continue
    p=g.representative_point();d=p.distance(Point(x,y))
    if not 40<=d<=range_m: continue
    if category=='river' and not (r['tags'].get('water') in {'river','reservoir','lake'} or r['tags'].get('waterway') in {'river','riverbank'} or g.area>10000): continue
    if category=='greenery' and g.area<10000: continue
    # Preserve directional diversity, then meaningful area/height within strata.
    bearing=math.degrees(math.atan2(p.x-x,p.y-y))%360
    importance=min(80,max(0,r.get('height_m',0))) if category=='city' else math.sqrt(g.area) if g.area else 20
    scored.append((int(bearing//90),-importance/(1+d/(500 if category=='city' else 2000))**(1.5 if category=='city' else 1),d,r))
   nearby=[item for item in scored if item[2]<=near_radius]
   distant=[item for item in scored if item[2]>near_radius]
   per_sector={}
   for sector,imp,d,r in sorted(nearby,key=lambda s:(s[1],s[2])):
    if sector not in per_sector: per_sector[sector]=r
   chosen=list(per_sector.values())
   if distant:
    # One guaranteed outer-band opportunity per category allows a standing
    # point near the user's radius edge to inspect relevant farther targets.
    chosen.append(min(distant,key=lambda s:(s[1],s[2]))[3])
   for _,_,_,r in sorted(scored,key=lambda s:s[2]):
    if len(chosen)>=5: break
    if all(r['fid']!=c['fid'] for c in chosen): chosen.append(r)
   for r in chosen[:5]:
    r['inventory_radius_m']=range_m
    r['inventory_origin_distance_m']=r['geometry'].representative_point().distance(Point(x,y))
    r['category']=category;r['target_id']=f'{table}/{r["fid"]}';r['name']=r.get('name') or {'city':'건물 윤곽','mountain':'지도상의 봉우리·능선','greenery':'지도상의 녹지 지형','river':'지도상의 수면'}[category]
    groups.append(r)
  return groups[:cap]
 def places(self,text):
  text=text.strip()[:80]
  if len(text)<1: return []
  places=[];seen=set();deadline=time.monotonic()+.7
  for table in ('districts','landmarks','peaks_ridges','public_spaces','paths'):
   try: rows=self.rows(table,limit=25,where='a.name like ? escape \'\\\'',params=('%'+text.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%',),deadline=deadline)
   except sqlite3.OperationalError: break
   for r in rows:
    if r['name'] in seen: continue
    seen.add(r['name']);p=r['geometry'].representative_point();lon,lat=TO_LL.transform(p.x,p.y)
    places.append({'name':r['name'][:160],'lon':lon,'lat':lat,'source_id':r['source_id']})
    if len(places)>=20: return places
  return places
 def map(self,bbox,zoom,cap=700):
  x0,y0=TO_M.transform(bbox[0],bbox[1]);x1,y1=TO_M.transform(bbox[2],bbox[3]);bounds=(min(x0,x1),min(y0,y1),max(x0,x1),max(y0,y1));clip=box(*bounds)
  features=[];payload_bytes=0;tolerance=max(2,120/(2**max(0,zoom-10)))
  midpoint=((bounds[0]+bounds[2])/2,(bounds[1]+bounds[3])/2)
  quadrants=[(bounds[0],bounds[1],midpoint[0],midpoint[1]),(midpoint[0],bounds[1],bounds[2],midpoint[1]),
             (bounds[0],midpoint[1],midpoint[0],bounds[3]),(midpoint[0],midpoint[1],bounds[2],bounds[3])]
  seen=set()
  for table in ('districts','water','green_space','paths'):
   quota=30 if table=='districts' else 120 if table=='water' else 100 if table=='green_space' else max(100,cap-250)
   roadfilter="json_extract(a.tags_json,'$.highway') in ('primary','secondary','tertiary','pedestrian')" if table=='paths' and zoom<14 else ''
   order='largest_envelope' if table in {'water','green_space'} else 'major_roads' if table=='paths' else None
   # Round-robin viewport quadrants before the final quota. Whole clipped
   # geometries are emitted once, so a broad Han River polygon can serve more
   # than one quadrant without duplicate payloads or southwest-only row bias.
   buckets=[self.rows(table,quadrant,math.ceil(quota/4),where=roadfilter,order_by=order) for quadrant in quadrants]
   selected=[]
   for i in range(math.ceil(quota/4)):
    for bucket in buckets:
     if i>=len(bucket):continue
     r=bucket[i];key=(table,r['fid'])
     if key in seen:continue
     seen.add(key);selected.append(r)
     if len(selected)>=quota:break
    if len(selected)>=quota:break
   for r in selected:
    g=r['geometry']
    if not g.is_valid: continue
    g=g.intersection(clip).simplify(tolerance,preserve_topology=True)
    if g.is_empty: continue
    # Suppress oversized geometry rather than sending unlimited coordinates.
    geo=mapping(transform(TO_LL.transform,g))
    size=len(json.dumps(geo).encode())
    if size>40000: continue
    if payload_bytes+size+500>1_000_000:break
    payload_bytes+=size+500
    features.append({'type':'Feature','geometry':geo,'properties':{'layer':table,'name':(r.get('name') or '')[:160],'source_id':r['source_id']}})
  return {'type':'FeatureCollection','features':features[:cap],'sampled':True,'attribution':'© OpenStreetMap contributors · ODbL','detail':'Bounded simplified local geographic data; omitted map features are not absent ground features.'}
 def validate(self,hashes=True):
  result=[]
  for item in self.manifest['artifacts']:
   p=self.root/item['path']
   if not p.resolve().is_relative_to(self.root.resolve()): raise ValueError('Source manifest path escape')
   ok=p.is_file() and p.stat().st_size==item['bytes']
   if ok and hashes: ok=sha256(p)==item['sha256']
   result.append({'path':item['path'],'ok':ok,'sha256':item['sha256']})
  if not all(x['ok'] for x in result): raise ValueError('Package integrity failed')
  return result
 def close(self):
  for c in self.connections.values(): c.close()
