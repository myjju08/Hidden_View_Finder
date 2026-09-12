"""Atomic, portable packages and small public aggregate acquisition reports."""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import uuid

from seoul_visibility.acquisition_safety import atomic_json, sha256

MIB = 1024**2
MAX_PACKAGE_METADATA = 8 * MIB


def _json_bytes(value):
    data = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()
    if len(data) > MAX_PACKAGE_METADATA:
        raise ValueError('Package metadata exceeds bounded 8 MiB allowance')
    return data


def _read_json(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_PACKAGE_METADATA:
        raise ValueError('Package metadata is unsafe, missing or exceeds bounded size')
    return json.loads(path.read_text())


def _fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(descriptor)
    finally: os.close(descriptor)


def _new_json(path, value, budget, reservation):
    """Create a new durable receipt; orphaned unique writers are never deleted."""
    path = budget.safe_path(path)
    if path.exists():
        raise ValueError('New package receipt path already exists; preserved')
    data = _json_bytes(value)
    temporary = budget.safe_path(path.with_name(path.name + '.' + uuid.uuid4().hex + '.writing'))
    reservation.check_write(len(data) + 16384, temporary)
    path.parent.mkdir(parents=True, exist_ok=True)
    with temporary.open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    if path.exists():
        raise ValueError('Package receipt appeared during publication; files preserved')
    temporary.replace(path)
    _fsync_directory(path.parent)


def _metadata_record(name, value):
    data = _json_bytes(value)
    return {'path': name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


def _verify_record(path, record):
    if (path.is_symlink() or not path.is_file() or path.stat().st_size != record['bytes']
            or sha256(path) != record['sha256']):
        raise ValueError(f'Package checksum/size mismatch; existing file preserved: {path.name}')


def _preserve_fragment(path, plan, budget, reservation, recoveries):
    """Move only a declared owned metadata writer after validated sources remain."""
    path = budget.safe_path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_PACKAGE_METADATA:
        raise ValueError('Unsafe or oversized interrupted metadata preserved in place')
    attempt = budget.safe_path(path.parent.parent / ('.' + plan['package_id'] + '.interrupted-' + uuid.uuid4().hex + '.partial'))
    reservation.check_write(32768, attempt)
    attempt.mkdir(exist_ok=False)
    record = {'schema': 'citywide-preserved-package-fragment-v1', 'task_owned': True,
              'source_identity_sha256': plan['source_identity_sha256'],
              'original_path': f".{plan['package_id']}.staging/{path.name}",
              'preserved_path': path.name, 'bytes': path.stat().st_size, 'sha256': sha256(path),
              'deletion_performed': False, 'reason': 'Owned incomplete/duplicate metadata writer; verified source artifacts retained'}
    _new_json(attempt / 'preservation.json', record, budget, reservation)
    path.replace(attempt / path.name)
    _fsync_directory(attempt)
    recoveries.append({'directory': attempt.name, **record})


def _package_json(path, value, plan, budget, reservation, recoveries):
    """Reuse exact metadata, finish complete writers, preserve incomplete writers."""
    path = budget.safe_path(path)
    writing = budget.safe_path(path.with_name(path.name + '.writing'))
    expected = _metadata_record(path.name, value)
    if path.exists():
        _verify_record(path, expected)
        if writing.exists():
            _preserve_fragment(writing, plan, budget, reservation, recoveries)
        return
    if writing.exists():
        if writing.stat().st_size == expected['bytes'] and sha256(writing) == expected['sha256']:
            reservation.check_write(8192, path)
            with writing.open('rb') as stream: os.fsync(stream.fileno())
            writing.replace(path)
            _fsync_directory(path.parent)
            recoveries.append({'path': path.name, 'action': 'completed matching metadata writer'})
            return
        _preserve_fragment(writing, plan, budget, reservation, recoveries)
    atomic_json(path, value, budget)


def _check_package_directory(directory, records, metadata, budget, *, complete=False):
    directory = budget.safe_path(directory)
    if not directory.is_dir():
        raise ValueError('Package path is not a directory')
    expected = {item['path']: item for item in records}
    expected.update({name: _metadata_record(name, value) for name, value in metadata.items()})
    allowed = set(expected) | ({name + '.writing' for name in metadata} if not complete else set())
    for number, path in enumerate(directory.iterdir(), 1):
        if number > 64 or path.name not in allowed:
            raise ValueError('Unregistered package path preserved; recovery refused')
        budget.safe_path(path)
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError('Non-regular package path preserved; recovery refused')
        if path.name in expected:
            _verify_record(path, expected[path.name])
        elif path.stat().st_size > MAX_PACKAGE_METADATA:
            raise ValueError('Interrupted package metadata exceeds bounded allowance')
    if complete and set(path.name for path in directory.iterdir()) != set(expected):
        raise ValueError('Completed package is missing declared files')


def _manifest(package_id, identity_hash, records, readiness, config, created):
    return {'schema_version': 1, 'package_id': package_id, 'created_utc': created,
        'source_identity_sha256': identity_hash,
        'artifacts': records, 'readiness': readiness, 'crs': 'EPSG:5186; GeoJSON interfaces EPSG:4326 longitude/latitude',
        'contract': {'maximum_sight_distance_m': config['maximum_sight_distance_m'],
            'terrain_interpolation_halo_m': config['terrain_interpolation_halo_m'],
            'candidate_spacing_m': config['candidate_spacing_m'], 'planned_resolution_m': config['resolution_m']},
        'limitations': ['Terrain support outside official Seoul source remains missing.',
            'Dataset distribution coverage does not certify real-world completeness.',
            'Mapped access is unverified; no candidate is claimed scenic, visible, open, or field-verified.',
            'Building heights are estimated metres above ground; no roof elevation or terrain datum conversion inferred.',
            'Raw recovery inputs and runtime dependencies are development-only and outside this deployable subset.']}


def _verify_plan(plan, identity_hash, package_id, records, extras, readiness, config):
    unsigned = {key: value for key, value in plan.items() if key != 'plan_sha256'}
    if (plan.get('schema') != 'citywide-owned-package-v1' or plan.get('task_owned') is not True
            or plan.get('package_id') != package_id or plan.get('source_identity_sha256') != identity_hash
            or plan.get('source_artifacts') != records
            or plan.get('plan_sha256') != hashlib.sha256(_json_bytes(unsigned)).hexdigest()):
        raise ValueError('Package ownership/source recipe mismatch; interrupted files preserved')
    metadata = plan.get('metadata', {})
    if set(metadata) != set(extras):
        raise ValueError('Package ownership metadata schema mismatch')
    # sources.json freezes the original invocation's checks/timings. Its owned
    # snapshot stays valid across retries; artifact receipts and source identity
    # are independently compared to the newly verified source bytes above.
    if any(metadata[name] != value for name, value in extras.items() if name != 'sources.json'):
        raise ValueError('Package ownership pinned metadata mismatch')
    manifest = plan.get('manifest', {})
    created = manifest.get('created_utc')
    if not isinstance(created, str):
        raise ValueError('Package ownership lacks original publication timestamp')
    datetime.fromisoformat(created)
    expected_records = records + [_metadata_record(name, value) for name, value in sorted(metadata.items())]
    if manifest != _manifest(package_id, identity_hash, expected_records, readiness, config, created):
        raise ValueError('Package ownership manifest mismatch')
    return metadata


def portable(value,root):
    """Strip machine-specific absolute paths from public provenance records."""
    if isinstance(value,dict):
        return {k:portable(v,root) for k,v in value.items()
                if k not in {'gdal_python_module','roots','minimum_observed_free'}}
    if isinstance(value,(list,tuple)):return [portable(v,root) for v in value]
    if isinstance(value,str):
        if value==str(root):return '.'
        if str(root)+'/' in value:return value.replace(str(root)+'/','')
        if value.startswith('/'):return '<external path>'
    return value


def validate_package(directory):
    directory=Path(directory)
    if directory.is_symlink():raise ValueError('Package directory symlink is not permitted')
    directory=directory.resolve()
    manifest=_read_json(directory/'manifest.json')
    if not isinstance(manifest.get('artifacts'),list) or len(manifest['artifacts'])>64:
        raise ValueError('Package manifest artifact list is missing or exceeds its bounded allowance')
    checked=[]
    for item in manifest['artifacts']:
        relative=Path(item['path'])
        path=directory/relative
        if relative.is_absolute() or '..' in relative.parts or path.is_symlink() or not path.resolve().is_relative_to(directory):
            raise ValueError('Package resource escapes portable directory')
        if item['path'] in checked:raise ValueError('Duplicate package artifact path')
        if path.stat().st_size!=item['bytes'] or sha256(path)!=item['sha256']:
            raise ValueError(f'Package checksum/size mismatch: {relative}')
        if path.suffix=='.gpkg':
            db=sqlite3.connect(f'file:{path}?mode=ro',uri=True)
            try:
                if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('Package SQLite corruption')
                layers=db.execute('SELECT table_name,srs_id FROM gpkg_geometry_columns').fetchall()
                if not layers or any(crs!=5186 for _,crs in layers):raise ValueError('Package geometry CRS mismatch')
            finally:db.close()
        if path.suffix=='.geojson':
            from shapely.geometry import shape
            value=json.loads(path.read_text())
            if value.get('type')!='FeatureCollection' or not value.get('features'):
                raise ValueError('Invalid package geometry collection')
            for feature in value['features']:
                geometry=shape(feature['geometry'])
                if not geometry.is_valid or geometry.is_empty or geometry.geom_type not in {'Polygon','MultiPolygon'}:
                    raise ValueError('Invalid package extent polygon')
        if path.suffix=='.poly':
            from scripts.data.osm_distribution import parse_poly
            parse_poly(path.read_text(encoding='utf-8'))
        checked.append(item['path'])
    if not checked:raise ValueError('Empty geographic package')
    return {'integrity_valid':True,'validated_files':checked,'readiness':manifest['readiness'],
            'coverage_validation_is_separate':True,'network_requests':0}


def publish_package(pipeline):
    p=pipeline;root=p.config_path.parents[2]
    files=[];receipts={}
    for name,stage in [('terrain','terrain_normalized'),('buildings','buildings'),
                       ('osm','osm_normalized'),('candidates','candidates'),('coverage','coverage')]:
        record=p.state['stages'].get(stage,{})
        if record.get('status')!='complete':continue
        path=p.budget.safe_path(p.norm/f'{name}.gpkg')
        receipt_path=p.budget.safe_path(p.norm/('coverage.json' if name=='coverage' else f'{name}.source.json'))
        if not path.is_file() or not receipt_path.is_file():
            raise ValueError(f'Completed {name} stage lacks its durable artifact receipt')
        receipt=_read_json(receipt_path)
        digest=sha256(path)
        if receipt.get('sha256')!=digest or record['result'].get('sha256')!=digest:
            raise ValueError(f'{name} receipt/stage checksum mismatch; publication refused')
        files.append(path);receipts[receipt_path.name]=portable(receipt,root)
    extents=p.budget.safe_path(p.data/'geometry'/f'extents.{p.config_id}.geojson')
    boundary=p.state['stages'].get('boundary',{})
    if boundary.get('status')=='complete':
        if not extents.exists() or sha256(extents)!=boundary['result']['sha256']:
            raise ValueError('Boundary stage fingerprint mismatch; publication refused')
        files.append(extents)
    osm_record=p.state['stages'].get('osm',{})
    domain=osm_record.get('result',{}).get('extraction_domain')
    if osm_record.get('status')=='complete' and domain:
        domain_path=p.budget.safe_path(root/domain['path'])
        if not domain_path.is_file() or sha256(domain_path)!=domain['sha256']:
            raise ValueError('OSM extraction-domain fingerprint mismatch; publication refused')
        files.append(domain_path)
    if not files:raise ValueError('No validated normalized geographic files available to package')
    records=[{'path':'extents.geojson' if f==extents else f.name,'sha256':sha256(f),'bytes':f.stat().st_size} for f in sorted(files)]
    if len({record['path'] for record in records}) != len(records):
        raise ValueError('Package input basenames collide; source files preserved')
    provenance={k:portable(v,root) for k,v in p.state['stages'].items()
                if k in {'boundary','terrain','buildings','osm','terrain_normalized','osm_normalized','candidates','coverage'}}
    licences={
        'terrain':{'licence':'KOGL Type1','attribution':'Seoul Metropolitan Government / NGII,2023 contours and spot heights',
            'url':'https://data.seoul.go.kr/dataList/OA-22241/F/1/datasetView.do'},
        'osm':{'licence':'ODbL1.0','attribution':'© OpenStreetMap contributors','url':'https://www.openstreetmap.org/copyright'},
        'buildings':{'licence':'Source-specific ODbL footprints / CC BY-NC4.0 height and other footprints',
            'url':'https://github.com/zhu-xlab/GlobalBuildingAtlas',
            'distribution':'https://source.coop/tge-labs/globalbuildingatlas-lod1',
            'deployment_status':'review required; public availability of older combined conversion is not blanket redistribution permission'}}
    build_config={**p.config,'effective_resource_overrides':dict(p.state.get('resource_overrides',{}))}
    extras={'sources.json':provenance,'licences.json':licences,'build-config.json':build_config,**receipts}
    # Durable receipts capture input identities and processing recipes. Include
    # configuration/readiness as well, so identical GIS bytes cannot revive a
    # manifest with obsolete provenance or readiness claims.
    identity={'artifacts':records,'receipts':receipts,'configuration':build_config,
              'readiness':p.readiness(),'publisher_implementation_sha256':sha256(Path(__file__))}
    identity_hash=hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
    final=p.budget.safe_path(p.data/'packages'/f"{p.config['package_version']}-{identity_hash[:12]}")
    temporary=p.budget.safe_path(final.with_name('.'+final.name+'.staging'))
    owner=p.budget.safe_path(final.with_name('.'+final.name+'.owner.json'))
    plan=None
    if owner.exists():
        plan=_read_json(owner)
        extras=_verify_plan(plan,identity_hash,final.name,records,extras,p.readiness(),p.config)
    elif final.exists() or temporary.exists():
        raise ValueError('Unowned package directory preserved; verify ownership before recovery')
    if plan is None:
        manifest=_manifest(final.name,identity_hash,
            records+[_metadata_record(name,value) for name,value in sorted(extras.items())],
            p.readiness(),p.config,datetime.now(timezone.utc).isoformat())
        plan={'schema':'citywide-owned-package-v1','task_owned':True,'package_id':final.name,
              'source_identity_sha256':identity_hash,'source_artifacts':records,
              'metadata':extras,'manifest':manifest}
        plan['plan_sha256']=hashlib.sha256(_json_bytes(plan)).hexdigest()
    planned_metadata={**extras,'manifest.json':plan['manifest']}
    if final.exists():
        _check_package_directory(final,records,planned_metadata,p.budget,complete=True)
        result=validate_package(final)
        return {'path':p.relative(final),'bytes':sum(x.stat().st_size for x in final.iterdir()),'validation':result,'reused':True}
    logical=sum(r['bytes'] for r in records)
    if logical>p.config['storage']['deployable_target_bytes']:
        raise ValueError('Normalized package exceeds configured8GiB deployable design target; needs explicit partitioning')
    # Existing staging/fragments remain in the baseline. Hardlinks add no GIS
    # payload duplication; reserve metadata + its atomic writers + recovery
    # journals conservatively. Shared budget adds its mandatory 25% margin.
    metadata_bytes=sum(len(_json_bytes(value)) for value in planned_metadata.values())
    metadata_peak=3*metadata_bytes+2*len(_json_bytes(plan))+16*MIB
    recoveries=[]
    resumed=temporary.exists()
    with p.budget.reserve(metadata_peak,temporary_bytes=logical+metadata_peak,label='atomic package publication') as reservation:
        if not owner.exists():
            _new_json(owner,plan,p.budget,reservation)
        if not temporary.exists():
            reservation.check_write(8192,temporary)
            temporary.mkdir(parents=True)
            _fsync_directory(temporary.parent)
        _check_package_directory(temporary,records,planned_metadata,p.budget)
        for source,record in zip(sorted(files),records):
            target=p.budget.safe_path(temporary/record['path'])
            _verify_record(source,record)
            if target.exists():
                _verify_record(target,record)
                continue
            if source.stat().st_dev != temporary.stat().st_dev:
                raise ValueError('Package hardlink publication requires the same filesystem; no unbudgeted copying')
            reservation.check_write(8192,target)
            os.link(source,target,follow_symlinks=False)
        for name,value in sorted(planned_metadata.items(),key=lambda item:(item[0]=='manifest.json',item[0])):
            _package_json(temporary/name,value,plan,p.budget,reservation,recoveries)
        _check_package_directory(temporary,records,planned_metadata,p.budget,complete=True)
        checked=validate_package(temporary)
        reservation.check_write(8192,final)
        _fsync_directory(temporary)
        if final.exists():
            raise ValueError('Final package appeared during publication; both directories preserved')
        temporary.replace(final)
        _fsync_directory(final.parent)
    return {'path':p.relative(final),'bytes':sum(x.stat().st_size for x in final.iterdir()),
            'validation':checked,'publication':'same-filesystem atomic directory rename; data files hardlinked to validated normalized inputs',
            'resumed_staging':resumed,'recovery_actions':recoveries,'ownership_receipt':p.relative(owner)}


def aggregate_report(p):
    root=p.config_path.parents[2]
    categories={}
    for name in ['raw','dependencies','normalized','prepared','staging','packages']:
        base=p.data/name
        categories[name]=sum(x.stat().st_size for x in base.rglob('*') if x.is_file() and not x.is_symlink()) if base.exists() else 0
    network=0
    for file in (p.data/'raw').rglob('*.source.json'):
        value=json.loads(file.read_text());network+=int(value.get('received_bytes',0))
    for file in (p.data/'dependencies').rglob('*.source.json'):
        value=json.loads(file.read_text());network+=int(value.get('received_bytes',0))
    buildings=p.state['stages'].get('buildings',{}).get('result',{})
    network+=int(buildings.get('http_bytes_downloaded',buildings.get('network_bytes',0)))
    startup_path=root/'reports/citywide/startup.json'
    startup=json.loads(startup_path.read_text()) if startup_path.exists() else None
    report={'schema_version':1,'updated_utc':datetime.now(timezone.utc).isoformat(),'readiness':p.readiness(),
        'resource_overrides':dict(p.state.get('resource_overrides',{})),
        'stages':p.state['stages'],'implementation_files_sha256':p.state.get('implementation_files_sha256',{}),
        'storage':{'startup_shell_inventory':startup,'initial_pipeline_snapshot':p.state['initial_storage'],'final':p.budget.snapshot(),
            'logical_bytes_by_category':categories,'category_note':'Category totals include package hardlinks; shared total deduplicates inodes and uses max(logical,allocated).',
            'recorded_network_bytes_lower_bound':network,
            'network_scope':'Retained download sidecars plus latest building-subset invocation only. Prior interrupted/uncommitted building reads and some metadata probes were not completely metered; this is not exact total network traffic.',
            'dependencies_bytes':categories['dependencies'],'cleanup':p.state.get('cleanup',[])+
                p.state['stages'].get('prepare',{}).get('result',{}).get('cleanup',[]),
            'additional_accounted_bytes_explanation':p.state.get('additional_accounted_bytes_explanation'),
            'peak_note':'Sampled guards and reservations; unrelated shared-disk writes cannot be prevented by polling.'},
        'reproduction':{'clean':'bash scripts/data/python.sh scripts/data/citywide.py all'+(
            ' --candidate-max-bytes '+str(p.state['resource_overrides']['candidates_bytes'])
            if p.state.get('resource_overrides',{}).get('candidates_bytes') else ''),
            'offline_validation':'bash scripts/data/python.sh scripts/data/citywide.py validate',
            'resume':p.state['resume_command'],'metadata_only':'bash scripts/data/python.sh scripts/data/citywide.py plan --online',
            'zero_network_plan':'python scripts/data/citywide.py plan'},
        'commands_executed':p.state['commands'],'stage_history':p.state.get('stage_history',[]),
        'optional_context':{'weather':'deferred; no keys or history required',
            'crowds':'unknown; absence is not quietness','public_access':'mapped evidence only'}}
    return portable(report,root)
