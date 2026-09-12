#!/usr/bin/env python3
"""Bounded, optional browser-test dependency setup. No package install scripts.

Uses the npm publisher integrity for pinned playwright-core, then that release's
documented Chromium headless-shell distribution. Does not install system packages.
All archives, extraction, profiles, and caches stay in accounted project roots.
"""
from hidden_view_finder.prototype.runtime import budget as prototype_budget,config as prototype_config
from pathlib import Path, PurePosixPath
from contextlib import nullcontext
import argparse, base64, hashlib, json, os, stat, subprocess, tarfile, time, urllib.request, urllib.parse, zipfile
from seoul_visibility.acquisition_safety import Budget, atomic_json

ROOT=Path(__file__).resolve().parents[2]
DEPS=ROOT/'data/prototype/dependencies'
STAGE=ROOT/'data/citywide/staging/prototype-browser'
NPM_URL='https://registry.npmjs.org/playwright-core/-/playwright-core-1.63.0.tgz'
NPM_INTEGRITY='rYCsBF/M5HjUch52bbtVONEFjv6Xu8sm8h72dNlR5bzIE1fvC/bxgspzkjSfU+MweEMmPM8KJebG6nnyxo5mCg=='
HOSTS={'registry.npmjs.org','cdn.playwright.dev','playwright.download.prss.microsoft.com','cdn.playwright.dev','storage.googleapis.com','www.googleapis.com'}
BROWSER_MAX_BYTES=900_000_000

def main(development=False):
 b=prototype_budget(prototype_config())
 if development:
  lock=json.loads((ROOT/'.citywide-writer.lock').read_text());os.kill(lock['pid'],0)
  if not lock['label'].startswith('prototype development:') or lock['total_limit']>20_000_000_000:raise RuntimeError('Live enclosing development reservation required')
 context=nullcontext(None) if development else b.reserve(BROWSER_MAX_BYTES,BROWSER_MAX_BYTES,'prototype browser dependency archives and extraction')
 with context as reservation:
  def browser_bytes():
   paths=[DEPS/name for name in ['browser-archives','playwright-core','chromium-headless-shell','firefox','browser-native']]
   return sum(max(p.stat().st_size,p.stat().st_blocks*512) for directory in paths if directory.exists() for p in directory.rglob('*') if p.is_file())
  credit=0;last_check=0
  def check(size,path):
   nonlocal credit,last_check
   b.safe_path(path)
   if browser_bytes()+size>BROWSER_MAX_BYTES:raise RuntimeError('Browser aggregate footprint exceeds900MB')
   # A prepaid4MiB allowance avoids rescanning all unchanged GIS inputs for
   # each tiny library file. Replenish at most once per second; statvfs and the
   # browser-specific byte cap are still checked before every chunk.
   if size>credit or time.monotonic()-last_check>=1:
    allowance=max(4*1024*1024,size)
    if reservation:reservation.check_write(allowance,path)
    else:b.check(allowance,allowance,path)
    credit=allowance;last_check=time.monotonic()
   fs=os.statvfs(path.parent if path.parent.exists() else DEPS)
   if fs.f_bavail*fs.f_frsize-size<8*1024**3+16*1024**2:raise RuntimeError('Browser filesystem reserve pressure')
   credit-=size
  def download(url,path,cap,sha512=None):
   b.safe_path(path);path.parent.mkdir(parents=True,exist_ok=True)
   if path.exists():data_hash=hashlib.file_digest(path.open('rb'),'sha512').digest()
   else:
    part=path.with_suffix(path.suffix+'.part');b.safe_path(part)
    if part.exists():raise RuntimeError('Preserved interrupted browser download: '+str(part.relative_to(ROOT)))
    with urllib.request.urlopen(urllib.request.Request(url,headers={'Accept-Encoding':'identity'}),timeout=30) as r:
     if r.status!=200 or urllib.parse.urlsplit(r.url).hostname not in HOSTS:raise RuntimeError('Unapproved browser distribution response')
     length=int(r.headers.get('Content-Length',0))
     if length>cap:raise RuntimeError('Browser archive declared size exceeds cap')
     total=0;digest=hashlib.sha512()
     with part.open('xb') as f:
      while data:=r.read(512*1024):
       total+=len(data)
       if total>cap:raise RuntimeError('Browser archive transfer exceeds cap')
       check(len(data),part);f.write(data);digest.update(data)
     if length and total!=length:raise RuntimeError('Truncated browser archive')
     data_hash=digest.digest()
    if sha512 and base64.b64encode(data_hash).decode()!=sha512:raise RuntimeError('npm publisher integrity mismatch')
    os.replace(part,path)
   if path.stat().st_size>cap or (sha512 and base64.b64encode(data_hash).decode()!=sha512):raise RuntimeError('Existing dependency failed validation')
   return {'url':url,'path':str(path.relative_to(ROOT)),'bytes':path.stat().st_size,'sha256':hashlib.file_digest(path.open('rb'),'sha256').hexdigest(),'integrity':'npm publisher SHA512' if sha512 else 'local SHA256 only; no separately published browser checksum'}
  npm=DEPS/'browser-archives/playwright-core-1.63.0.tgz'
  record=download(NPM_URL,npm,6_000_000,NPM_INTEGRITY)
  dest=DEPS/'playwright-core';expanded=0
  def write_member(target,stream,size,mode):
   check(0,target);target.parent.mkdir(parents=True,exist_ok=True)
   if target.exists():
    actual=hashlib.file_digest(target.open('rb'),'sha256').hexdigest();expected=hashlib.file_digest(stream,'sha256').hexdigest()
    if target.stat().st_size!=size or actual!=expected:raise RuntimeError('Preserving incompatible extracted browser member')
    return
   part=target.with_suffix(target.suffix+'.part');b.safe_path(part)
   if part.exists():raise RuntimeError('Preserved interrupted browser extraction member')
   count=0
   with part.open('xb') as f:
    while data:=stream.read(512*1024):
     count+=len(data)
     if count>size:raise RuntimeError('Expansion exceeded inspected member size')
     check(len(data),part);f.write(data)
   if count!=size:raise RuntimeError('Truncated extracted member')
   os.chmod(part,mode & 0o755);os.replace(part,target)
  with tarfile.open(npm,'r:gz') as archive:
   for member in archive:
    parts=PurePosixPath(member.name)
    if parts.is_absolute() or '..' in parts.parts or not parts.parts or parts.parts[0]!='package' or not(member.isfile() or member.isdir()):raise RuntimeError('Unsafe npm member')
    expanded+=member.size
    if expanded>20_000_000:raise RuntimeError('npm expansion exceeds20MB')
    if member.isdir():continue
    write_member(dest/str(PurePosixPath(*parts.parts[1:])),archive.extractfile(member),member.size,member.mode)
  STAGE.mkdir(parents=True,exist_ok=True)
  env={**os.environ,'TMPDIR':str(STAGE),'TMP':str(STAGE),'TEMP':str(STAGE),'XDG_CACHE_HOME':str(STAGE/'cache'),'PLAYWRIGHT_BROWSERS_PATH':str(DEPS/'browsers')}
  dry=subprocess.run(['node',str(dest/'cli.js'),'install','--dry-run','chromium-headless-shell'],env=env,check=True,capture_output=True,text=True,timeout=30)
  if len(dry.stdout)>30_000:raise RuntimeError('Browser plan output cap')
  import re
  urls=re.findall(r'https://[^\s]+',dry.stdout)
  shell_urls=[url for url in urls if ('headless-shell' in url or 'headless_shell' in url)]
  if not shell_urls:raise RuntimeError('Headless shell download was not identified in official CLI plan')
  url=shell_urls[0]
  archive_path=DEPS/'browser-archives/chromium-headless-shell.zip'
  browser=download(url,archive_path,180_000_000)
  browser_dest=DEPS/'chromium-headless-shell'
  with zipfile.ZipFile(archive_path) as archive:
   total=sum(member.file_size for member in archive.infolist())
   missing=sum(m.file_size for m in archive.infolist() if not m.is_dir() and not (browser_dest/m.filename).exists())
   if missing+browser_bytes()>BROWSER_MAX_BYTES:raise RuntimeError('Browser inspected missing expansion plus retained files exceeds900MB: '+str(missing+browser_bytes()))
   for member in archive.infolist():
    p=PurePosixPath(member.filename);mode=member.external_attr>>16
    if p.is_absolute() or '..' in p.parts or stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in {0,stat.S_IFREG,stat.S_IFDIR}):raise RuntimeError('Unsafe browser ZIP member')
    if member.is_dir():continue
    write_member(browser_dest/str(p),archive.open(member),member.file_size,mode or 0o644)
  executables=list(browser_dest.glob('*/headless_shell'))+list(browser_dest.glob('*/chrome-headless-shell'))
  if len(executables)!=1:raise RuntimeError('Expected one extracted headless executable')
  report={'playwright_version':'1.63.0','npm':record,'browser':browser,'expanded_bytes':total+expanded,'aggregate_bytes':browser_bytes(),'browser_allocation_bytes':BROWSER_MAX_BYTES,'browser_executable':str(executables[0].relative_to(ROOT)),'cli_plan':dry.stdout,'test_status':'not yet run','system_packages_installed':False,'documentation':['https://playwright.dev/docs/browsers','https://playwright.dev/python/docs/browsers#chromium-headless-shell']}
  path=ROOT/'reports/prototype/browser-dependencies.json'
  if development:path.write_text(json.dumps(report,indent=2)+'\n')
  else:atomic_json(path,report,b)
  print(json.dumps(report))

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--development',action='store_true');main(p.parse_args().development)
