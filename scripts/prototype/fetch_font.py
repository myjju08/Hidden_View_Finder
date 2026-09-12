#!/usr/bin/env python3
"""Acquire the complete Korean variable font in place, without a duplicate copy.

The pinned Git blob identity came from Google Fonts' official GitHub Contents
API. It is checked separately from the local SHA256 fingerprint. SIL OFL1.1.
"""
from hidden_view_finder.prototype.runtime import budget as prototype_budget,config as prototype_config
from pathlib import Path
from contextlib import nullcontext
import argparse,hashlib,json,os,urllib.request,urllib.parse
from seoul_visibility.acquisition_safety import Budget,atomic_json
ROOT=Path(__file__).resolve().parents[2]
DEST=ROOT/'src/hidden_view_finder/static/vendor/fonts'
FONT_URL='https://raw.githubusercontent.com/google/fonts/main/ofl/notosanskr/NotoSansKR%5Bwght%5D.ttf'
FONT_BYTES=10414588
FONT_BLOB='b386890ba945e1f39448a6b59f20c5d194f58808'

def main(development=False):
 b=prototype_budget(prototype_config())
 if development:
  lock=json.loads((ROOT/'.citywide-writer.lock').read_text());os.kill(lock['pid'],0)
  if not lock['label'].startswith('prototype development:'):raise RuntimeError('Live enclosing development reservation required')
 context=nullcontext(None) if development else b.reserve(12_000_000,12_000_000,'prototype Korean font')
 with context as reservation:
  records=[]
  for name,url,cap in [('NotoSansKR.ttf',FONT_URL,FONT_BYTES),('OFL.txt','https://raw.githubusercontent.com/google/fonts/main/ofl/notosanskr/OFL.txt',10_000)]:
   target=b.safe_path(DEST/name);part=b.safe_path(target.with_suffix(target.suffix+'.part'))
   if target.exists():data=target.read_bytes();status='reused'
   else:
    with urllib.request.urlopen(url,timeout=30) as response:
     if response.status!=200 or urllib.parse.urlsplit(response.url).hostname!='raw.githubusercontent.com':raise RuntimeError('Unapproved font response')
     data=response.read(cap+1)
    status='acquired'
   if len(data)>cap:raise RuntimeError('Font artifact cap')
   if name.endswith('.ttf'):
    if len(data)!=FONT_BYTES or data[:4]!=b'\x00\x01\x00\x00':raise RuntimeError('Invalid font format/size')
    if hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()!=FONT_BLOB:raise RuntimeError('Google Fonts publisher Git blob identity mismatch')
   elif b'SIL OPEN FONT LICENSE' not in data:raise RuntimeError('Unexpected font licence')
   if not target.exists():
    if part.exists():raise RuntimeError('Preserved interrupted font write: '+str(part.relative_to(ROOT)))
    target.parent.mkdir(parents=True,exist_ok=True)
    with part.open('xb') as output:
     for start in range(0,len(data),512*1024):
      chunk=data[start:start+512*1024]
      if reservation:reservation.check_write(len(chunk),part)
      else:b.check(len(chunk),len(chunk),part)
      output.write(chunk)
    os.replace(part,target)
   records.append({'path':str(target.relative_to(ROOT)),'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),'url':url,'status':status})
  report={'font':'Noto Sans KR variable','license':'SIL Open Font License1.1','publisher':'Google Fonts','publisher_git_blob_sha1':FONT_BLOB,'publisher_metadata':'https://api.github.com/repos/google/fonts/contents/ofl/notosanskr/NotoSansKR%5Bwght%5D.ttf','assets':records,'bytes':sum(r['bytes'] for r in records),'transformation':'none; complete font, including Korean glyphs, bundled once'}
  if development:(ROOT/'reports/prototype/font.json').write_text(json.dumps(report,indent=2)+'\n')
  else:atomic_json(ROOT/'reports/prototype/font.json',report,b)
  print(json.dumps(report))
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--development',action='store_true');main(p.parse_args().development)
