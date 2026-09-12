#!/usr/bin/env python3
"""Reproduce the small local Leaflet bundle; no tiles or GIS acquisition.

Official documentation supplies independent SHA256 for JS/CSS. The pinned
upstream LICENSE has a locally computed fingerprint, not a publisher checksum.
"""
from hidden_view_finder.prototype.runtime import budget as prototype_budget,config as prototype_config
from pathlib import Path
from contextlib import nullcontext
import argparse, base64, hashlib, json, os, urllib.request, urllib.parse
from seoul_visibility.acquisition_safety import Budget, atomic_json

ROOT = Path(__file__).resolve().parents[2]
DEST = ROOT / 'src/hidden_view_finder/static/vendor/leaflet'
ITEMS = (
 ('leaflet.js','https://unpkg.com/leaflet@1.9.4/dist/leaflet.js',160_000,'20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo='),
 ('leaflet.css','https://unpkg.com/leaflet@1.9.4/dist/leaflet.css',20_000,'p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY='),
 ('LICENSE','https://raw.githubusercontent.com/Leaflet/Leaflet/v1.9.4/LICENSE',10_000,None),
)

def main(development=False):
 b=prototype_budget(prototype_config())
 if development:
  lock=json.loads((ROOT/'.citywide-writer.lock').read_text());os.kill(lock['pid'],0)
  if not lock['label'].startswith('prototype development:') or lock['total_limit']>20_000_000_000:raise RuntimeError('Live enclosing development reservation required')
 context=nullcontext(None) if development else b.reserve(300_000,300_000,'prototype Leaflet bundle')
 records=[]
 with context as reservation:
  for name,url,cap,expected in ITEMS:
   path=b.safe_path(DEST/name)
   if path.exists():data=path.read_bytes();status='reused'
   else:
    with urllib.request.urlopen(urllib.request.Request(url,headers={'Accept-Encoding':'identity'}),timeout=30) as response:
     if response.status!=200 or urllib.parse.urlsplit(response.url).hostname not in {'unpkg.com','raw.githubusercontent.com'}:raise RuntimeError('Unexpected vendor response')
     data=response.read(cap+1)
    status='acquired'
   if len(data)>cap or b'<html' in data[:100].lower():raise RuntimeError('Invalid or oversized vendor asset')
   digest=hashlib.sha256(data).digest()
   if expected and base64.b64encode(digest).decode()!=expected:raise RuntimeError('Leaflet official integrity check failed')
   if not path.exists():
    if reservation:reservation.check_write(len(data),path)
    else:b.check(len(data),len(data),path)
    path.parent.mkdir(parents=True,exist_ok=True);part=b.safe_path(path.with_suffix(path.suffix+'.part'));part.write_bytes(data);os.replace(part,path)
   records.append({'path':str(path.relative_to(ROOT)),'url':url,'bytes':len(data),'sha256':digest.hex(),'integrity':'official documentation SHA256' if expected else 'locally computed SHA256; pinned release path','status':status})
  report={'library':'Leaflet','version':'1.9.4','documentation':'https://leafletjs.com/download.html','license':'BSD-2-Clause','assets':records,'total_bytes':sum(r['bytes'] for r in records),'external_runtime_requests':False,'css_image_usage':'Custom vector markers and built-in zoom controls; no Leaflet default marker/layer image controls used.'}
  if development:(ROOT/'reports/prototype/leaflet.json').write_text(json.dumps(report,indent=2)+'\n')
  else:atomic_json(ROOT/'reports/prototype/leaflet.json',report,b)
  print(json.dumps(report))

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--development',action='store_true');main(p.parse_args().development)
