#!/usr/bin/env python3
"""Inspect/start/validate existing prototype inputs. Never acquires geographic data."""
import argparse,json,sys,time,resource
from pathlib import Path
from hidden_view_finder.prototype.runtime import ROOT,config,budget,public_storage
from hidden_view_finder.prototype.data import Data
from seoul_visibility.acquisition_safety import atomic_json

def inspect(c):
 d=Data(c['package_manifest'])
 try:
  artifacts=d.validate(hashes=True)
  counts={}
  for layer in ['candidates','buildings','districts','water','green_space','peaks_ridges','paths','landmarks']:
   conn=d.connection(layer);counts[layer]=conn.execute('select count(*) from '+layer).fetchone()[0]
  return {'package_id':d.version,'artifact_validation':artifacts,'counts':counts,'readiness_preserved':d.manifest['readiness'],'storage':public_storage(c),'policy':{'candidate_deduplication':'same projected coordinates within1mm; source lineage preserved within sampled shortlist','standing':'dedicated mapped pedestrian use, restrictions override; actual endpoint checked; no route prerequisite','ray':'closed-column supercover; every sample including seams required; unknown takes precedence','unresolved_relations':'source-derived valid independent objects only; unresolved public-space geometry is never invented','publication':'licence review of actually published layers remains required'},'memory':{'available_cpu_affinity':len(__import__('os').sched_getaffinity(0)),'gdal_workers':1,'gdal_cache_bytes':67108864,'per_adapter_blocks_bytes':33554432},'input_mutations':0}
 finally:d.close()

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('command',choices=['inspect','serve','validate','status']);p.add_argument('--config');p.add_argument('--port',type=int,default=8000);p.add_argument('--report',action='store_true');p.add_argument('--development',action='store_true',help=argparse.SUPPRESS);a=p.parse_args();c=config(a.config)
 resource.setrlimit(resource.RLIMIT_CORE,(0,0))
 if a.command=='serve':
  from hidden_view_finder.prototype.api import create_app
  from hidden_view_finder.prototype.http import HeaderTimeoutH11Protocol
  import uvicorn
  print(f'Hidden View Finder: http://127.0.0.1:{a.port} · local-only · AI default disabled',flush=True)
  uvicorn.run(create_app(a.config),host='127.0.0.1',port=a.port,workers=1,access_log=False,log_level='warning',limit_concurrency=16,timeout_keep_alive=5,proxy_headers=False,http=HeaderTimeoutH11Protocol)
  return
 result=public_storage(c) if a.command=='status' else inspect(c)
 if a.command=='validate':
  from scripts.data.package import validate_package
  result['existing_offline_package_validator']=validate_package(Path(c['package_manifest']).parent)
  from hidden_view_finder.prototype.tiles import Tiles,inspect_uncertainty
  issues=inspect_uncertainty(Path(c['package_manifest']).parent/'buildings.gpkg',ROOT/'reports/citywide/raster-overlap-anomalies.json')
  with Tiles(c['tile_manifest'],issues['regions'],overlap_anomalies_path=ROOT/'reports/citywide/raster-overlap-anomalies.json') as t:result['tile_adapter']=t.metadata
 if a.report:
  target=ROOT/'reports/prototype/input-inspection.json'
  if a.development:
   lock=json.loads((ROOT/'.citywide-writer.lock').read_text());__import__('os').kill(lock['pid'],0)
   if not lock['label'].startswith('prototype development:'):raise RuntimeError('Expected enclosing reservation')
   target.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
  else:atomic_json(target,result,budget(c))
 print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
