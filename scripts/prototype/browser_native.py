#!/usr/bin/env python3
"""Extract eight small official Ubuntu browser libraries locally, never install.

No package scripts execute. Relative library links become inspected copies of
their archive targets, so no writable output path follows a symlink. Cached APT
SHA256 and exact versions remain pinned; one official snapshot fallback is used
only for404/410. This does not alter the GIS runtime or system library paths.
"""
from hidden_view_finder.prototype.runtime import budget as prototype_budget,config as prototype_config
from pathlib import Path,PurePosixPath
from contextlib import nullcontext
import argparse,hashlib,io,json,os,re,subprocess,tarfile,urllib.request,urllib.error
from seoul_visibility.acquisition_safety import Budget,atomic_json
ROOT=Path(__file__).resolve().parents[2]
DEST=ROOT/'data/prototype/dependencies/browser-native'
NAMES=['libnspr4','libnss3','libatk1.0-0t64','libatk-bridge2.0-0t64','libatspi2.0-0t64','libxcomposite1','libxdamage1']
LOCK=ROOT/'configs/prototype-browser-native.json'

def plan(firefox=False):
 requested=['libgtk-3-0t64','libdbus-glib-1-2'] if firefox else NAMES
 selected={'libgtk-3-0t64','libdbus-glib-1-2','libepoxy0','libcolord2','libcups2t64','libavahi-common3','libavahi-client3','libxinerama1','libdconf1'}
 output=subprocess.check_output(['apt-get','--print-uris','--yes','--download-only','--no-install-recommends','install',*requested],text=True,timeout=30)
 entries=[]
 for line in output.splitlines():
  m=re.fullmatch(r"'([^']+)' (\S+) (\d+) MD5Sum:([a-f0-9]+)",line)
  if m:
   url,name,size,md5=m.groups()
   if firefox and urllib.parse.unquote(name).rsplit('_',2)[0] not in selected:continue
   if not url.startswith('http://archive.ubuntu.com/ubuntu/'):raise RuntimeError('Unapproved native browser host')
   entries.append({'url':url.replace('http:','https:',1),'filename':name,'bytes':int(size),'md5':md5})
 for entry in entries:
  decoded=urllib.parse.unquote(entry['filename']);package,version,_=decoded.rsplit('_',2)
  meta=subprocess.check_output(['apt-cache','show',package+'='+version],text=True,timeout=10)
  fields=dict(line.split(': ',1) for line in meta.splitlines() if ': ' in line and not line.startswith(' '))
  if fields.get('MD5sum')!=entry['md5'] or int(fields.get('Size','0'))!=entry['bytes']:raise RuntimeError('Cached APT metadata mismatch')
  entry.update(package=package,version=version,sha256=fields['SHA256'])
 if sum(e['bytes'] for e in entries)>(5_000_000 if firefox else 3_000_000) or len(entries)>10:raise RuntimeError('Native browser package plan exceeded cap')
 return entries

def main(development=False,firefox=False):
 b=prototype_budget(prototype_config())
 if development:
  lock=json.loads((ROOT/'.citywide-writer.lock').read_text());os.kill(lock['pid'],0)
  if not lock['label'].startswith('prototype development:'):raise RuntimeError('Live development reservation required')
 with nullcontext(None) if development else b.reserve(45_000_000 if firefox else 20_000_000,45_000_000 if firefox else 20_000_000,'prototype native browser libraries') as reservation:
  def check(size,path):
   b.safe_path(path)
   dirs=[ROOT/'data/prototype/dependencies'/name for name in ['browser-archives','playwright-core','chromium-headless-shell','firefox','browser-native']]
   counted=sum(max(p.stat().st_size,p.stat().st_blocks*512) for directory in dirs if directory.exists() for p in directory.rglob('*') if p.is_file())
   if counted+size+8192>900_000_000:raise RuntimeError('Native library would exceed900MB browser aggregate')
   if reservation:reservation.check_write(size,path)
   else:b.check(size,size,path)
  lock_path=LOCK.with_name('prototype-browser-native-firefox.json') if firefox else LOCK
  records=json.loads(lock_path.read_text()) if lock_path.exists() else plan(firefox)
  if not lock_path.exists():lock_path.write_text(json.dumps(records,indent=2)+'\n')
  total=0;outputs=[]
  for entry in records:
   archive=b.safe_path(DEST/'archives'/entry['filename']);archive.parent.mkdir(parents=True,exist_ok=True)
   if archive.exists():raw=archive.read_bytes()
   else:
    try:
     with urllib.request.urlopen(entry['url'],timeout=25) as r:raw=r.read(entry['bytes']+1)
    except urllib.error.HTTPError as error:
     if error.code not in {404,410}:raise
     url=entry['url'].replace('https://archive.ubuntu.com/ubuntu/','https://snapshot.ubuntu.com/ubuntu/20260824T000000Z/')
     with urllib.request.urlopen(url,timeout=25) as r:raw=r.read(entry['bytes']+1)
    if len(raw)!=entry['bytes'] or hashlib.sha256(raw).hexdigest()!=entry['sha256']:raise RuntimeError('Publisher native checksum failed')
    check(len(raw),archive);part=archive.with_suffix('.deb.part')
    if part.exists():raise RuntimeError('Preserved interrupted native archive')
    part.write_bytes(raw);os.replace(part,archive)
   if len(raw)!=entry['bytes'] or hashlib.sha256(raw).hexdigest()!=entry['sha256'] or not raw.startswith(b'!<arch>\n'):raise RuntimeError('Native archive validation failed')
   process=subprocess.Popen(['dpkg-deb','--fsys-tarfile',str(archive)],stdout=subprocess.PIPE)
   content=process.stdout.read(16_000_001)
   if len(content)>16_000_000:process.kill();process.wait();raise RuntimeError('Native package expanded transfer limit')
   if process.wait(timeout=10):raise RuntimeError('Native package decoder failed')
   with tarfile.open(fileobj=io.BytesIO(content),mode='r:') as tf:
    members={m.name.removeprefix('./'):m for m in tf.getmembers()}
    for name,member in members.items():
     p=PurePosixPath(name)
     if p.is_absolute() or '..' in p.parts or not(member.isfile() or member.isdir() or member.issym()):raise RuntimeError('Unsafe native member')
     if member.isdir():continue
     source=member
     linked_data=None
     if member.issym():
      target=PurePosixPath(os.path.normpath(str(p.parent/member.linkname)))
      if target.is_absolute() or '..' in target.parts:raise RuntimeError('Unsafe native library link')
      if str(target) in members and members[str(target)].isfile():source=members[str(target)]
      elif str(target).startswith('usr/share/doc/'):
       existing=b.safe_path(DEST/'root'/str(target))
       if not existing.is_file() and firefox:
        outputs.append({'path':str((DEST/'root'/name).relative_to(ROOT)),'status':'preserved_in_original_deb_only','target':str(target),'reason':'Cross-package documentation link; omitted package is not a headless runtime ELF dependency.'});continue
       if not existing.is_file() or existing.stat().st_size>100_000:raise RuntimeError('Unresolved native documentation link')
       linked_data=existing.read_bytes()
      else:raise RuntimeError('Unresolved native library link')
     member_cap=9_000_000 if firefox else 8_000_000
     data=linked_data if linked_data is not None else tf.extractfile(source).read(member_cap+1);total+=len(data)
     if len(data)>member_cap or total>(35_000_000 if firefox else 12_000_000):raise RuntimeError('Native browser expansion cap')
     output=b.safe_path(DEST/'root'/name);digest=hashlib.sha256(data).hexdigest()
     if output.exists():
      if hashlib.sha256(output.read_bytes()).hexdigest()!=digest:raise RuntimeError('Existing native library changed; preserved')
     else:
      check(len(data),output);output.parent.mkdir(parents=True,exist_ok=True);part=output.with_suffix(output.suffix+'.part')
      if part.exists():raise RuntimeError('Preserved interrupted native library')
      part.write_bytes(data);os.chmod(part,source.mode & 0o755);os.replace(part,output)
     outputs.append({'path':str(output.relative_to(ROOT)),'bytes':len(data),'sha256':digest,'materialized_link':member.issym()})
  report={'packages':records,'outputs':outputs,'expanded_bytes':total,'system_install':False,'maintainer_scripts_executed':False,'native_library_path':'data/prototype/dependencies/browser-native/root/usr/lib/x86_64-linux-gnu'}
  report_path=ROOT/'reports/prototype'/('browser-native-firefox.json' if firefox else 'browser-native.json')
  if development:report_path.write_text(json.dumps(report,indent=2)+'\n')
  else:atomic_json(report_path,report,b)
  print(json.dumps({'packages':len(records),'expanded_bytes':total}))
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--development',action='store_true');p.add_argument('--firefox',action='store_true');a=p.parse_args();main(a.development,a.firefox)
