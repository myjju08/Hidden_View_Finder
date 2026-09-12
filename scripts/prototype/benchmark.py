#!/usr/bin/env python3
"""Bounded, restartable25-district HTTP exercise from existing mapped candidates."""
from pathlib import Path
import argparse,json,time,sys,hashlib,statistics,urllib.request,urllib.error
from hidden_view_finder.prototype.runtime import ROOT,config,budget
from hidden_view_finder.prototype.data import Data,TO_LL
from hidden_view_finder.prototype.models import Query
from seoul_visibility.acquisition_safety import atomic_json

def run(url,development=False):
 c=config();d=Data(c['package_manifest']);output=ROOT/'reports/prototype/citywide-queries.json';records=[]
 signature=hashlib.sha256(b''.join((ROOT/p).read_bytes() for p in ['src/hidden_view_finder/prototype/scenes.py','src/hidden_view_finder/prototype/data.py','src/hidden_view_finder/prototype/tiles.py'])).hexdigest()
 if output.exists():
  previous=json.loads(output.read_text())
  if previous.get('implementation_sha256')==signature:records=[r for r in previous['queries'] if r['status']!='http_error']
 def save():
  value={'schema_version':1,'implementation_sha256':signature,'source_package':d.version,'queries':records,'district_count':len({r['origin_district'] for r in records}),'field_verified':False,'network_scope':'localhost APIs only; no acquisition/provider calls','global_readiness_changed':False}
  if development:output.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
  else:atomic_json(output,value,budget(c))
 previous_start=0
 for district in d.districts:
  name=district['name']
  if any(r['origin_district']==name for r in records):continue
  pt=district['geometry'].representative_point();lon,lat=TO_LL.transform(pt.x,pt.y)
  q=Query.parse({'origin':{'lon':lon,'lat':lat},'radius_m':3000,'view_at':'2026-09-11T18:00:00+09:00','preferences':['city','mountain','greenery']})
  candidates,_=d.candidates(q,160,time.monotonic()+2)
  inside=[r for r in candidates if district['geometry'].covers(r['geometry'])]
  origin=min(inside,key=lambda r:r['geometry'].distance(pt)) if inside else None
  payload={'origin':{'lon':origin['lon'] if origin else lon,'lat':origin['lat'] if origin else lat},'radius_m':3000,'view_at':'2026-09-11T18:00:00+09:00','preferences':['city','mountain','greenery'],'composition':'any','crowd_preference':'any','limit':3}
  delay=max(0,6.2-(time.monotonic()-previous_start))
  if delay:time.sleep(delay)
  start=time.monotonic();previous_start=start
  request=urllib.request.Request(url+'/api/recommendations',json.dumps(payload).encode(),{'Content-Type':'application/json'})
  try:
   with urllib.request.urlopen(request,timeout=25) as response:result=json.loads(response.read(2_000_001))
   record={'origin_district':name,'origin_source_id':origin['source_id'] if origin else None,'origin':payload['origin'],'view_at':payload['view_at'],'status':result['status'],'http_seconds':round(time.monotonic()-start,4),'search':result['search'],'views':[{'view_id':v['view_id'],'standing':{k:v['standing'][k] for k in ['lon','lat','district','source_ids']},'supported_categories':v['supported_categories'],'coverage':v['coverage'],'score':v['score'],'solar':v['solar']} for v in result['views']]}
  except urllib.error.HTTPError as e:
   record={'origin_district':name,'origin_source_id':origin['source_id'] if origin else None,'origin':payload['origin'],'status':'http_error','http_status':e.code,'http_seconds':round(time.monotonic()-start,4)}
   if e.code==429:print('Rate-limited; checkpoint saved. Resume after60seconds.',flush=True);save();return
  records.append(record);save();print(name,record['status'],len(record.get('views',[])),round(time.monotonic()-start,2),flush=True)
 d.close()
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--url',default='http://127.0.0.1:8000');p.add_argument('--development',action='store_true');a=p.parse_args()
 if a.url not in {'http://127.0.0.1:8000','http://localhost:8000'}:raise ValueError('Benchmark only accepts owned localhost server')
 if a.development:
  lock=json.loads((ROOT/'.citywide-writer.lock').read_text());__import__('os').kill(lock['pid'],0)
  if not lock['label'].startswith('prototype development:'):raise RuntimeError('Enclosing development reservation required')
 run(a.url,a.development)
