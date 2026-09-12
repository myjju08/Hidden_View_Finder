import os, resource, time, json, hashlib, math
from pathlib import Path
os.environ.update(OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1', GDAL_NUM_THREADS='1', GDAL_PAM_ENABLED='NO', PROJ_NETWORK='OFF')
resource.setrlimit(resource.RLIMIT_AS,(512*1024**2,512*1024**2))
os.sched_setaffinity(0,{min(os.sched_getaffinity(0))})
import numpy as np
from osgeo import gdal,osr
gdal.UseExceptions();gdal.SetCacheMax(16*1024**2)
start=time.monotonic();deadline=start+180
root=Path('data/citywide/prepared/67cd2654e14de3c0').resolve()
plan=json.loads((root/'plan.json').read_text());recipe=plan['recipe_sha256']
tiles={}
for path in sorted((root/'tiles').glob('*/tile.json')):
 record=json.loads(path.read_text())
 assert record['recipe_sha256']==recipe
 grid=record['grid'];key=(round(grid['bounds'][0]/5000),round(grid['bounds'][1]/5000))
 tiles[key]=(path.parent,record)
assert len(tiles)==109, 'This report reproduces the completed109-tile package'
pairs=[]
for key in sorted(tiles):
 for step in ((1,0),(0,1),(1,1),(-1,1)):
  other=(key[0]+step[0],key[1]+step[1])
  if other in tiles:pairs.append((key,other,'edge' if 0 in step else 'corner'))
checked={}
def checked_open(path,expected):
 if path.is_symlink() or not path.resolve().is_relative_to(root):raise ValueError('Unsafe raster path')
 if path not in checked:
  digest=hashlib.sha256()
  with path.open('rb') as stream:
   while chunk:=stream.read(1024*1024):digest.update(chunk)
  if digest.hexdigest()!=expected['sha256'] or path.stat().st_size!=expected['bytes']:raise ValueError('Raster fingerprint changed')
  checked[path]=digest.hexdigest()
 return gdal.Open(str(path),gdal.GA_ReadOnly)
total={k:0 for k in ('same_coordinate_comparisons','both_valid','both_nodata','validity_mismatch','a_valid_b_nodata','a_nodata_b_valid','both_nodata_outside_seoul','both_nodata_inside_seoul','finite_nonzero','finite_gt_0_01m','finite_gt_0_1m','finite_gt_1m','finite_gt_5m','finite_gt_10m')}
sample_values=np.empty(0,dtype=np.float64);sample_keys=np.empty(0,dtype=np.float64)
rng=np.random.default_rng(518620260911);sample_cap=262144
rows=[];maximum=0.;maximum_record=None;alignment_checks=0;mask_errors=0
def add_sample(values):
 global sample_values,sample_keys
 if not len(values):return
 keys=rng.random(len(values))
 sample_values=np.concatenate((sample_values,values));sample_keys=np.concatenate((sample_keys,keys))
 if len(sample_keys)>sample_cap:
  keep=np.argpartition(sample_keys,sample_cap-1)[:sample_cap]
  sample_values=sample_values[keep];sample_keys=sample_keys[keep]
for left,right,kind in pairs:
 stats={k:0 for k in total};difference_chunks=[];worst=None;pairmax=0.
 for aa,bb in ((left,right),(right,left)):
  ap,ar=tiles[aa];bp,br=tiles[bb]
  a=checked_open(ap/ar['products']['dtm']['path'],ar['products']['dtm'])
  b=checked_open(bp/br['terrain_halo']['path'],br['terrain_halo'])
  quality=checked_open(ap/ar['products']['terrain_quality']['path'],ar['products']['terrain_quality'])
  contract=checked_open(ap/ar['products']['contract_mask']['path'],ar['products']['contract_mask'])
  ag=a.GetGeoTransform();bg=b.GetGeoTransform()
  assert list(ag)==ar['grid']['transform'] and list(bg)==br['terrain_halo_grid']['transform']
  assert ag[1]==bg[1]==5 and ag[5]==bg[5]==-5 and ag[2]==ag[4]==bg[2]==bg[4]==0
  assert a.GetSpatialRef().IsSame(b.GetSpatialRef())
  assert quality.GetGeoTransform()==ag and contract.GetGeoTransform()==ag
  ax0,ay0,ax1,ay1=ar['grid']['bounds'];bx0,by0,bx1,by1=br['terrain_halo_grid']['bounds']
  xmin,xmax=max(ax0,bx0),min(ax1,bx1);ymin,ymax=max(ay0,by0),min(ay1,by1)
  assert xmin<xmax and ymin<ymax
  offsets=[(xmin-ag[0])/5,(ag[3]-ymax)/5,(xmin-bg[0])/5,(bg[3]-ymax)/5,(xmax-xmin)/5,(ymax-ymin)/5]
  assert all(value==int(value) for value in offsets)
  ax,ay,bx,by,width,height=map(int,offsets);alignment_checks+=1
  for yy in range(0,height,256):
   for xx in range(0,width,512):
    if time.monotonic()>deadline:raise TimeoutError('Bounded overlap audit exceeded180seconds')
    ww,hh=min(512,width-xx),min(256,height-yy)
    av=a.GetRasterBand(1).ReadAsArray(ax+xx,ay+yy,ww,hh);bv=b.GetRasterBand(1).ReadAsArray(bx+xx,by+yy,ww,hh)
    am=(a.GetRasterBand(1).GetMaskBand().ReadAsArray(ax+xx,ay+yy,ww,hh)!=0)&np.isfinite(av)
    bm=(b.GetRasterBand(1).GetMaskBand().ReadAsArray(bx+xx,by+yy,ww,hh)!=0)&np.isfinite(bv)
    q=quality.GetRasterBand(1).ReadAsArray(ax+xx,ay+yy,ww,hh)!=0
    mask=contract.GetRasterBand(1).ReadAsArray(ax+xx,ay+yy,ww,hh)
    mask_errors+=int(np.count_nonzero(q!=am))
    both=am&bm;neither=~am&~bm
    stats['same_coordinate_comparisons']+=av.size
    stats['both_valid']+=int(np.count_nonzero(both));stats['both_nodata']+=int(np.count_nonzero(neither))
    stats['validity_mismatch']+=int(np.count_nonzero(am!=bm))
    stats['a_valid_b_nodata']+=int(np.count_nonzero(am&~bm));stats['a_nodata_b_valid']+=int(np.count_nonzero(~am&bm))
    stats['both_nodata_outside_seoul']+=int(np.count_nonzero(neither&((mask&2)==0)))
    stats['both_nodata_inside_seoul']+=int(np.count_nonzero(neither&((mask&2)!=0)))
    diffs=np.abs(av[both].astype(np.float64)-bv[both].astype(np.float64))
    if len(diffs):
     pairvalue=float(diffs.max())
     if pairvalue>pairmax:
      index=int(np.argmax(diffs));cy,cx=np.argwhere(both)[index]
      pairmax=pairvalue;worst={'a_tile':ar['grid']['id'],'b_halo_tile':br['grid']['id'],
       'coordinate_epsg5186':[xmin+(xx+int(cx)+.5)*5,ymax-(yy+int(cy)+.5)*5],
       'a_elevation_m':float(av[cy,cx]),'b_elevation_m':float(bv[cy,cx])}
     difference_chunks.append(diffs);add_sample(diffs)
     stats['finite_nonzero']+=int(np.count_nonzero(diffs))
     for threshold,key in ((.01,'finite_gt_0_01m'),(.1,'finite_gt_0_1m'),(1,'finite_gt_1m'),(5,'finite_gt_5m'),(10,'finite_gt_10m')):
      stats[key]+=int(np.count_nonzero(diffs>threshold))
  a=b=quality=contract=None
 if difference_chunks:
  differences=np.concatenate(difference_chunks)
  quantiles={str(q):float(v) for q,v in zip((.5,.9,.95,.99),np.quantile(differences,(.5,.9,.95,.99)))}
 else:quantiles=None
 if pairmax>maximum:maximum,maximum_record=pairmax,worst
 rows.append({'tiles':[tiles[left][1]['grid']['id'],tiles[right][1]['grid']['id']],'kind':kind,**stats,'max_abs_difference_m':pairmax,'exact_pair_quantiles_m':quantiles,'maximum_location':worst})
 for key in total:total[key]+=stats[key]
summary={'mode':'read-only same-coordinate DTM versus adjacent retained halo; edge and corner neighbors; both directions',
 'recipe_sha256':recipe,'snapshot_completed_tiles':len(tiles),'available_pairs':len(pairs),
 'edge_pairs':sum(x[2]=='edge' for x in pairs),'corner_pairs':sum(x[2]=='corner' for x in pairs),
 'completed_pairs':len(rows),'raster_fingerprints_verified':len(checked),'exact_grid_alignment_checks':alignment_checks,
 'terrain_quality_mask_disagreements':mask_errors,'counts':total,'max_abs_difference_m':maximum,'maximum_location':maximum_record,
 'aggregate_quantiles_m':{str(q):float(v) for q,v in zip((.5,.9,.95,.99),np.quantile(sample_values,(.5,.9,.95,.99)))} if len(sample_values) else None,
 'aggregate_quantile_method':'Uniform priority reservoir of up to262144 jointly valid comparisons, deterministic seed518620260911; approximate, pair quantiles exact',
 'aggregate_quantile_sample_count':len(sample_values),'maximum_read_window_pixels':[512,256],
 'rlimit_address_space_bytes':512*1024**2,'gdal_cache_bytes':16*1024**2,'cpu_affinity_count':len(os.sched_getaffinity(0)),
 'elapsed_seconds':time.monotonic()-start,'observed_peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
 'largest_difference_pairs':sorted(rows,key=lambda v:v['max_abs_difference_m'],reverse=True)[:12],
 'largest_nodata_disagreement_pairs':sorted(rows,key=lambda v:v['validity_mismatch'],reverse=True)[:8],
 'completed_tile_ids':[record['grid']['id'] for _,record in tiles.values()],
 'limitations':['All109 completed tiles included; audit checks internal overlap consistency only.',
 'Comparisons reuse overlapping coordinates across pairs and directions; counts are not unique geographic cells.',
 'Only jointly finite valid cells enter difference quantiles; NoData disagreements remain separate.',
 'Internal overlap consistency is not source/vertical-datum correctness or field accuracy.',
 'No different-coordinate neighboring cells were subtracted; no data/code files were written.']}
print(json.dumps(summary,sort_keys=True))
