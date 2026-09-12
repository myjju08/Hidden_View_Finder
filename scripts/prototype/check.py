#!/usr/bin/env python3
"""Run the small fixture suite under the same shared storage reservation.
A fresh unique directory preserves earlier test outputs. No real acquisition or
paid provider call is part of the suite; fixture outputs have a250MB peak bound.
"""
from pathlib import Path
import os,sys,time,json,resource,re
from hidden_view_finder.prototype.runtime import ROOT,config,budget,artifact_usage
from seoul_visibility.acquisition_safety import run_bounded,atomic_json

def main():
 c=config();b=budget(c);stamp=time.strftime('%Y%m%dT%H%M%S');base=ROOT/'data/citywide/staging'/('prototype-check-'+stamp);log=ROOT/'data/prototype/test-artifacts'/('pytest-'+stamp+'.log')
 usage=artifact_usage(c)
 # Capped2MB child log plus small report/fixture logs, with25% margin.
 if usage['used_bytes']+5_000_000>usage['limit_bytes']:raise RuntimeError('Combined prototype artifact cap reached; preserve previous artifacts and resume after manifest-owned cleanup')
 command=[sys.executable,'-m','pytest','-q','--basetemp='+str(base),'-p','no:cacheprovider']
 for item in sys.argv[1:]:
  target=(ROOT/item).resolve()
  if not target.is_relative_to(ROOT/'tests') or not target.is_file() or target.suffix!='.py':raise ValueError('Only existing repository testfiles accepted')
  command.append(str(target))
 resource.setrlimit(resource.RLIMIT_CORE,(0,0))
 result=run_bounded(command,b,peak_bytes=250_000_000,temporary_bytes=250_000_000,cwd=ROOT,env=dict(os.environ),timeout=300,log_path=log,maximum_log_bytes=2_000_000,lag_margin_bytes=64*1024**2,poll_seconds=.25)
 tail=log.read_text()[-4000:]
 match=re.search(r'(\d+) passed(?:, (\d+) warnings?)? in ([\d.]+)s',tail)
 result['test_summary']={'passed':int(match[1]),'warnings':int(match[2] or 0),'pytest_seconds':float(match[3]),'skipped':0,'failed':0} if match else {'summary':'See bounded log; summary pattern unavailable'}
 result['artifact_usage']=artifact_usage(c)
 result['command']=['bash','scripts/prototype/python.sh','scripts/prototype/check.py',*sys.argv[1:]];result['log_path']=str(log.relative_to(ROOT));result['synthetic_fixtures']=True
 atomic_json(ROOT/'reports/prototype/tests-final.json',result,b)
 print(tail);print('Report: reports/prototype/tests-final.json')
if __name__=='__main__':main()
