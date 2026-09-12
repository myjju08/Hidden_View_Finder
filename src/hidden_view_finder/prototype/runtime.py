"""Shared immutable storage policy and read-only source configuration."""
from __future__ import annotations
from pathlib import Path
import json,os,stat
from seoul_visibility.acquisition_safety import Budget
from seoul_visibility.errors import ResourceBudgetError
ROOT=Path(__file__).resolve().parents[3]
CAPS={'task_additions_bytes':4_000_000_000,'image_cache_bytes':250_000_000,'query_cache_bytes':256_000_000,'map_derivatives_bytes':250_000_000,'artifacts_bytes':100_000_000}

def config(path=None):
 c=json.loads(Path(path or ROOT/'configs/prototype.json').read_text())
 for key,maximum in CAPS.items():
  v=c['storage'][key]
  if type(v) is not int or not 0<=v<=maximum: raise ResourceBudgetError('Invalid prototype sub-budget '+key)
 ai_cap=c['ai'].get('image_cache_bytes',c['storage']['image_cache_bytes'])
 if type(ai_cap) is not int or not 0<=ai_cap<=CAPS['image_cache_bytes']:raise ResourceBudgetError('Invalid AI image cache sub-budget')
 c['ai']['image_cache_bytes']=min(ai_cap,c['storage']['image_cache_bytes'])
 for key in ('package_manifest','tile_manifest','runtime_root'):
  p=Path(c[key]);resolved=(ROOT/p).resolve()
  if p.is_absolute() or not resolved.is_relative_to(ROOT): raise ValueError('Configuration path escapes checkout')
  c[key]=str(resolved)
 if c['limits']['geometry_workers']!=1: raise ValueError('One geometry worker required')
 for key,cap in {'candidate_representatives':500,'target_groups':20,'sparse_rays':10000,'traversed_cells':2000000,'refined_views':20,'maximum_sight_distance_m':10000,'map_features':1000,'memory_scene_entries':100}.items():
  if not 1<=c['limits'][key]<=cap: raise ValueError('Work cap rejected: '+key)
 if not .2<=c['limits']['geometry_seconds']<=10: raise ValueError('Geometry deadline must be .2–10 seconds')
 budget(c)
 return c

def budget(c):
 s=c['storage']
 # Never accept environment overrides of the mandatory limits.
 for key in ('HVF_TOTAL_STORAGE_BYTES','HARD_TOTAL_STORAGE_BYTES'):
  if key in os.environ and int(os.environ[key])>20_000_000_000: raise ResourceBudgetError('Environment storage ceiling exceeds20decimalGB')
 b=Budget(ROOT,stage_root=ROOT/'data/citywide/staging',limit=s['total_bytes'],min_free=s['minimum_free_bytes'],stage_limit=s['temporary_bytes'],additional_accounted_bytes=s['additional_accounted_bytes'])
 startup=ROOT/'reports/prototype/startup.json'
 if startup.exists():
  baseline=json.loads(startup.read_text())['snapshot']['accounted_bytes']
  b.limit=min(b.limit,baseline+s['task_additions_bytes'])
 return b

def public_storage(c):
 b=budget(c);s=b.check();startup=ROOT/'reports/prototype/startup.json'
 baseline=json.loads(startup.read_text())['snapshot']['accounted_bytes'] if startup.exists() else s['accounted_bytes']
 growth=max(0,s['accounted_bytes']-baseline)
 if growth>c['storage']['task_additions_bytes']: raise ResourceBudgetError('Prototype additions ceiling exceeded')
 return {'status':'available','accounted_bytes':s['accounted_bytes'],'total_limit_bytes':20_000_000_000,'effective_limit_bytes':b.limit,'free_bytes':min(x['free_bytes'] for x in s['filesystems']),'minimum_free_bytes':b.min_free,'temporary_bytes':s['temporary_bytes'],'task_growth_bytes':growth,'os_quota_verified':False}

def artifact_usage(c):
 """One inode-aware cap for owned prototype logs, reports and browser artifacts.

 Profiles and dedicated artifact directories count in full. In regression/check
 fixture roots only logs, JUnit XML, screenshots, named reports and trace ZIPs
 count here; pytest's per-case test_* directories, synthetic GIS, source
 archives and ordinary fixture JSON remain in
 the shared total/staging budget but are not screenshots/logs/traces. No files
 are changed or deleted. Callers must reserve incremental peak separately and
 compare against this aggregate before and during writes.
 """
 limit=c['storage']['artifacts_bytes']
 if type(limit) is not int or not 0<=limit<=CAPS['artifacts_bytes']:raise ResourceBudgetError('Invalid combined artifact ceiling')
 b=budget(c);runtime=b.safe_path(Path(c['runtime_root']));stage=b.safe_path(ROOT/'data/citywide/staging')
 seen=set();charged=logical=allocated=files=0;categories={}
 def add(path,category,inert_profile_lock=False):
  nonlocal charged,logical,allocated,files
  if inert_profile_lock:
   if category!='browser_profiles' or path.name!='lock':raise ResourceBudgetError('Only browser profile lock links are inert metadata')
   b.safe_path(path.parent)
  else:path=b.safe_path(path)
  try:info=path.lstat() if inert_profile_lock else path.stat()
  except FileNotFoundError:return
  identity=(info.st_dev,info.st_ino)
  if identity in seen:return
  seen.add(identity)
  size=info.st_size;blocks=info.st_blocks*512;cost=max(size,blocks)
  charged+=cost;logical+=size;allocated+=blocks
  if stat.S_ISREG(info.st_mode) or inert_profile_lock and stat.S_ISLNK(info.st_mode):files+=1
  categories[category]=categories.get(category,0)+cost
 def selected(path):
  name=path.name.lower();suffix=path.suffix.lower()
  return (suffix in {'.log','.xml','.png','.jpg','.jpeg','.webp','.har'}
          or suffix in {'.json','.md'} and any(word in name for word in ('report','summary','validation','benchmark','smoke','inspection'))
          or suffix=='.zip' and 'trace' in name)
 def scan(root,category,all_files=False):
  root=b.safe_path(root)
  if not root.exists():return
  if root.is_file():
   if all_files or selected(root):add(root,category)
   return
  add(root,category)
  for path in root.rglob('*'):
   if not all_files and any(part.startswith('test_') for part in path.relative_to(root).parts[:-1]):continue
   # Do not follow symlinks, even internal aliases. Selected output symlinks
   # cannot become a way to omit or spend storage outside declared roots.
   if path.is_symlink():
    # Firefox's per-profile `lock` is an inert PID/host marker, not a
    # filesystem output redirect. Charge the link inode/target-string bytes
    # without resolving or opening its target. Profile roots and every other
    # output symlink still use the normal escape rejection.
    if category=='browser_profiles' and path.name=='lock':
     add(path,category,inert_profile_lock=True)
     for parent in path.parents:
      if parent==root or not parent.is_relative_to(root):break
      add(parent,category)
    elif all_files or selected(path):b.safe_path(path)
    continue
   if path.is_file() and (all_files or selected(path)):
    add(path,category)
    for parent in path.parents:
     if parent==root:break
     if not parent.is_relative_to(root):break
     add(parent,category)
 for base in dict.fromkeys((runtime,b.safe_path(ROOT/'data/prototype'))):
  for name in ('test-artifacts','staging/artifacts','browser-artifacts'):
   scan(base/name,'runtime_artifacts',True)
 scan(stage/'prototype-browser/runtime','browser_profiles',True)
 if stage.exists():
  for path in stage.glob('prototype*'):
   if path.is_file():scan(path,'staging_reports')
   elif path.is_dir():scan(path,'staging_test_artifacts')
 scan(ROOT/'reports/prototype','aggregate_reports',True)
 return {'used_bytes':charged,'logical_bytes':logical,'allocated_bytes':allocated,
         'file_count':files,'limit_bytes':limit,'headroom_bytes':max(0,limit-charged),
         'over_limit':charged>limit,'categories':categories}
