"""Synthetic byte-budget fixtures; never fill disk or touch acquired inputs."""
from copy import deepcopy
import json
import os
from pathlib import Path

import pytest

from hidden_view_finder.prototype import runtime
from hidden_view_finder.prototype.image_cache import ImageCache
from seoul_visibility.acquisition_safety import Budget
from seoul_visibility.errors import ResourceBudgetError


@pytest.fixture
def setup(tmp_path,monkeypatch):
    original=json.loads((runtime.ROOT/'configs/prototype.json').read_text())
    monkeypatch.setattr(runtime,'ROOT',tmp_path)
    path=tmp_path/'config.json'
    path.write_text(json.dumps(original))
    def configured(change=None):
        value=deepcopy(original)
        if change:change(value)
        path.write_text(json.dumps(value))
        return runtime.config(path)
    return tmp_path,configured


def put(root,name,blob=b'synthetic fixture'):
    path=root/name
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(blob)
    return path


def test_lower_storage_image_limit_is_effective_in_provider_configuration(setup):
    _,configured=setup
    c=configured(lambda value:value['storage'].update(image_cache_bytes=123456))
    assert c['ai']['image_cache_bytes']==123456


def test_lower_ai_image_limit_remains_stricter(setup):
    _,configured=setup
    c=configured(lambda value:value['ai'].update(image_cache_bytes=12345))
    assert c['ai']['image_cache_bytes']==12345


def test_zero_image_cache_limit_keeps_read_only_constructor_available(setup):
    root,configured=setup
    c=configured(lambda value:value['storage'].update(image_cache_bytes=0))
    assert c['ai']['image_cache_bytes']==0
    cache=ImageCache(root/'images',Budget(root),max_bytes=c['ai']['image_cache_bytes'])
    assert cache.lookup('0'*64) is None
    assert not cache.root.exists()


@pytest.mark.parametrize('value',[250000001,True,-1])
def test_invalid_ai_cache_ceiling_rejected_even_with_stricter_storage(setup,value):
    _,configured=setup
    with pytest.raises(ResourceBudgetError,match='image cache'):
        configured(lambda config:config['ai'].update(image_cache_bytes=value))


def test_combined_artifacts_count_profiles_browser_test_logs_and_reports(setup):
    root,configured=setup
    c=configured()
    put(root,'data/prototype/staging/artifacts/run-a/desktop.png')
    put(root,'data/prototype/test-artifacts/pytest.log')
    put(root,'data/citywide/staging/prototype-browser/runtime/profile/places.sqlite')
    put(root,'data/citywide/staging/prototype-regression/pytest.log')
    put(root,'data/citywide/staging/prototype-check-123/junit.xml')
    put(root,'reports/prototype/validation.json')
    result=runtime.artifact_usage(c)
    assert result['file_count']==6
    assert result['used_bytes']>=result['logical_bytes']
    assert result['used_bytes']>=result['allocated_bytes']
    assert result['limit_bytes']==100000000 and result['over_limit'] is False
    assert set(result['categories'])>={'runtime_artifacts','browser_profiles','staging_test_artifacts','aggregate_reports'}


def test_fixture_gis_archives_and_per_case_cache_files_excluded_from_artifact_subcap(setup):
    root,configured=setup
    c=configured()
    put(root,'data/citywide/staging/prototype-regression/run-a/terrain.tif')
    put(root,'data/citywide/staging/prototype-regression/run-a/source.zip')
    put(root,'data/citywide/staging/prototype-regression/run-a/source.json')
    put(root,'data/citywide/staging/prototype-check-123/test_scene0/image.jpg')
    put(root,'data/citywide/staging/prototype-check-123/test_scene0/report.json')
    put(root,'data/citywide/staging/prototype-regression/trace-run.zip')
    assert runtime.artifact_usage(c)['file_count']==1


def test_hard_linked_artifact_counted_once(setup):
    root,configured=setup
    c=configured()
    source=put(root,'data/prototype/staging/artifacts/run-a/desktop.png',b'x'*20000)
    before=runtime.artifact_usage(c)
    os.link(source,source.with_name('same.png'))
    after=runtime.artifact_usage(c)
    assert after['file_count']==before['file_count']==1
    assert after['used_bytes']-before['used_bytes']<4096


def test_artifact_usage_honors_lower_configured_combined_cap(setup):
    root,configured=setup
    c=configured(lambda value:value['storage'].update(artifacts_bytes=100))
    put(root,'data/prototype/test-artifacts/test.log',b'x'*101)
    result=runtime.artifact_usage(c)
    assert result['limit_bytes']==100
    assert result['over_limit'] is True
    assert result['headroom_bytes']==0


def test_selected_artifact_symlink_escape_rejected(setup,tmp_path):
    root,configured=setup
    c=configured()
    artifact=put(root,'data/prototype/test-artifacts/owned.log')
    artifact.unlink()
    artifact.symlink_to(tmp_path.parent/'outside-synthetic.log')
    try:
        with pytest.raises(ResourceBudgetError,match='outside|symlink'):
            runtime.artifact_usage(c)
    finally:
        artifact.unlink()


@pytest.mark.parametrize('target_kind',['pid_marker','absolute'])
def test_browser_profile_lock_marker_counts_link_without_following_target(setup,target_kind):
    root,configured=setup
    c=configured()
    target=('12345@synthetic-host' if target_kind=='pid_marker'
            else str(root.parent/'outside-synthetic-profile-lock-target'))
    profile=root/'data/citywide/staging/prototype-browser/runtime/firefox-profile'
    profile.mkdir(parents=True)
    lock=profile/'lock'
    lock.symlink_to(target)
    try:
        info=lock.lstat()
        result=runtime.artifact_usage(c)
        assert result['file_count']==1
        assert result['categories']['browser_profiles']>=max(info.st_size,info.st_blocks*512)
        assert os.readlink(lock)==target
        assert not lock.exists()
    finally:
        lock.unlink()


def test_browser_profile_nonlock_output_link_still_rejected(setup):
    root,configured=setup
    c=configured()
    profile=root/'data/citywide/staging/prototype-browser/runtime/firefox-profile'
    profile.mkdir(parents=True)
    link=profile/'cache.sqlite'
    link.symlink_to(root.parent/'outside-synthetic-output.sqlite')
    try:
        with pytest.raises(ResourceBudgetError,match='outside|symlink'):
            runtime.artifact_usage(c)
    finally:
        link.unlink()
