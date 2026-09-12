#!/usr/bin/env python3
"""One bounded official Firefox alternative when host Chromium sandbox cannot run.

Keep default Firefox security settings. Exact file preallocation avoids transient
buffered filesystem allocation exceeding the inspected peak. No system install.
"""
from pathlib import Path,PurePosixPath
from contextlib import nullcontext
import argparse,hashlib,json,os,stat,urllib.request,urllib.parse,zipfile
from hidden_view_finder.prototype.runtime import budget as prototype_budget,config as prototype_config
from seoul_visibility.acquisition_safety import atomic_json,preallocate_keep_size
ROOT=Path(__file__).resolve().parents[2];DEPS=ROOT/'data/prototype/dependencies'
URL='https://cdn.playwright.dev/dbazure/download/playwright/builds/firefox/1543/firefox-ubuntu-24.04.zip'
ARCHIVE_BYTES=114822786;EXPANDED_BYTES=320457770
ETAG='"0xDA58D8A6D9A220AC105391BC64AF04CA94DEB1C57CC00DACDB21ED364D3D92BB"'
CAP=900_000_000

def main(development=False):
 b=prototype_budget(prototype_config())
 if development:
  lock=json.loads((ROOT/'.citywide-writer.lock').read_text());os.kill(lock['pid'],0)
  if not lock['label'].startswith('prototype development:'):raise RuntimeError('Live enclosing development reservation required')
 with nullcontext(None) if development else b.reserve(ARCHIVE_BYTES+EXPANDED_BYTES+5_000_000,ARCHIVE_BYTES+EXPANDED_BYTES,'prototype Firefox alternative') as reservation:
  roots=[DEPS/name for name in ['browser-archives','playwright-core','chromium-headless-shell','browser-native','firefox']]
  def usage():return sum(max(p.stat().st_size,p.stat().st_blocks*512) for root in roots if root.exists() for p in root.rglob('*') if p.is_file())
  class Guard:
   budget=b;_small_batch_mode=False
   def check_write(self,size,path):
    b.safe_path(path)
    if usage()+size+4096>CAP:raise RuntimeError('Browser aggregate900MB limit')
    if reservation:return reservation.check_write(size,path)
    return b.check(size,size,path)
   def observe(self):
    if usage()>CAP:raise RuntimeError('Browser aggregate900MB pressure')
    return reservation.observe() if reservation else b.check()
  guard=Guard();archive=b.safe_path(DEPS/'browser-archives/firefox-1543.zip');archive.parent.mkdir(parents=True,exist_ok=True)
  part=archive.with_suffix('.zip.part');side=archive.with_suffix('.zip.state.json')
  if not archive.exists():
   if part.exists():
    if not side.exists() or json.loads(side.read_text()).get('etag')!=ETAG:raise RuntimeError('Preserved Firefox partial identity unavailable')
   offset=part.stat().st_size if part.exists() else 0
   headers={'Accept-Encoding':'identity'}
   if offset:headers.update({'Range':f'bytes={offset}-','If-Range':ETAG})
   with urllib.request.urlopen(urllib.request.Request(URL,headers=headers),timeout=30) as response:
    host=urllib.parse.urlsplit(response.url).hostname
    if host not in {'cdn.playwright.dev','playwright.download.prss.microsoft.com'} or response.headers.get('ETag')!=ETAG:raise RuntimeError('Firefox distribution identity changed')
    if response.status!=(206 if offset else 200):raise RuntimeError('Firefox response/resume status invalid')
    if offset and response.headers.get('Content-Range')!=f'bytes {offset}-{ARCHIVE_BYTES-1}/{ARCHIVE_BYTES}':raise RuntimeError('Firefox resume range invalid')
    length=response.headers.get('Content-Length')
    if length and int(length)!=ARCHIVE_BYTES-offset:raise RuntimeError('Firefox distribution size changed')
    side.write_text(json.dumps({'url':URL,'etag':ETAG,'bytes':ARCHIVE_BYTES,'ownership':'prototype browser dependency'})+'\n')
    with part.open('r+b' if part.exists() else 'xb') as out:
     preallocate_keep_size(out,ARCHIVE_BYTES,part,guard);out.seek(offset)
     while data:=response.read(2*1024*1024):
      if offset+len(data)>ARCHIVE_BYTES:raise RuntimeError('Firefox transfer bound')
      guard.observe();out.write(data);offset+=len(data)
    if offset!=ARCHIVE_BYTES:raise RuntimeError('Truncated Firefox distribution; partial retained')
   if part.open('rb').read(4)!=b'PK\x03\x04':raise RuntimeError('Firefox archive magic')
   os.replace(part,archive)
  if archive.stat().st_size!=ARCHIVE_BYTES:raise RuntimeError('Existing Firefox archive size mismatch')
  outputs=[]
  with zipfile.ZipFile(archive) as z:
   members=z.infolist()
   if len(members)!=54 or sum(m.file_size for m in members)!=EXPANDED_BYTES:raise RuntimeError('Firefox inspected archive schema changed')
   if not (DEPS/'firefox').exists() and usage()+EXPANDED_BYTES+1_000_000>CAP:raise RuntimeError('Firefox expansion cannot fit900MB browser cap')
   for member in members:
    p=PurePosixPath(member.filename);mode=member.external_attr>>16
    if p.is_absolute() or '..' in p.parts or stat.S_ISLNK(mode) or stat.S_IFMT(mode) not in {0,stat.S_IFREG,stat.S_IFDIR}:raise RuntimeError('Unsafe Firefox member')
    if member.is_dir():continue
    target=b.safe_path(DEPS/'firefox'/str(p));target.parent.mkdir(parents=True,exist_ok=True);part=target.with_suffix(target.suffix+'.part')
    if target.exists():
     with z.open(member) as src:expected=hashlib.file_digest(src,'sha256').hexdigest()
     with target.open('rb') as src:actual=hashlib.file_digest(src,'sha256').hexdigest()
     if target.stat().st_size!=member.file_size or actual!=expected:raise RuntimeError('Existing Firefox member changed; preserved')
     outputs.append({'path':str(target.relative_to(ROOT)),'bytes':member.file_size,'sha256':actual});continue
    if part.exists():raise RuntimeError('Preserved interrupted Firefox member; inspect before resuming')
    with z.open(member) as src,part.open('xb') as out:
     preallocate_keep_size(out,member.file_size,part,guard);count=0;digest=hashlib.sha256()
     while data:=src.read(2*1024*1024):
      count+=len(data)
      if count>member.file_size:raise RuntimeError('Firefox expanded member bound')
      guard.observe();out.write(data);digest.update(data)
     if count!=member.file_size:raise RuntimeError('Firefox member truncated')
    os.chmod(part,mode & 0o755);os.replace(part,target)
    outputs.append({'path':str(target.relative_to(ROOT)),'bytes':count,'sha256':digest.hexdigest()})
  report={'browser':'Firefox155.0','playwright_revision':'1543','url':URL,'etag':ETAG,'archive_bytes':ARCHIVE_BYTES,'archive_sha256':hashlib.file_digest(archive.open('rb'),'sha256').hexdigest(),'integrity':'local whole-archive SHA256 and ZIP CRC; no independently published checksum','expanded_bytes':EXPANDED_BYTES,'outputs':outputs,'browser_executable':'data/prototype/dependencies/firefox/firefox/firefox','browser_aggregate_bytes':usage(),'browser_cap_bytes':CAP,'chromium_preserved':True,'sandbox_changes':False,'purpose':'One official default-sandbox alternative after confirmed Chromium host sandbox failure'}
  if development:(ROOT/'reports/prototype/firefox-dependencies.json').write_text(json.dumps(report,indent=2)+'\n')
  else:atomic_json(ROOT/'reports/prototype/firefox-dependencies.json',report,b)
  print(json.dumps({k:v for k,v in report.items() if k!='outputs'}))
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--development',action='store_true');main(p.parse_args().development)
