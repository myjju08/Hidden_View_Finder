#!/usr/bin/env python3
"""Run owned localhost browser checks with counted profiles and bounded artifacts.

Chromium's sandbox remains enabled. When run as root, the existing unprivileged
`nobody` account owns only this task's disposable browser profiles/artifacts.
No system settings, users, mounts, quotas, or externally reachable service change.
"""
from hidden_view_finder.prototype.runtime import budget as prototype_budget,config as prototype_config,artifact_usage
from pathlib import Path
from contextlib import nullcontext
import argparse,json,os,resource,shutil,signal,subprocess,time
from seoul_visibility.acquisition_safety import Budget,atomic_json

ROOT=Path(__file__).resolve().parents[2]

def main(base,origin,development=False,launch_only=False,engine='chromium'):
 c=prototype_config();b=prototype_budget(c)
 if development:
  lock=json.loads((ROOT/'.citywide-writer.lock').read_text());os.kill(lock['pid'],0)
  if not lock['label'].startswith('prototype development:'):raise RuntimeError('Live development reservation required')
 stage=b.safe_path(ROOT/'data/citywide/staging/prototype-browser/runtime')
 artifact_root=b.safe_path(ROOT/'data/prototype/staging/artifacts')
 artifacts=b.safe_path(artifact_root/('run-'+str(time.time_ns())))
 usage=artifact_usage(c)
 # Measured browser profiles peaked near40MB;64MB incremental estimate plus25%
 # covers this bounded local-page/screenshot suite. Preserve existing artifacts.
 if usage['used_bytes']+80_000_000>usage['limit_bytes']:raise RuntimeError('Combined prototype artifact cap cannot fit conservative browser peak; existing artifacts preserved')
 context=nullcontext(None) if development else b.reserve(96_000_000,96_000_000,'prototype browser bounded test profiles and screenshots')
 with context as reservation:
  for directory in (stage,artifacts):
   directory.mkdir(parents=True,exist_ok=True)
   if os.getuid()==0:os.chown(directory,65534,65534)
  owner={'kind':'prototype-owned-disposable-browser-test','artifacts':str(artifacts.relative_to(ROOT)),'profiles':str(stage.relative_to(ROOT)),'created_unix':time.time(),'cleanup_policy':'Playwright removes only profiles it creates; screenshots/reports retained in unique run directory. No source or previous run files deleted.'}
  (artifacts/'ownership.json').write_text(json.dumps(owner,indent=2)+'\n')
  # Browser workers have no reason to inherit application/provider credentials.
  env={'PATH':os.environ.get('PATH','/usr/bin:/bin'),'LANG':'C.UTF-8','TMPDIR':str(stage),'TMP':str(stage),'TEMP':str(stage),'XDG_CACHE_HOME':str(stage/'cache'),'XDG_CONFIG_HOME':str(stage/'config'),'HVF_BROWSER_ARTIFACTS':str(artifacts),'HVF_BROWSER_BASE':base,'HVF_BROWSER_ORIGIN':origin,'HVF_BROWSER_LAUNCH_ONLY':'1' if launch_only else '0'}
  native=ROOT/'data/prototype/dependencies/browser-native/root/usr/lib/x86_64-linux-gnu'
  if native.is_dir():env['LD_LIBRARY_PATH']=str(native)
  env['HVF_BROWSER_ENGINE']=engine
  env['DEBUG']='pw:browser'
  command=[shutil.which('node'),str(ROOT/'scripts/prototype/browser_check.cjs')]
  if os.getuid()==0:command=['runuser','-u','nobody','--',*command]
  def limits():
   resource.setrlimit(resource.RLIMIT_CORE,(0,0));resource.setrlimit(resource.RLIMIT_FSIZE,(16_000_000,16_000_000));os.setsid()
  log=artifacts/'browser-run.log'
  if log.exists() and log.stat().st_size>1_000_000:raise RuntimeError('Preserving prior browser log: cap reached')
  start=time.monotonic();peak=0;peak_rss=0;process=None
  def owned_rss(pid):
   pending=[pid];seen=set();total=0
   while pending:
    current=pending.pop()
    if current in seen:continue
    seen.add(current)
    try:
     total+=int(Path(f'/proc/{current}/statm').read_text().split()[1])*os.sysconf('SC_PAGE_SIZE')
     pending.extend(int(p) for p in Path(f'/proc/{current}/task/{current}/children').read_text().split())
    except (FileNotFoundError,ProcessLookupError,IndexError):pass
   return total
  with log.open('ab') as output:
   process=subprocess.Popen(command,cwd=ROOT,env=env,stdout=output,stderr=subprocess.STDOUT,preexec_fn=limits)
   try:
    while process.poll() is None:
     combined=artifact_usage(c);size=combined['used_bytes'];peak=max(peak,size)
     rss=owned_rss(process.pid);peak_rss=max(peak_rss,rss)
     if rss>1_500_000_000:raise RuntimeError('Browser owned-process RSS watchdog limit1.5GB')
     if size>combined['limit_bytes']-16_000_000:raise RuntimeError('Combined prototype artifact pressure: preserving16MB stop margin')
     if log.stat().st_size>1_000_000:raise RuntimeError('Browser log exceeded1MB')
     if time.monotonic()-start>600:raise RuntimeError('Browser suite600s work deadline')
     if reservation:reservation.observe()
     else:b.check(16_000_000,16_000_000,artifacts)
     time.sleep(.5)
   finally:
    if process.poll() is None:
     os.killpg(process.pid,signal.SIGTERM)
     try:process.wait(timeout=3)
     except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGKILL);process.wait()
  report={'exit_code':process.returncode,'elapsed_s':round(time.monotonic()-start,3),'sampled_peak_browser_artifact_bytes':peak,'artifact_limit_bytes':usage['limit_bytes'],'artifact_measurement_scope':'Combined browser profiles, retained browser/test artifacts and prototype reports','sandbox_disabled':False,'run_as':'existing nobody user' if os.getuid()==0 else 'current user','command':['node','scripts/prototype/browser_check.cjs'],'log':str(log.relative_to(ROOT)),'artifacts':str(artifacts.relative_to(ROOT)),'shared_budget_policy':'20,000,000,000 total;8GiB filesystem free reserve;4GiB aggregate temporary; enclosing development reservation' if development else 'shared Budget reservation','os_quota_asserted':False}
  report.update(sampled_peak_owned_process_rss_bytes=peak_rss,rss_limit_bytes=1_500_000_000,rss_note='Sampled sum of owned process-tree RSS; shared pages may be counted more than once.')
  if development:(ROOT/'reports/prototype/browser-run.json').write_text(json.dumps(report,indent=2)+'\n')
  else:atomic_json(ROOT/'reports/prototype/browser-run.json',report,b)
  print(json.dumps(report));return process.returncode

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--base',default='http://127.0.0.1:8000');p.add_argument('--origin',default='');p.add_argument('--development',action='store_true');p.add_argument('--launch-only',action='store_true');p.add_argument('--engine',choices=['chromium','firefox'],default='chromium');a=p.parse_args();raise SystemExit(main(a.base,a.origin,a.development,a.launch_only,a.engine))
