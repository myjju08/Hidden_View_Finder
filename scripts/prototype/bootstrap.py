#!/usr/bin/env python3
"""Small pinned wheel installer. No scientific-stack installation or GIS acquisition.
Run with the existing GIS Python wrapper. --development is for a live enclosing
writer reservation only; normal setup obtains the shared budget itself.
"""
from hidden_view_finder.prototype.runtime import budget as prototype_budget,config as prototype_config
from pathlib import Path, PurePosixPath
import json,sys,hashlib,urllib.request,zipfile,os,argparse
from contextlib import nullcontext
from packaging.requirements import Requirement
from packaging.tags import sys_tags
from packaging.utils import canonicalize_name
from seoul_visibility.acquisition_safety import Budget,atomic_json
ROOT=Path(__file__).resolve().parents[2]
DEST=ROOT/'data/prototype/dependencies/site'
LOCK=ROOT/'configs/prototype-requirements.json'
CAP=100_000_000

def get(url,cap):
 parsed=urllib.parse.urlsplit(url)
 if parsed.scheme!='https' or parsed.hostname not in {'pypi.org','files.pythonhosted.org'} or parsed.username or parsed.password:raise RuntimeError('Only official HTTPS wheel metadata/distribution hosts allowed')
 with urllib.request.urlopen(url,timeout=30) as r:
  if r.status!=200 or urllib.parse.urlsplit(r.url).hostname not in {'pypi.org','files.pythonhosted.org'}: raise RuntimeError('Unapproved wheel response')
  data=r.read(cap+1)
 if len(data)>cap: raise RuntimeError('Bounded dependency response exceeds cap')
 return data

def plan():
 tags={str(t):i for i,t in enumerate(sys_tags())}; pending=[Requirement('fastapi'),Requirement('uvicorn')]; chosen={}
 while pending:
  req=pending.pop(0); name=canonicalize_name(req.name)
  if req.marker and not req.marker.evaluate(): continue
  if name in chosen:
   if chosen[name]['version'] not in req.specifier: raise RuntimeError('Dependency version conflict: '+name)
   continue
  exact=next((x.version for x in req.specifier if x.operator=='=='),None)
  print('Resolving',name,exact or 'latest',flush=True)
  meta=json.loads(get('https://pypi.org/pypi/'+name+('/'+exact if exact else '')+'/json',32_000_000)); version=meta['info']['version']
  if version not in req.specifier: raise RuntimeError('Latest dependency does not satisfy constraint: '+str(req))
  options=[]
  for item in meta['urls']:
   if not item['filename'].endswith('.whl'): continue
   parts=item['filename'][:-4].split('-'); variants=[a+'-'+b+'-'+c for a in parts[-3].split('.') for b in parts[-2].split('.') for c in parts[-1].split('.')]
   rank=min([tags[t] for t in variants if t in tags] or [10**9])
   if rank<10**9: options.append((rank,item))
  if not options: raise RuntimeError('No compatible binary wheel: '+name)
  item=min(options,key=lambda x:x[0])[1]
  chosen[name]={'name':name,'version':version,'url':item['url'],'filename':item['filename'],'bytes':item['size'],'sha256':item['digests']['sha256']}
  if sum(x['bytes'] for x in chosen.values())>20_000_000: raise RuntimeError('Wheel transfer plan exceeds20MB')
  pending.extend(Requirement(r) for r in meta['info']['requires_dist'] or [])
 return list(chosen.values())

def install(development=False):
 b=prototype_budget(prototype_config())
 context=nullcontext(None) if development else b.reserve(CAP,CAP,'prototype Python dependencies')
 with context as reservation:
  records=json.loads(LOCK.read_text()) if LOCK.exists() else plan()
  if sum(x['bytes'] for x in records)>20_000_000: raise RuntimeError('Dependency cap exceeded')
  total=0
  DEST.mkdir(parents=True,exist_ok=True)
  for item in records:
   archive=ROOT/'data/prototype/dependencies/wheels'/item['filename']; b.safe_path(archive)
   if archive.exists(): data=archive.read_bytes()
   else:
    data=get(item['url'],item['bytes'])
    if len(data)!=item['bytes'] or hashlib.sha256(data).hexdigest()!=item['sha256']: raise RuntimeError('Publisher wheel checksum failed')
    if reservation: reservation.check_write(len(data),archive)
    archive.parent.mkdir(parents=True,exist_ok=True); archive.write_bytes(data)
   if len(data)!=item['bytes'] or hashlib.sha256(data).hexdigest()!=item['sha256']: raise RuntimeError('Existing wheel is corrupt')
   with zipfile.ZipFile(archive) as z:
    for member in z.infolist():
     p=PurePosixPath(member.filename)
     if p.is_absolute() or '..' in p.parts or ((member.external_attr>>16)&0o170000)==0o120000: raise RuntimeError('Unsafe wheel member')
     total+=member.file_size
     if total>60_000_000 or member.file_size>20_000_000: raise RuntimeError('Wheel expansion bound exceeded')
     target=b.safe_path(DEST/str(p))
     if member.is_dir(): target.mkdir(parents=True,exist_ok=True); continue
     blob=z.read(member)
     if target.exists():
      if target.read_bytes()!=blob: raise RuntimeError('Preserving incompatible existing dependency: '+member.filename)
      continue
     if reservation: reservation.check_write(len(blob),target)
     target.parent.mkdir(parents=True,exist_ok=True); target.write_bytes(blob)
   print(item['name'],item['version'],flush=True)
  if not LOCK.exists(): LOCK.write_text(json.dumps(records,indent=2)+'\n')
  result={'wheel_bytes':sum(x['bytes'] for x in records),'expanded_member_bytes':total,'packages':len(records),'source':'PyPI publisher SHA256-verified wheels; no scripts executed'}
  if development: (ROOT/'reports/prototype/dependencies.json').write_text(json.dumps(result,indent=2)+'\n')
  else: atomic_json(ROOT/'reports/prototype/dependencies.json',result,b)
  print(json.dumps(result))
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--development',action='store_true');a=p.parse_args()
 if a.development:
  lock=json.loads((ROOT/'.citywide-writer.lock').read_text());os.kill(lock['pid'],0)
  if not lock['label'].startswith('prototype development:'): raise RuntimeError('Live development reservation required')
 install(a.development)
