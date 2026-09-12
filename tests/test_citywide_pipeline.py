"""Synthetic fixture tests: orchestration never downloads real citywide inputs."""
import copy
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.data import citywide
from scripts.data.package import validate_package, publish_package, portable
from scripts.data import package as package_module
from seoul_visibility.errors import ResourceBudgetError
from seoul_visibility.acquisition_safety import AcquisitionError


@pytest.fixture
def configured(tmp_path,monkeypatch):
    monkeypatch.setattr(citywide,'ROOT',tmp_path)
    config=json.loads((ROOT/'configs/data/seoul_citywide.json').read_text())
    path=tmp_path/'configs/data/test.json';path.parent.mkdir(parents=True);path.write_text(json.dumps(config))
    return path,config


@pytest.mark.parametrize('limit',[20*1024**3,20000000001])
def test_reject_overlimit_config_before_pipeline_writes(configured,limit):
    path,config=configured;config['storage']['total_bytes']=limit;path.write_text(json.dumps(config))
    with pytest.raises(ResourceBudgetError):citywide.Pipeline(path)
    assert not (path.parents[2]/'data').exists()


def test_zero_network_plan_without_gis_data(configured,monkeypatch):
    import urllib.request
    monkeypatch.setattr(urllib.request,'urlopen',lambda *a,**k:pytest.fail('zero-network plan attempted network'))
    path,_=configured;p=citywide.Pipeline(path);result=p.plan(False)
    assert result['network_mode']=='zero network'
    assert 'Boundary must' in result['geometry_status']
    assert not p.data.exists()


def test_geometry_only_plan_includes_effective_candidate_and_raster_bounds(configured,monkeypatch):
    import urllib.request
    from shapely.geometry import box
    from osgeo import gdal
    monkeypatch.setattr(urllib.request,'urlopen',lambda *a,**k:pytest.fail('Offline plan attempted network'))
    monkeypatch.setattr(gdal,'Open',lambda *a,**k:pytest.fail('Geometry-only plan opened a raster'))
    p=citywide.Pipeline(configured[0]);p.set_candidate_limit(805306368)
    city=box(126.98,37.49,127.02,37.51)
    monkeypatch.setattr(p,'geometries',lambda:{'recommendation':city,'obstruction_support':city.buffer(.1),
                                            'terrain_interpolation_support':city.buffer(.11)})
    result=p.plan(False)
    assert result['network_mode']=='zero network'
    assert result['stages']['candidates']['artifact_cap_bytes']==805306368
    prepared=result['stages']['preparation']
    assert prepared['requested_tile_count']>1 and prepared['cell_spacing_m']==5
    assert prepared['retained_bytes_with_uncertainty']>prepared['retained_bytes_before_uncertainty']
    assert prepared['conservative_peak_incremental_growth_bytes']>prepared['retained_bytes_with_uncertainty']
    assert result['policy']['total_bytes']==20000000000
    assert not p.data.exists()


def test_settings_change_invalidates_normalized_location(configured):
    path,config=configured;first=citywide.Pipeline(path)
    config['candidate_spacing_m']=30;path.write_text(json.dumps(config));second=citywide.Pipeline(path)
    assert first.config_id!=second.config_id and first.norm!=second.norm


def test_interrupt_saves_stage_checkpoint(configured):
    p=citywide.Pipeline(configured[0])
    def interrupted():raise KeyboardInterrupt('synthetic interruption')
    with pytest.raises(KeyboardInterrupt):p.stage('synthetic',interrupted)
    state=json.loads(p.state_path.read_text())
    assert state['stages']['synthetic']['status']=='interrupted'
    assert state['resume_command'].endswith(' resume')


def test_missing_sources_are_not_ready(configured):
    p=citywide.Pipeline(configured[0]);assert not any(p.readiness()[key] for key in ['acquisition_ready','normalized_ready','visibility_ready'])


def test_public_reports_remove_exact_root_and_external_private_paths():
    root=Path('/synthetic/project')
    assert portable({'root':str(root),'file':str(root/'data/file.gpkg'),'external':'/synthetic/private/input'},root)=={
        'root':'.','file':'data/file.gpkg','external':'<external path>'}
    assert portable('https://example.test/public',root)=='https://example.test/public'


def test_live_stage_is_checkpointed_before_work(configured):
    p=citywide.Pipeline(configured[0])
    def operation():
        assert json.loads(p.state_path.read_text())['stages']['synthetic']['status']=='running'
        return {'synthetic':True}
    assert p.stage('synthetic',operation)['status']=='complete'


def test_missing_boundary_prevents_country_payload(configured,monkeypatch):
    p=citywide.Pipeline(configured[0])
    monkeypatch.setattr(citywide,'guarded_download',lambda *a,**k:pytest.fail('Payload requested before geometry'))
    with pytest.raises(FileNotFoundError):p._osm()


def test_failed_boundary_stops_dependent_acquisition(configured,monkeypatch):
    p=citywide.Pipeline(configured[0]);seen=[]
    def stage(name,operation):
        seen.append(name);return {'status':'validation_blocked'}
    monkeypatch.setattr(p,'stage',stage)
    p.acquire()
    assert seen==['boundary']


def test_changed_extent_refuses_dependent_geometry_reuse(configured):
    p=citywide.Pipeline(configured[0]);path=p.data/'geometry'/f'extents.{p.config_id}.geojson'
    path.parent.mkdir(parents=True);path.write_text('{"features":[]}')
    p.state['stages']['boundary']={'status':'complete','result':{'sha256':'incorrect'}}
    with pytest.raises(Exception,match='Boundary contract is unvalidated or changed'):p.geometries()


def test_resource_only_candidate_limit_preserves_geographic_namespace(configured):
    p=citywide.Pipeline(configured[0]);identifier=p.config_id
    p.set_candidate_limit(768*1024**2);p.save()
    resumed=citywide.Pipeline(configured[0])
    assert resumed.config_id==identifier
    assert resumed.state['resource_overrides']['candidates_bytes']==768*1024**2
    for value in [True,-1,20*1024**3,4*1024**3+1]:
        with pytest.raises(ValueError):resumed.set_candidate_limit(value)


def test_unlocated_relation_gap_blocks_readiness_but_preserves_artifacts(configured):
    p=citywide.Pipeline(configured[0])
    for name in ['boundary','terrain','buildings','osm','terrain_normalized','osm_normalized','candidates','coverage']:
        p.state['stages'][name]={'status':'complete','result':{}}
    osm=p.state['stages']['osm_normalized']['result']
    p.state['stages']['buildings']['result']['invalid_geometry']=0
    osm.update(normalized_ready=True,geographic_completeness_confirmed=False,
               relations={'relation_geometry_complete':False,'unassembled_relation_ids':[123]})
    result=p.readiness()
    assert result['acquisition_ready'] and result['normalized_artifacts_complete']
    assert not result['normalized_ready'] and not result['visibility_ready']
    assert result['osm_incomplete_relation_count']==1
    osm.update(geographic_completeness_confirmed=True,
               relations={'relation_geometry_complete':True,'unassembled_relation_ids':[]})
    assert p.readiness()['normalized_ready']
    p.state['stages']['buildings']['result']['invalid_geometry']=7
    assert not p.readiness()['normalized_ready']
    assert p.readiness()['building_unrepaired_invalid_geometry_count']==7


def test_status_is_readonly_while_another_stage_holds_writer_lock(configured):
    p=citywide.Pipeline(configured[0])
    with p.budget.reserve(1024*1024,label='synthetic live writer'):
        assert citywide.main(['--config',str(configured[0]),'status'])==0
    assert not p.data.exists()


def test_state_checkpoint_rejects_valid_json_corruption_using_owned_checksum(configured):
    p=citywide.Pipeline(configured[0]);p.save()
    modified=json.loads(p.state_path.read_text());modified['stages']['synthetic']={'status':'complete'}
    p.state_path.write_text(json.dumps(modified))
    before=p.state_path.read_bytes()
    with pytest.raises(AcquisitionError,match='checksum'):
        citywide.Pipeline(configured[0])
    assert p.state_path.read_bytes()==before


@pytest.mark.parametrize('canonical_exists',[False,True])
def test_complete_owned_state_writer_recovers_without_losing_progress(configured,monkeypatch,canonical_exists):
    p=citywide.Pipeline(configured[0])
    if canonical_exists:p.save()
    p.state['stages']['synthetic']={'status':'complete','result':{'fixture':True}}
    p.set_candidate_limit(768*1024**2)
    original=Path.replace
    def interrupt(path,target):
        if Path(target)==p.state_path:
            raise InterruptedError('synthetic state publication interruption')
        return original(path,target)
    monkeypatch.setattr(Path,'replace',interrupt)
    with pytest.raises(InterruptedError):p.save()
    retained=next(p.data.glob(p.state_path.name+'.'+'?'*32+'.writing'))
    payload=retained.read_bytes()
    assert retained.with_name(retained.name[:-len('.writing')]+'.owner.json').exists()
    monkeypatch.setattr(Path,'replace',original)
    resumed=citywide.Pipeline(configured[0])
    assert resumed.state['stages']['synthetic']['status']=='complete'
    assert resumed.state['resource_overrides']['candidates_bytes']==768*1024**2
    resumed.save()
    assert retained.read_bytes()==payload
    final=json.loads(p.state_path.read_text())
    assert final['stages']['synthetic']['status']=='complete'
    entry=next(row for row in final['checkpoint_preservations'] if row['path']==resumed.relative(retained))
    assert entry['ownership_verified'] and entry['sha256']==citywide.sha256(retained)
    assert entry['action']=='preserved_in_place' and not entry['deletion_performed']


def test_incomplete_owned_state_writer_is_retained_before_successful_resume(configured,monkeypatch):
    p=citywide.Pipeline(configured[0]);p.save()
    original=citywide._checkpoint_payload
    def interrupted(path,content):
        with path.open('xb') as stream:stream.write(content[:31])
        raise InterruptedError('synthetic mid-checkpoint interruption')
    monkeypatch.setattr(citywide,'_checkpoint_payload',interrupted)
    with pytest.raises(InterruptedError):p.save()
    retained=next(p.data.glob(p.state_path.name+'.'+'?'*32+'.writing'))
    before=retained.read_bytes()
    monkeypatch.setattr(citywide,'_checkpoint_payload',original)
    resumed=citywide.Pipeline(configured[0]);resumed.save()
    assert retained.read_bytes()==before
    entries=json.loads(p.state_path.read_text())['checkpoint_preservations']
    assert entries[0]['ownership_verified'] and entries[0]['bytes']==31


def test_missing_canonical_with_incomplete_owned_writer_blocks_precisely(configured,monkeypatch):
    p=citywide.Pipeline(configured[0])
    def interrupted(path,content):
        with path.open('xb') as stream:stream.write(content[:17])
        raise InterruptedError('synthetic initial checkpoint interruption')
    monkeypatch.setattr(citywide,'_checkpoint_payload',interrupted)
    with pytest.raises(InterruptedError):p.save()
    retained=next(p.data.glob(p.state_path.name+'.'+'?'*32+'.writing'))
    with pytest.raises(AcquisitionError,match='Canonical state is absent.*no complete owned matching writer'):
        citywide.Pipeline(configured[0])
    assert retained.stat().st_size==17 and not p.state_path.exists()


def test_legacy_unowned_writer_preserved_without_promotion_or_overwrite(configured):
    p=citywide.Pipeline(configured[0]);p.save()
    legacy=p.state_path.with_name(p.state_path.name+'.writing')
    legacy.write_bytes(b'synthetic incomplete legacy checkpoint')
    resumed=citywide.Pipeline(configured[0]);resumed.save()
    assert legacy.read_bytes()==b'synthetic incomplete legacy checkpoint'
    entry=json.loads(p.state_path.read_text())['checkpoint_preservations'][0]
    assert not entry['ownership_verified'] and entry['action']=='preserved_in_place'
    assert not entry['deletion_performed']


def test_missing_canonical_never_promotes_unowned_legacy_writer(configured):
    p=citywide.Pipeline(configured[0]);p.data.mkdir(parents=True)
    legacy=p.state_path.with_name(p.state_path.name+'.writing')
    legacy.write_text(json.dumps(p.state))
    with pytest.raises(AcquisitionError,match='no complete owned matching writer'):
        citywide.Pipeline(configured[0])
    assert legacy.exists() and not p.state_path.exists()


def test_checkpoint_recovery_obeys_other_live_writer_lock(configured):
    p=citywide.Pipeline(configured[0]);p.save()
    other=citywide.Pipeline(configured[0]);before=p.state_path.read_bytes()
    with p.budget.reserve(1024*1024,label='synthetic live acquisition writer'):
        with pytest.raises(ResourceBudgetError,match='shared reservation lock'):
            other.save()
    assert p.state_path.read_bytes()==before


def test_stale_state_invocation_cannot_overwrite_newer_canonical(configured):
    original=citywide.Pipeline(configured[0]);original.save()
    stale=citywide.Pipeline(configured[0])
    original.state['stages']['synthetic']={'status':'complete'};original.save()
    before=original.state_path.read_bytes()
    with pytest.raises(AcquisitionError,match='changed since this invocation'):
        stale.save()
    assert original.state_path.read_bytes()==before


def test_package_rejects_escaped_manifest_paths(tmp_path):
    (tmp_path/'manifest.json').write_text(json.dumps({'artifacts':[{'path':'../outside','bytes':0,'sha256':'x'}]}))
    with pytest.raises(ValueError,match='escapes'):validate_package(tmp_path)


def test_offline_package_rejects_corruption(tmp_path):
    import hashlib
    path=tmp_path/'source.json';path.write_text('{}')
    manifest={'artifacts':[{'path':path.name,'bytes':2,'sha256':hashlib.sha256(b'{}').hexdigest()}],
              'readiness':{'visibility_ready':False}}
    (tmp_path/'manifest.json').write_text(json.dumps(manifest))
    assert validate_package(tmp_path)['network_requests']==0
    path.write_text('[]')
    with pytest.raises(ValueError,match='checksum'):validate_package(tmp_path)


def test_package_uses_receipts_and_versions_readiness(configured):
    # Synthetic polygon and untrusted file, with no real GIS downloads.
    p=citywide.Pipeline(configured[0]);p.norm.mkdir(parents=True)
    (p.norm/'untrusted.gpkg').write_bytes(b'not a validated input')
    extent=p.data/'geometry'/f'extents.{p.config_id}.geojson';extent.parent.mkdir()
    extent.write_text(json.dumps({'type':'FeatureCollection','features':[{'type':'Feature','properties':{},
        'geometry':{'type':'Polygon','coordinates':[[[0,0],[1,0],[1,1],[0,0]]]}}]}))
    p.state['stages']['boundary']={'status':'complete','result':{'sha256':citywide.sha256(extent)}}
    first=publish_package(p)
    assert not (p.config_path.parents[2]/first['path']/'untrusted.gpkg').exists()
    assert publish_package(p)['reused'] is True
    previous=p.readiness()
    p.readiness=lambda:{**previous,'deployment_licence_review':'synthetic changed review status'}
    second=publish_package(p)
    assert first['path']!=second['path']
    p.state['stages']['buildings']={'status':'complete','result':{'sha256':'synthetic-invalid'}}
    with pytest.raises(ValueError,match='durable artifact receipt'):publish_package(p)


def _boundary_package_fixture(configured):
    """Small synthetic polygon; no GIS library, real source, or network required."""
    p=citywide.Pipeline(configured[0])
    extent=p.data/'geometry'/f'extents.{p.config_id}.geojson'
    extent.parent.mkdir(parents=True)
    extent.write_text(json.dumps({'type':'FeatureCollection','features':[{'type':'Feature','properties':{},
        'geometry':{'type':'Polygon','coordinates':[[[0,0],[1,0],[1,1],[0,0]]]}}]}))
    p.state['stages']['boundary']={'status':'complete','result':{'sha256':citywide.sha256(extent)}}
    return p,extent


def test_package_resume_after_first_hardlink_preserves_source_and_timestamp(configured,monkeypatch):
    p,source=_boundary_package_fixture(configured)
    original=package_module.os.link
    def interrupt(source,target,**kwargs):
        original(source,target,**kwargs)
        raise InterruptedError('synthetic interruption after first hardlink')
    monkeypatch.setattr(package_module.os,'link',interrupt)
    with pytest.raises(InterruptedError):publish_package(p)
    staging=next((p.data/'packages').glob('*.staging'))
    owner=next((p.data/'packages').glob('*.owner.json'))
    expected=json.loads(owner.read_text())['manifest']['created_utc']
    inode=(staging/'extents.geojson').stat().st_ino
    assert inode==source.stat().st_ino
    monkeypatch.setattr(package_module.os,'link',original)
    # Volatile inspection timings may change on resume; the verified owner keeps
    # its original sources.json snapshot and current pinned source identity.
    p.state['stages']['boundary']['seconds']=2.0
    result=publish_package(p)
    final=p.config_path.parents[2]/result['path']
    assert result['resumed_staging'] and not staging.exists()
    assert (final/'extents.geojson').stat().st_ino==inode
    assert json.loads((final/'manifest.json').read_text())['created_utc']==expected
    assert validate_package(final)['integrity_valid']


@pytest.mark.parametrize('complete_writer',[False,True])
def test_package_resume_metadata_writer_preserves_or_promotes_exact_content(configured,monkeypatch,complete_writer):
    p,_=_boundary_package_fixture(configured)
    original=package_module.atomic_json
    def interrupt(path,value,budget):
        if Path(path).name=='sources.json':
            partial=Path(path).with_name('sources.json.writing')
            payload=package_module._json_bytes(value) if complete_writer else b'{"synthetic_interrupted":'
            budget._reservation.check_write(len(payload),partial)
            partial.write_bytes(payload)
            raise InterruptedError('synthetic interrupted metadata writer')
        original(path,value,budget)
    monkeypatch.setattr(package_module,'atomic_json',interrupt)
    with pytest.raises(InterruptedError):publish_package(p)
    staging=next((p.data/'packages').glob('*.staging'))
    retained=(staging/'sources.json.writing').read_bytes()
    monkeypatch.setattr(package_module,'atomic_json',original)
    result=publish_package(p)
    final=p.config_path.parents[2]/result['path']
    assert not (final/'sources.json.writing').exists()
    assert validate_package(final)['integrity_valid']
    if complete_writer:
        assert (final/'sources.json').read_bytes()==retained
        assert result['recovery_actions']==[{'path':'sources.json','action':'completed matching metadata writer'}]
    else:
        attempts=list((p.data/'packages').glob('*.partial'))
        assert len(attempts)==1
        assert (attempts[0]/'sources.json.writing').read_bytes()==retained
        receipt=json.loads((attempts[0]/'preservation.json').read_text())
        assert receipt['deletion_performed'] is False
        assert result['recovery_actions'][0]['sha256']==receipt['sha256']


def test_package_resume_fully_validated_staging_before_directory_rename(configured,monkeypatch):
    p,_=_boundary_package_fixture(configured)
    original=Path.replace
    def interrupt(path,target):
        if path.name.endswith('.staging'):
            raise InterruptedError('synthetic interruption before directory publication')
        return original(path,target)
    monkeypatch.setattr(Path,'replace',interrupt)
    with pytest.raises(InterruptedError):publish_package(p)
    staging=next((p.data/'packages').glob('*.staging'))
    assert validate_package(staging)['integrity_valid']
    expected=(staging/'manifest.json').read_bytes()
    monkeypatch.setattr(Path,'replace',original)
    result=publish_package(p)
    final=p.config_path.parents[2]/result['path']
    assert result['resumed_staging'] and (final/'manifest.json').read_bytes()==expected


def test_package_corrupt_staged_artifact_refused_without_modifying_source(configured,monkeypatch):
    p,source=_boundary_package_fixture(configured)
    source_hash=citywide.sha256(source)
    original=package_module.os.link
    def interrupt(source,target,**kwargs):
        original(source,target,**kwargs)
        raise InterruptedError('synthetic interruption')
    monkeypatch.setattr(package_module.os,'link',interrupt)
    with pytest.raises(InterruptedError):publish_package(p)
    staging=next((p.data/'packages').glob('*.staging'))
    unrelated=staging.parent/'synthetic-corrupt-fixture'
    unrelated.write_bytes(b'corrupt synthetic replacement')
    unrelated.replace(staging/'extents.geojson')
    monkeypatch.setattr(package_module.os,'link',original)
    with pytest.raises(ValueError,match='checksum/size mismatch'):publish_package(p)
    assert (staging/'extents.geojson').read_bytes()==b'corrupt synthetic replacement'
    assert citywide.sha256(source)==source_hash


def test_package_unregistered_path_and_missing_ownership_refuse_recovery(configured,monkeypatch):
    p,_=_boundary_package_fixture(configured)
    original=package_module.os.link
    def interrupt(source,target,**kwargs):
        original(source,target,**kwargs)
        raise InterruptedError('synthetic interruption')
    monkeypatch.setattr(package_module.os,'link',interrupt)
    with pytest.raises(InterruptedError):publish_package(p)
    monkeypatch.setattr(package_module.os,'link',original)
    staging=next((p.data/'packages').glob('*.staging'))
    user_file=staging/'user-note.txt';user_file.write_text('preserved synthetic user file')
    with pytest.raises(ValueError,match='Unregistered'):publish_package(p)
    assert user_file.read_text()=='preserved synthetic user file'
    next((p.data/'packages').glob('*.owner.json')).rename(p.data/'packages'/'synthetic-owner-backup.json')
    with pytest.raises(ValueError,match='Unowned package'):publish_package(p)
    assert user_file.exists()
