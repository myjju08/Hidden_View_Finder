#!/usr/bin/env python3
"""Acquire and validate versioned Seoul geographic inputs under one hard budget."""
from __future__ import annotations
import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import resource
import shutil
import sqlite3
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'src'),str(ROOT)]
from seoul_visibility.acquisition_safety import Budget, AcquisitionError, MAX_METADATA_BYTES, atomic_json, guarded_download, sha256, redact_url
from seoul_visibility.resources import available_memory_bytes


def now(): return datetime.now(timezone.utc).isoformat()
def fingerprint(value): return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def _checkpoint_bytes(value):
    content=(json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode()
    if len(content)>MAX_METADATA_BYTES:
        raise AcquisitionError('checkpoint_blocked','State checkpoint exceeds the bounded 8 MiB metadata allowance')
    return content


def _checkpoint_payload(path,content):
    """Exclusive, bounded state writer; ownership is durable before this call."""
    with path.open('xb') as stream:
        stream.write(content);stream.flush();os.fsync(stream.fileno())


def _checkpoint_sync(directory):
    fd=os.open(directory,os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
    try:os.fsync(fd)
    finally:os.close(fd)


class Pipeline:
    def __init__(self,config):
        self.config_path=Path(config).resolve()
        self.config=json.loads(self.config_path.read_text())
        c=self.config;s=c['storage']
        if c['projected_crs']!='EPSG:5186': raise ValueError('Existing citywide builder supports EPSG:5186')
        for k in ['maximum_sight_distance_m','terrain_interpolation_halo_m','resolution_m','candidate_spacing_m']:
            if not isinstance(c[k],(int,float)) or not math.isfinite(c[k]) or c[k]<=0: raise ValueError(f'Invalid {k}')
        if s['uncertain_incremental_margin']<1.25: raise ValueError('Safety margin must be at least25%')
        if not s['one_writer'] or c['memory']['workers']!=1: raise ValueError('Only one large writer is supported')
        extra=[(ROOT/p).resolve() for p in c['external_data_roots']]
        # Only explicitly configured roots are scanned; no unrelated private directory search.
        self.data=(ROOT/c['data_root']).absolute()
        self.budget=Budget(ROOT,extra_roots=extra,limit=s['total_bytes'],min_free=s['minimum_free_bytes'],
                           stage_root=self.data/'staging',stage_limit=s['temporary_bytes'],additional_accounted_bytes=5*1024**2)
        self.data=self.budget.safe_path(self.data)
        self.config_id=fingerprint(c)[:12]
        self.norm=self.data/'normalized'/self.config_id
        self.state_path=self.data/f'state.{self.config_id}.json'
        self.state=self._load_checkpoint_state()
        if self.state is None:self.state={
            'schema_version':1,'configuration_sha256':fingerprint(c),'created_utc':now(),'stages':{},'commands':[],
            'initial_storage':self.budget.check(),'cleanup':[]}
        self.state['resume_command']=f'bash scripts/data/python.sh scripts/data/citywide.py --config {self.config_path.relative_to(ROOT)} resume'
        self.state['implementation_files_sha256']={str(path.relative_to(ROOT)):sha256(path)
            for base in [ROOT/'scripts/data',ROOT/'src/seoul_visibility'] for path in sorted(base.glob('*.py'))}
        for relative in ['scripts/acquire_buildings.py','scripts/acquire_terrain.py','scripts/acquire_seoul_boundary.py',
                         'scripts/building_acquisition_common.py','scripts/data/python.sh']:
            if (ROOT/relative).is_file():self.state['implementation_files_sha256'][relative]=sha256(ROOT/relative)
        self.state['additional_accounted_bytes_explanation']='Conservative5MiB allowance for early synthetic pytest scratch outside the checkout before explicit basetemp was added; no private directories searched. Subsequent fixtures remain under accounted staging.'
        for key,value in self.state.get('resource_overrides',{}).items():
            if key!='candidates_bytes':raise ValueError('Unknown persisted resource override')
            self.set_candidate_limit(value)

    def set_candidate_limit(self,value):
        # Resource-only adjustment: source identities, geographic contract and
        # deterministic sampling recipes do not change. Shared limits still win.
        if type(value) is not int or not 4*1024**2<=value<=min(self.budget.stage_limit,self.budget.limit):
            raise ValueError('Candidate artifact limit must fit the existing temporary and total ceilings')
        self.state.setdefault('resource_overrides',{})['candidates_bytes']=value

    def relative(self,path): return str(Path(path).relative_to(ROOT))

    def _checkpoint_path(self,identifier,suffix):
        if (not isinstance(identifier,str) or len(identifier)!=32
                or any(c not in '0123456789abcdef' for c in identifier)):
            raise AcquisitionError('checkpoint_blocked','Invalid checkpoint identity; state files preserved')
        return self.budget.safe_path(self.state_path.with_name(self.state_path.name+'.'+identifier+suffix))

    def _read_checkpoint_json(self,path,with_fingerprint=False):
        path=self.budget.safe_path(path)
        if not path.is_file() or path.stat().st_size>MAX_METADATA_BYTES:
            raise AcquisitionError('checkpoint_blocked',f'Missing or oversized checkpoint metadata: {self.relative(path)}; preserved')
        content=path.read_bytes()
        if len(content)>MAX_METADATA_BYTES:
            raise AcquisitionError('checkpoint_blocked',f'Checkpoint grew beyond its metadata bound: {self.relative(path)}; preserved')
        try:value=json.loads(content)
        except (ValueError,UnicodeError) as exc:
            raise AcquisitionError('checkpoint_blocked',f'Corrupt checkpoint JSON: {self.relative(path)}; preserved for inspection') from exc
        if not isinstance(value,dict):
            raise AcquisitionError('checkpoint_blocked',f'Checkpoint is not an object: {self.relative(path)}; preserved')
        return (value,len(content),hashlib.sha256(content).hexdigest()) if with_fingerprint else value

    def _checkpoint_owner(self,identifier):
        path=self._checkpoint_path(identifier,'.owner.json')
        value=self._read_checkpoint_json(path)
        if (value.get('schema')!='citywide-state-checkpoint-v1'
                or value.get('task_owned') is not True or value.get('exclusive_creation') is not True
                or value.get('configuration_sha256')!=fingerprint(self.config)
                or value.get('canonical_path')!=self.state_path.name
                or value.get('writer_path')!=self._checkpoint_path(identifier,'.writing').name
                or type(value.get('expected_bytes')) is not int
                or not 0<value['expected_bytes']<=MAX_METADATA_BYTES
                or not isinstance(value.get('expected_sha256'),str)):
            raise AcquisitionError('checkpoint_blocked',f'Checkpoint ownership does not match this configuration: {self.relative(path)}; preserved')
        return value

    def _read_checkpoint_state(self,path,with_fingerprint=False):
        value,size,digest=self._read_checkpoint_json(path,with_fingerprint=True)
        if (value.get('schema_version')!=1 or value.get('configuration_sha256')!=fingerprint(self.config)
                or not isinstance(value.get('stages'),dict) or not isinstance(value.get('commands'),list)
                or not isinstance(value.get('initial_storage'),dict) or not isinstance(value.get('cleanup'),list)
                or not isinstance(value.get('checkpoint_preservations',[]),list)):
            raise AcquisitionError('checkpoint_blocked',f'State schema or configuration mismatch: {self.relative(path)}; preserved')
        identifier=value.get('checkpoint_id')
        if identifier is not None:
            owner=self._checkpoint_owner(identifier)
            if size!=owner['expected_bytes'] or digest!=owner['expected_sha256']:
                raise AcquisitionError('checkpoint_blocked',f'State checksum differs from its durable ownership receipt: {self.relative(path)}; preserved')
        return (value,digest) if with_fingerprint else value

    def _checkpoint_writers(self,canonical_hash):
        """Read only known state-writer names; never search unrelated directories."""
        if not self.data.exists():return [],[]
        prefix=self.state_path.name+'.'
        writers=[];eligible=[]
        for path in sorted(self.data.glob(prefix+'*.writing')):
            middle=path.name[len(prefix):-len('.writing')]
            if middle and (len(middle)!=32 or any(c not in '0123456789abcdef' for c in middle)):
                continue
            path=self.budget.safe_path(path)
            if not path.is_file():
                raise AcquisitionError('checkpoint_blocked',f'Unexpected state writer type: {self.relative(path)}; preserved')
            if len(writers)>=1000 or path.stat().st_size>MAX_METADATA_BYTES:
                raise AcquisitionError('checkpoint_blocked','State writer count/size exceeds bounded recovery; preserved for inspection')
            item={'path':self.relative(path),'sha256':sha256(path),'bytes':path.stat().st_size,
                  'allocated_bytes':path.stat().st_blocks*512,'action':'preserved_in_place',
                  'deletion_performed':False,'ownership_verified':False}
            owner_path=self._checkpoint_path(middle,'.owner.json') if middle else None
            if owner_path is not None and owner_path.exists():
                owner=self._checkpoint_owner(middle)
                item.update(ownership_verified=True,ownership_record=self.relative(owner_path))
                if item['bytes']>owner['expected_bytes']:
                    raise AcquisitionError('checkpoint_blocked',f'State writer grew beyond its owned bound: {item["path"]}; preserved')
                if item['bytes']==owner['expected_bytes'] and item['sha256']==owner['expected_sha256']:
                    state=self._read_checkpoint_state(path)
                    if state.get('checkpoint_id')!=middle:
                        raise AcquisitionError('checkpoint_blocked','Complete state writer has mismatched checkpoint identity; preserved')
                    if owner.get('previous_canonical_sha256')==canonical_hash:
                        eligible.append((item,state))
            writers.append(item)
        legacy=self.state_path.with_name(self.state_path.name+'.writing')
        if legacy.exists() or legacy.is_symlink():
            legacy=self.budget.safe_path(legacy)
            if not legacy.is_file() or legacy.stat().st_size>MAX_METADATA_BYTES:
                raise AcquisitionError('checkpoint_blocked','Legacy state writer is unsafe or oversized; preserved')
            writers.append({'path':self.relative(legacy),'sha256':sha256(legacy),
                'bytes':legacy.stat().st_size,'allocated_bytes':legacy.stat().st_blocks*512,
                'action':'preserved_in_place','deletion_performed':False,'ownership_verified':False,
                'reason':'Legacy fixed-name writer has no durable ownership; it is never promoted or overwritten'})
        # A second failed save may explicitly supersede a first complete writer.
        superseded=set()
        for item,state in eligible:
            owner=self._checkpoint_owner(state['checkpoint_id'])
            if owner.get('supersedes_pending_sha256'):superseded.add(owner['supersedes_pending_sha256'])
        tips=[row for row in eligible if row[0]['sha256'] not in superseded]
        if len(tips)>1:
            raise AcquisitionError('checkpoint_blocked','Multiple complete state writers disagree; all preserved for inspection')
        return writers,tips

    def _load_checkpoint_state(self):
        self.budget.safe_path(self.state_path)
        canonical,self._state_disk_sha256=(self._read_checkpoint_state(self.state_path,with_fingerprint=True)
            if self.state_path.exists() else (None,None))
        try: writers,pending=self._checkpoint_writers(self._state_disk_sha256)
        except FileNotFoundError:
            # A live writer may atomically publish while read-only status inspects
            # its pending name. Re-read the canonical snapshot once; no mutation.
            canonical,self._state_disk_sha256=(self._read_checkpoint_state(self.state_path,with_fingerprint=True)
                if self.state_path.exists() else (None,None))
            writers,pending=self._checkpoint_writers(self._state_disk_sha256)
        self._loaded_pending_sha256=pending[0][0]['sha256'] if pending else None
        if pending:return pending[0][1]
        if canonical is None and writers:
            raise AcquisitionError('checkpoint_blocked',
                'Canonical state is absent and no complete owned matching writer can recover it; '
                'preserve and inspect '+', '.join(row['path'] for row in writers[:3]))
        return canonical

    def save(self):
        # Read-only status construction does not acquire this lock. Any recovery
        # publication and all ownership/report writes use the live writer lock.
        with self.budget.checkpoint_writer():
            current_hash=(self._read_checkpoint_state(self.state_path,with_fingerprint=True)[1]
                if self.state_path.exists() else None)
            if current_hash!=self._state_disk_sha256:
                raise AcquisitionError('checkpoint_blocked','Canonical state changed since this invocation loaded it; preserve files and rerun the command')
            writers,pending=self._checkpoint_writers(current_hash)
            pending_hash=pending[0][0]['sha256'] if pending else None
            if pending_hash!=self._loaded_pending_sha256:
                raise AcquisitionError('checkpoint_blocked','A completed state writer appeared since loading; preserve files and rerun to recover it')
            preserved=self.state.setdefault('checkpoint_preservations',[])
            known={(item.get('path'),item.get('sha256')) for item in preserved if isinstance(item,dict)}
            preserved.extend(item for item in writers if (item['path'],item['sha256']) not in known)
            identifier=uuid.uuid4().hex
            self.state.update(updated_utc=now(),storage=self.budget.snapshot(),checkpoint_id=identifier)
            content=_checkpoint_bytes(self.state)
            writer=self._checkpoint_path(identifier,'.writing')
            owner_path=self._checkpoint_path(identifier,'.owner.json')
            owner={'schema':'citywide-state-checkpoint-v1','task_owned':True,'exclusive_creation':True,
                'configuration_sha256':fingerprint(self.config),'canonical_path':self.state_path.name,
                'writer_path':writer.name,'expected_bytes':len(content),'expected_sha256':hashlib.sha256(content).hexdigest(),
                'previous_canonical_sha256':current_hash,'supersedes_pending_sha256':pending_hash,
                'preserved_writers':writers,'created_utc':now()}
            owner_bytes=_checkpoint_bytes(owner)
            self.budget.check(len(content)+len(owner_bytes)+32768,len(content)+len(owner_bytes)+32768,writer,checkpoint=True)
            atomic_json(owner_path,owner,self.budget,checkpoint=True)
            _checkpoint_sync(self.data)
            self.budget.check(len(content)+8192,len(content)+8192,writer,checkpoint=True)
            _checkpoint_payload(writer,content)
            self._read_checkpoint_state(writer)
            if (sha256(self.state_path) if self.state_path.exists() else None)!=current_hash:
                raise AcquisitionError('checkpoint_blocked','Canonical state changed before publication; both versions preserved')
            writer.replace(self.state_path)
            self._state_disk_sha256=owner['expected_sha256'];self._loaded_pending_sha256=None
            _checkpoint_sync(self.data)

    @contextmanager
    def lock(self):
        self.budget.check(additional=65536)
        path=self.budget.safe_path(ROOT/'.citywide-run.lock')
        fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        try:
            try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError as exc: raise RuntimeError('A live citywide invocation is running; its files are preserved') from exc
            yield
        finally: os.close(fd)

    def stage(self,name,function):
        start=time.monotonic();print(json.dumps({'stage':name,'event':'start'}),flush=True)
        try:
            self.budget.check()
            self.state['stages'][name]={'status':'running','started_utc':now()}
            self.save()
            result=function()
            outcome=result.get('status') if isinstance(result,dict) else None
            record={'status':outcome if isinstance(outcome,str) and outcome.endswith('_blocked') else 'complete',
                    'result':result,'seconds':time.monotonic()-start,'checked_utc':now()}
        except (Exception,KeyboardInterrupt) as exc:
            status=('interrupted' if isinstance(exc,KeyboardInterrupt) else getattr(exc,'status',None)) or ('storage_blocked' if 'storage_blocked' in str(exc) else
                'dependency_blocked' if isinstance(exc,ImportError) else 'validation_blocked')
            record={'status':status,'error':str(exc)[:1500],'error_type':type(exc).__name__,
                    'seconds':time.monotonic()-start,'checked_utc':now()}
        rss=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        record['process_peak_rss_bytes']=rss if sys.platform=='darwin' else rss*1024
        record['memory_measurement']='Cumulative process peak for this invocation; child workers report separately'
        self.state['stages'][name]=record
        self.state.setdefault('stage_history',[]).append({'stage':name,**{key:value for key,value in record.items() if key!='result'}})
        self.save()
        print(json.dumps({'stage':name,'event':record['status'],'seconds':round(record['seconds'],2),
                         **({'error':record['error']} if 'error' in record else {})}),flush=True)
        if record['status']=='interrupted':raise KeyboardInterrupt('Stage checkpoint saved; resume command remains in state')
        return record

    def inspect(self):
        deps={}
        for name in ['numpy','scipy','shapely','pyproj','psutil','pyarrow','osmium','pytest','GDAL']:
            try: deps[name]=importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError: deps[name]='not installed'
        try:
            from osgeo import gdal
            deps['gdal_native']=gdal.VersionInfo('RELEASE_NAME')
            deps['gdal_python_module']=gdal.__file__
        except ImportError: pass
        inventory=[]
        for p in sorted(ROOT.iterdir()):
            if p.is_dir():
                logical=sum(f.stat().st_size for f in p.rglob('*') if f.is_file() and not f.is_symlink())
                inventory.append({'path':p.name,'logical_bytes_not_hardlink_deduplicated':logical})
        cgroups={}
        for key in ['memory.max','memory.current','cpu.max','pids.max']:
            p=Path('/sys/fs/cgroup')/key
            if p.exists():cgroups[key]=p.read_text().strip()
        value={'utc':now(),'git_head':os.popen('git rev-parse HEAD').read().strip(),
            'platform':platform.platform(),'python':sys.version,'dependencies':deps,'cgroup':cgroups,
            'available_memory_bytes':available_memory_bytes(),'cpu_affinity':len(os.sched_getaffinity(0)),
            'inventory':inventory,'external_data_roots':self.config['external_data_roots'],
            'storage':self.budget.check(),'quota':'No task OS quota installed or asserted; user-space policy and filesystem headroom',
            'shared_filesystem_limitation':'Unrelated writers can consume free space between checks. Conservative peak reservations and margins are not an OS quota.'}
        return value

    def boundary(self):
        from scripts.acquire_seoul_boundary import OSM_URL,OSM_SHA256
        from shapely.geometry import shape,mapping
        from shapely.ops import transform
        from pyproj import Transformer
        def validate(path):
            values=json.loads(path.read_text())
            if len(values)!=1 or values[0].get('osm_id')!=2297418 or values[0].get('osm_type')!='relation':
                raise ValueError('Boundary response is not Seoul administrative relation2297418')
            geom=shape(values[0]['geojson'])
            if geom.geom_type not in {'Polygon','MultiPolygon'} or not geom.is_valid or geom.is_empty:
                raise ValueError('Boundary geometry is missing or invalid')
            xy=transform(Transformer.from_crs(4326,5186,always_xy=True).transform,geom)
            if not 500_000_000<xy.area<700_000_000 or not (126<geom.bounds[0]<128 and 37<geom.bounds[1]<39):
                raise ValueError('Seoul boundary area/coordinate plausibility check failed')
        raw=self.data/'raw/boundary/seoul_nominatim_20260911.json'
        transfer=guarded_download(OSM_URL,raw,self.budget,max_bytes=self.config['limits']['boundary_transfer_bytes'],
            expected_sha256=self.config['boundary']['expected_response_sha256'],
            expected_size=self.config['boundary']['expected_response_bytes'],
            allowed_hosts={'nominatim.openstreetmap.org'},validator=validate,max_retries=1,
            headers={'User-Agent':'HiddenViewFinder/0.2 (https://github.com/myjju08/Hidden_View_Finder; single cached Seoul boundary)'})
        values=json.loads(raw.read_text());geom=shape(values[0]['geojson'])
        xy=transform(Transformer.from_crs(4326,5186,always_xy=True).transform,geom)
        to_ll=Transformer.from_crs(5186,4326,always_xy=True).transform
        support_xy=xy.buffer(self.config['maximum_sight_distance_m'],quad_segs=64)
        halo_xy=support_xy.buffer(self.config['terrain_interpolation_halo_m'],quad_segs=64)
        features=[]
        for role,p in [('recommendation',xy),('obstruction_support',support_xy),('terrain_interpolation_support',halo_xy)]:
            features.append({'type':'Feature','properties':{'role':role,'area_m2_epsg5186':p.area},'geometry':mapping(transform(to_ll,p))})
        extents={'type':'FeatureCollection','features':features}
        path=self.data/'geometry'/f'extents.{self.config_id}.geojson'
        atomic_json(path,extents,self.budget)
        result={'path':self.relative(path),'sha256':sha256(path),'raw_sha256':sha256(raw),
            'source_id':'OSM relation2297418','licence':'ODbL1.0','attribution':'© OpenStreetMap contributors',
            'catalogue':'https://www.openstreetmap.org/relation/2297418','transfer':transfer,
            'source_date':'Response retrieval snapshot; per-node observation dates not provided',
            'new_source_record':sha256(raw)!=OSM_SHA256,'previous_inspected_fingerprint_preserved':OSM_SHA256,
            'new_record_inspection':'Identity, EPSG4326 coordinate ranges, valid polygon and Seoul area checked; old response hash not replaced',
            'seoul_area_m2':xy.area,'support_area_m2':support_xy.area,'halo_area_m2':halo_xy.area,
            'support_bbox_lonlat':transform(to_ll,support_xy).bounds,'halo_bbox_lonlat':transform(to_ll,halo_xy).bounds,
            'maximum_sight_distance_m':self.config['maximum_sight_distance_m'],'travel_distance':'separate future straight-line filter',
            'coverage':'Administrative standing-location boundary, not public-access or data-completeness evidence'}
        return result

    def geometries(self):
        from shapely.geometry import shape
        path=self.budget.safe_path(self.data/'geometry'/f'extents.{self.config_id}.geojson')
        if not path.exists():raise FileNotFoundError(path)
        record=self.state['stages'].get('boundary',{})
        if (path.stat().st_size>8*1024**2 or record.get('status')!='complete'
                or sha256(path)!=record.get('result',{}).get('sha256')):
            raise AcquisitionError('coverage_blocked','Boundary contract is unvalidated or changed; reacquire/inspect before dependent payloads')
        values=json.loads(path.read_text())
        return {f['properties']['role']:shape(f['geometry']) for f in values['features']}

    def plan(self,online=False):
        candidate_cap=self.state.get('resource_overrides',{}).get('candidates_bytes',self.config['limits']['candidates_bytes'])
        result={'network_mode':'bounded metadata probes' if online else 'zero network',
                'policy':self.config['storage'],'storage':self.budget.check(),
                'contract':{k:self.config[k] for k in ['maximum_sight_distance_m','terrain_interpolation_halo_m','resolution_m','candidate_spacing_m']},
                'stages':{'terrain':{'transfer_bytes':45852601,'expanded_bytes':78559101,'normalized_bound':384000000},
                          'osm':{'transfer_bytes':self.config['osm']['expected_bytes'],'normalized_bound':self.config['limits']['osm_normalized_bytes']},
                          'candidates':{'artifact_cap_bytes':candidate_cap,'incremental_peak_before_margin_bytes':candidate_cap+16*1024**2,
                              'spacing_m':self.config['candidate_spacing_m'],'estimate_kind':'Enforced artifact allowance, not an inferred candidate count'},
                          'dependencies':{'incremental_peak_bound':self.config['limits']['dependency_peak_bytes']}},
                'estimates':'Bounds are simultaneous incremental bytes. Shared reservation adds at least25%; staging is included in total.',
                'terrain_buffer_status':self.config['terrain']['buffer_source_status']}
        try:g=self.geometries()
        except FileNotFoundError:
            result['geometry_status']='Boundary must be acquired/validated before download spatial selection';return result
        result['extents']={k:list(v.bounds) for k,v in g.items()}
        from pyproj import Transformer
        from shapely.geometry import box
        from shapely.ops import transform
        from scripts.data.prepare_pipeline import tile_grids,retained_preparation_bound
        support=transform(Transformer.from_crs(4326,5186,always_xy=True).transform,g['obstruction_support'])
        grids=[grid for grid in tile_grids(support.bounds,self.config['resolution_m']) if box(*grid['bounds']).intersects(support)]
        result['stages']['preparation']=retained_preparation_bound(grids,
            self.config['terrain']['max_sample_points']*256+16*1024**2,
            self.config['terrain_interpolation_halo_m'],self.config['storage']['uncertain_incremental_margin'])
        result['stages']['preparation']['planning_scope']='Geometry-only byte estimate; no GIS payload or sample-index reads. Local point-density/window planning follows a validated index during preparation.'
        if online:
            from scripts.data.buildings_pipeline import plan_buildings
            result['stages']['buildings']=plan_buildings(g['obstruction_support'].bounds,self.config['limits']['buildings_transfer_bytes'])
            atomic_json(self.data/f'building-plan.{self.config_id}.json',result['stages']['buildings'],self.budget)
        return result

    def acquire(self):
        if self.stage('boundary',self.boundary)['status']!='complete':return
        g=self.geometries()
        from scripts.data.terrain_pipeline import acquire_terrain
        self.stage('terrain',lambda:acquire_terrain(self.data/'raw/terrain',self.budget))
        self.stage('building_plan',lambda:self._building_plan(g))
        self.stage('buildings',lambda:self._buildings(g))
        self.stage('osm',self._osm)

    def _building_plan(self,g):
        from scripts.data.buildings_pipeline import plan_buildings
        value=plan_buildings(g['obstruction_support'].bounds,self.config['limits']['buildings_transfer_bytes'])
        atomic_json(self.data/f'building-plan.{self.config_id}.json',value,self.budget);return value

    def _buildings(self,g):
        from scripts.data.buildings_pipeline import acquire_buildings
        plan=json.loads((self.data/f'building-plan.{self.config_id}.json').read_text())
        return acquire_buildings(g['obstruction_support'].bounds,self.norm/'buildings.gpkg',self.budget,plan,
                                 support_wgs84=g['obstruction_support'],
                                 progress=lambda event:print(json.dumps({'stage':'buildings',**event}),flush=True))

    def _osm(self):
        g=self.geometries()  # Define/validate the contract before any PBF payload.
        c=self.config['osm'];raw=self.data/'raw/osm'
        md5=guarded_download(c['checksum_url'],raw/'snapshot.md5',self.budget,max_bytes=4096,
                             allowed_hosts={'download.geofabrik.de'},max_retries=2)
        text=(raw/'snapshot.md5').read_text()
        if text.split()[0]!=c['publisher_md5']:raise ValueError('Publisher dated PBF checksum changed; inspect a new source version')
        path=raw/Path(c['url']).name
        def validate(p):
            with p.open('rb') as stream:
                if b'OSMHeader' not in stream.read(65536):raise ValueError('PBF header magic absent')
            with p.open('rb') as stream:
                if hashlib.file_digest(stream,'md5').hexdigest()!=c['publisher_md5']:raise ValueError('Publisher PBF MD5 verification failed')
            import osmium
            reader=osmium.io.Reader(osmium.io.File(str(p),'pbf'))
            try:
                header=reader.header()
                if not header.box().valid():raise ValueError('OSM PBF geographic header is invalid')
            finally:reader.close()
        info=guarded_download(c['url'],path,self.budget,max_bytes=self.config['limits']['osm_transfer_bytes'],
            expected_size=c['expected_bytes'],allowed_hosts={'download.geofabrik.de'},validator=validate)
        import osmium
        reader=osmium.io.Reader(str(path));header=reader.header()
        timestamp=header.get('osmosis_replication_timestamp');reader.close()
        from scripts.data.osm_distribution import acquire_distribution_geometry
        distribution=acquire_distribution_geometry(raw,self.budget,g)
        return {'path':self.relative(path),'transfer':info,'publisher_md5_verified':c['publisher_md5'],
            'source_timestamp':timestamp,'licence':'ODbL1.0','attribution':'© OpenStreetMap contributors',
            'extraction_domain':distribution,
            'catalogue':c['catalogue'],'coverage':'Geofabrik country distribution; mapped object completeness unverified'}

    def normalize(self,only=None):
        try:g=self.geometries()
        except FileNotFoundError:return
        from scripts.data.terrain_pipeline import normalize_terrain
        if only in {None,'terrain'}:self.stage('terrain_normalized',lambda:normalize_terrain(self.data/'raw/terrain',self.norm/'terrain.gpkg',g['terrain_interpolation_support'],self.budget))
        if only in {None,'osm'}:self.stage('osm_normalized',lambda:self._normalize_osm(g))
        if only in {None,'candidates'}:self.stage('candidates',lambda:self._candidates(g))
        if only in {None,'coverage'}:self.stage('coverage',lambda:self.coverage(g))
        if only in {None,'package'}:self.stage('package',self.package)

    def _normalize_osm(self,g):
        from scripts.data.osm_pipeline import normalize_osm
        pbf=self.data/'raw/osm'/Path(self.config['osm']['url']).name
        inspection=pbf.with_suffix('.inspection.json')
        cap=self.config['limits']['osm_normalized_bytes']
        with self.budget.reserve(cap+32*1024**2,temporary_bytes=cap+32*1024**2,label='OSM normalization') as r:
            return normalize_osm(pbf,self.norm/'osm.gpkg',g['recommendation'],g['obstruction_support'],
                lambda n:r.check_write(n,self.norm/'osm.gpkg.part'),max_output_bytes=cap,
                memory_limit_bytes=self.config['memory']['working_bytes'],budget=self.budget,
                inspection_cache=inspection if inspection.exists() else None)

    def _candidates(self,g):
        from scripts.data.osm_pipeline import generate_candidates
        cap=self.state.get('resource_overrides',{}).get('candidates_bytes',self.config['limits']['candidates_bytes'])
        with self.budget.reserve(cap+16*1024**2,temporary_bytes=cap+16*1024**2,label='standing candidates') as r:
            return generate_candidates(self.norm/'osm.gpkg',self.norm/'buildings.gpkg','buildings',g['recommendation'],
                self.norm/'candidates.gpkg',lambda n:r.check_write(n,self.norm/'candidates.gpkg.part'),
                spacing_m=self.config['candidate_spacing_m'],max_output_bytes=cap,budget=self.budget)

    def coverage(self,g):
        from scripts.data.coverage import coverage_report
        return coverage_report(self.norm,g,self.budget,self.config)

    def readiness(self):
        stages=self.state['stages'];complete=lambda k:stages.get(k,{}).get('status')=='complete'
        acquired=all(complete(k) for k in ['boundary','terrain','buildings','osm'])
        artifacts=acquired and all(complete(k) for k in ['terrain_normalized','osm_normalized','candidates','coverage'])
        osm=stages.get('osm_normalized',{}).get('result',{}) if complete('osm_normalized') else {}
        relations= osm.get('relations',{})
        relation_complete=relations.get('relation_geometry_complete',False)
        buildings=stages.get('buildings',{}).get('result',{}) if complete('buildings') else {}
        building_invalid=buildings.get('invalid_geometry')
        normalized=(artifacts and osm.get('normalized_ready',False)
                    and osm.get('geographic_completeness_confirmed',False) and building_invalid == 0)
        return {'acquisition_ready':acquired,'normalized_ready':normalized,'visibility_ready':False,
                'normalized_artifacts_complete':artifacts,'osm_relation_geometry_complete':relation_complete,
                'building_unrepaired_invalid_geometry_count':building_invalid,
                'osm_incomplete_relation_count':len(relations.get('unassembled_relation_ids',[])) if osm else None,
                'osm_incomplete_relation_scope':'Unknown geometry remains explicit; names or country-edge metadata do not prove exclusion from requested support' if osm and not relation_complete else None,
                'support_area_terrain_complete':False,'deployment_licence_review':'required: older mixed GBA distribution, source-specific conditions and ODbL obligations',
                'readiness_definitions':{'acquisition_ready':'All selected core source artifacts acquired and validated; this does not assert the terrain buffer is supplied',
                    'normalized_ready':'All core normalized artifacts and quality reports completed without unresolved geometry errors or unlocated incomplete relations',
                    'normalized_artifacts_complete':'All core normalized artifacts and quality reports published; recorded geographic gaps may remain',
                    'visibility_ready':'Requires supported terrain and obstruction surfaces over complete modeled windows; remains false for the missing surrounding terrain'},
                'field_verified':False}

    def package(self):
        from scripts.data.package import publish_package
        return publish_package(self)

    def prepare(self):
        from scripts.data.prepare_pipeline import prepare_tiles
        return prepare_tiles(self)

    def validate(self):
        from scripts.data.package import validate_package
        pkg=self.state['stages'].get('package',{}).get('result',{}).get('path')
        if not pkg:raise ValueError('No published package available; inspect per-stage blockers')
        return validate_package(ROOT/pkg)

    def report(self):
        from scripts.data.package import aggregate_report
        report=aggregate_report(self)
        atomic_json(ROOT/'reports/citywide/acquisition.json',report,self.budget)
        return report


def main(argv=None):
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=ROOT/'configs/data/seoul_citywide.json')
    parser.add_argument('command',choices=['inspect','plan','acquire','normalize','prepare','validate','status','resume','all','boundary','terrain','osm','buildings'])
    parser.add_argument('--online',action='store_true',help='plan only: bounded remote metadata; default is zero network')
    parser.add_argument('--only',choices=['terrain','osm','candidates','coverage','package'],help='normalize only: run one independently resumable stage')
    parser.add_argument('--candidate-max-bytes',type=int,help='Persist a candidate artifact cap within unchanged shared ceilings; geographic version and spacing stay fixed')
    args=parser.parse_args(argv)
    if args.only and args.command!='normalize':parser.error('--only applies to normalize')
    p=Pipeline(args.config)
    if args.candidate_max_bytes is not None:
        if args.command not in {'normalize','resume','all'}:parser.error('--candidate-max-bytes requires normalize, resume or all')
        p.set_candidate_limit(args.candidate_max_bytes)
    if args.command=='status':
        print(json.dumps({'readiness':p.readiness(),'stages':{k:v['status'] for k,v in p.state['stages'].items()},
            'data_root':p.relative(p.data),'storage':p.budget.snapshot(),
            'resume_command':p.state['resume_command']},indent=2),flush=True)
        return 0
    with p.lock():
        p.state['commands'].append({'command':args.command,'online':args.online,'only':args.only,
            'resource_overrides':dict(p.state.get('resource_overrides',{})),'utc':now()})
        if args.command=='inspect':p.stage('inspect',p.inspect)
        elif args.command=='plan':p.stage('plan',lambda:p.plan(args.online))
        elif args.command=='boundary':p.stage('boundary',p.boundary)
        elif args.command=='terrain':
            p.geometries()
            from scripts.data.terrain_pipeline import acquire_terrain
            p.stage('terrain',lambda:acquire_terrain(p.data/'raw/terrain',p.budget))
        elif args.command=='osm':p.stage('osm',p._osm)
        elif args.command=='buildings':
            g=p.geometries();p.stage('building_plan',lambda:p._building_plan(g));p.stage('buildings',lambda:p._buildings(g))
        elif args.command=='acquire':p.acquire()
        elif args.command=='normalize':p.normalize(args.only)
        elif args.command=='prepare':p.stage('prepare',p.prepare)
        elif args.command=='validate':p.stage('validation',p.validate)
        elif args.command in {'all','resume'}:
            p.stage('inspect',p.inspect);p.acquire();p.normalize();p.stage('prepare',p.prepare)
            p.stage('validation',p.validate)
        p.save();p.report()
        print(json.dumps({'readiness':p.readiness(),'stages':{k:v['status'] for k,v in p.state['stages'].items()},
            'data_root':p.relative(p.data),'free_bytes':[x['free_bytes'] for x in p.budget.snapshot()['filesystems']],
            'resume_command':p.state['resume_command']},indent=2),flush=True)


if __name__=='__main__':main()
