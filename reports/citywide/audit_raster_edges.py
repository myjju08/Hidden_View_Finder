import os,resource,json,time,math
from pathlib import Path
os.environ.update(OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='1',GDAL_NUM_THREADS='1',GDAL_PAM_ENABLED='NO')
resource.setrlimit(resource.RLIMIT_AS,(512*1024**2,512*1024**2));os.sched_setaffinity(0,{min(os.sched_getaffinity(0))})
import numpy as np
from osgeo import gdal
gdal.UseExceptions();gdal.SetCacheMax(16*1024**2)
root=Path('data/citywide/prepared/67cd2654e14de3c0/tiles')
snapshot_ids=sorted(path.parent.name for path in root.glob('*/tile.json'))
assert len(snapshot_ids)==109, 'This report reproduces the completed109-tile package'
start=time.monotonic();deadline=start+120
tiles={}
for name in snapshot_ids:
 record=json.loads((root/name/'tile.json').read_text());bounds=record['grid']['bounds'];tiles[(round(bounds[0]/5000),round(bounds[1]/5000))]=(name,record)
keys=('same_coordinate_comparisons','both_valid','both_nodata','validity_mismatch','both_nodata_inside_seoul','both_nodata_outside_seoul','finite_nonzero','finite_gt_0_01m','finite_gt_0_1m','finite_gt_1m')
totals={(kind,limit):{k:0 for k in keys} for kind in ('edge','corner') for limit in (5,20)}
values={(kind,limit):[] for kind,limit in totals};pair_counts={'edge':0,'corner':0};band_anomalies=[];broad_anomalies=[];alignment=0
for key in sorted(tiles):
 for step in ((1,0),(0,1),(1,1),(-1,1)):
  other=(key[0]+step[0],key[1]+step[1])
  if other not in tiles:continue
  kind='edge' if 0 in step else 'corner';pair_counts[kind]+=1
  lb=tiles[key][1]['grid']['bounds'];rb=tiles[other][1]['grid']['bounds']
  if kind=='edge':
   axis=0 if step[0] else 1;edge_coordinate=max(lb[axis],rb[axis]);feature={'kind':'vertical shared edge' if axis==0 else 'horizontal shared edge','coordinate':edge_coordinate}
  else:
   corner=[max(lb[0],rb[0]),max(lb[1],rb[1])];feature={'kind':'corner-only contact','coordinate_epsg5186':corner}
  for aa,bb in ((key,other),(other,key)):
   an,ar=tiles[aa];bn,br=tiles[bb];a=gdal.Open(str(root/an/'dtm.tif'));b=gdal.Open(str(root/bn/br['terrain_halo']['path']));c=gdal.Open(str(root/an/'contract_mask.tif'))
   ag=a.GetGeoTransform();bg=b.GetGeoTransform();ab=ar['grid']['bounds'];bbounds=br['terrain_halo_grid']['bounds']
   assert list(ag)==ar['grid']['transform'] and list(bg)==br['terrain_halo_grid']['transform'] and a.GetSpatialRef().IsSame(b.GetSpatialRef())
   xmin,xmax=max(ab[0],bbounds[0]),min(ab[2],bbounds[2]);ymin,ymax=max(ab[1],bbounds[1]),min(ab[3],bbounds[3])
   offsets=[(xmin-ag[0])/5,(ag[3]-ymax)/5,(xmin-bg[0])/5,(bg[3]-ymax)/5,(xmax-xmin)/5,(ymax-ymin)/5];assert all(x==int(x) for x in offsets)
   ax,ay,bx,by,width,height=map(int,offsets);alignment+=1
   direction_totals={limit:{k:0 for k in keys} for limit in (5,20)};direction_max={5:0.,20:0.}
   for yy in range(0,height,256):
    for xx in range(0,width,512):
     if time.monotonic()>deadline:raise TimeoutError('120-second read-only edge audit cap')
     w,h=min(512,width-xx),min(256,height-yy);av=a.GetRasterBand(1).ReadAsArray(ax+xx,ay+yy,w,h);bv=b.GetRasterBand(1).ReadAsArray(bx+xx,by+yy,w,h)
     am=(a.GetRasterBand(1).GetMaskBand().ReadAsArray(ax+xx,ay+yy,w,h)!=0)&np.isfinite(av);bm=(b.GetRasterBand(1).GetMaskBand().ReadAsArray(bx+xx,by+yy,w,h)!=0)&np.isfinite(bv);mask=c.GetRasterBand(1).ReadAsArray(ax+xx,ay+yy,w,h)
     both=am&bm;neither=~am&~bm;diff=np.abs(av.astype(np.float64)-bv.astype(np.float64))
     xc=xmin+(xx+np.arange(w)+.5)*5;yc=ymax-(yy+np.arange(h)+.5)*5
     if kind=='edge':
      distance=np.broadcast_to(np.abs(xc-edge_coordinate)[None,:] if axis==0 else np.abs(yc-edge_coordinate)[:,None],av.shape)
     else:distance=np.hypot(xc[None,:]-corner[0],yc[:,None]-corner[1])
     for ry,rx in np.argwhere((both&(diff!=0))|(am!=bm)):
      if len(broad_anomalies)>=50000:raise MemoryError('Bounded anomaly record cap50000 reached; no truncation claimed')
      broad_anomalies.append({'a_tile':an,'b_halo_tile':bn,'adjacency_kind':kind,'shared_boundary':feature,
       'coordinate_epsg5186':[float(xc[rx]),float(yc[ry])],'distance_to_shared_edge_or_corner_m':float(distance[ry,rx]),
       'a_elevation_m':float(av[ry,rx]) if am[ry,rx] else None,'b_elevation_m':float(bv[ry,rx]) if bm[ry,rx] else None,
       'difference_m':float(diff[ry,rx]) if both[ry,rx] else None,'inside_seoul':bool(mask[ry,rx]&2)})
     for limit in (5,20):
      selected=distance<=limit;valid=selected&both;nodata=selected&neither;dv=diff[valid];d=direction_totals[limit]
      d['same_coordinate_comparisons']+=int(np.count_nonzero(selected));d['both_valid']+=len(dv);d['both_nodata']+=int(np.count_nonzero(nodata));d['validity_mismatch']+=int(np.count_nonzero(selected&(am!=bm)))
      d['both_nodata_inside_seoul']+=int(np.count_nonzero(nodata&((mask&2)!=0)));d['both_nodata_outside_seoul']+=int(np.count_nonzero(nodata&((mask&2)==0)))
      for threshold,label in ((0,'finite_nonzero'),(.01,'finite_gt_0_01m'),(.1,'finite_gt_0_1m'),(1,'finite_gt_1m')):d[label]+=int(np.count_nonzero(dv>threshold))
      if len(dv):values[(kind,limit)].append(dv);direction_max[limit]=max(direction_max[limit],float(dv.max()))
   for limit in (5,20):
    for k in keys:totals[(kind,limit)][k]+=direction_totals[limit][k]
    if direction_totals[limit]['finite_nonzero'] or direction_totals[limit]['validity_mismatch']:band_anomalies.append({'a_tile':an,'b_halo_tile':bn,'adjacency_kind':kind,'shared_boundary':feature,'distance_limit_m':limit,'counts':direction_totals[limit],'max_difference_m':direction_max[limit]})
   a=b=c=None
bands={}
for (kind,limit),counts in totals.items():
 data=np.concatenate(values[(kind,limit)]) if values[(kind,limit)] else np.empty(0)
 bands[kind+'_within_'+str(limit)+'m']={'counts':counts,'max_abs_difference_m':float(data.max()) if len(data) else None,'exact_quantiles_m':{str(q):float(v) for q,v in zip((.5,.9,.95,.99),np.quantile(data,(.5,.9,.95,.99)))} if len(data) else None}
import zlib,base64
all_anomalies_payload=base64.b64encode(zlib.compress(json.dumps(broad_anomalies,separators=(',',':')).encode(),9)).decode()
significant_anomalies=[a for a in broad_anomalies if a['difference_m'] is None or a['difference_m']>1]
print(json.dumps({'snapshot_completed_tiles':len(tiles),'pairs':pair_counts,'alignment_checks':alignment,'bands':bands,'band_anomalies':band_anomalies,'broad_halo_gt_1m_or_nodata_anomalies':significant_anomalies,'all_nonzero_or_nodata_anomaly_count':len(broad_anomalies),'all_anomalies_zlib_base64':all_anomalies_payload,
 'method':'Same-coordinate overlap comparisons; 5m means one row/column of 5m-cell centres at2.5m from a shared edge;20m means four. Edge directions are reported separately. Corner-only pairs use Euclidean distance to their shared point and are never counted as shared-edge seams.',
 'counts_caveat':'Directions/pairs may repeat geographic coordinates; no different-coordinate adjacent values subtracted.',
 'elapsed_seconds':time.monotonic()-start,'observed_peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,'rlimit_address_space_bytes':512*1024**2,'cpu_affinity_count':1,'no_writes':True},sort_keys=True))
