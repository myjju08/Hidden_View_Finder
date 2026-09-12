"""Synthetic offline migration fixtures; no acquisition or citywide processing."""
from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import sqlite3

import pytest

from scripts.data import prepare_resources as reuse
from scripts.data import prepare_pipeline as prepare
from seoul_visibility.acquisition_safety import Budget, sha256
from seoul_visibility.errors import ResourceBudgetError


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2))


@pytest.fixture
def legacy_fixture(tmp_path, monkeypatch):
    old = {
        'source_sha256': 'a' * 64, 'buildings_sha256': 'b' * 64,
        'geometry_sha256': 'c' * 64, 'processing_version': reuse.LEGACY_VERSION,
        'resolution_m': 5, 'tile_metres': 5000, 'footprint_terrain_halo_m': 10,
        'surface_native_stage_cap_bytes': 480 * 1024**2,
        'building_base_method': 'maximum supported whole-footprint DTM; estimated AGL remains estimated',
        'settings': {'kind': 'samples', 'tile_size': 256, 'halo_m': 10,
            'max_points_per_tile': 100000, 'sample_spacing_m': 10,
            'contour_sampling': 'regular_arclength', 'max_triangle_edge_m': 5,
            'max_sample_points': 3000000, 'max_feature_vertices': 200000,
            'max_validation_spots': 0, 'sample_index_max_bytes': 1024**2,
            'sources': [{'layer': 'contours', 'elevation_field': 'elevation_m', 'crs': 'EPSG:5186'}]},
        'implementation_sha256': {'scripts/data/prepare_pipeline.py': 'd' * 64,
            'src/seoul_visibility/terrain.py': 'e' * 64,
            'src/seoul_visibility/prepare.py': 'f' * 64},
        'dependencies': {'gdal': 'synthetic-gdal', 'numpy': 'synthetic-numpy',
            'scipy': 'synthetic-scipy', 'python': [3, 12, 13]}}
    recipe_hash = prepare._key(old)
    data = tmp_path / 'data'
    directory = data / 'prepared' / recipe_hash[:16]
    plan = {'recipe': old, 'recipe_sha256': recipe_hash}
    plan_path = directory / 'plan.json'
    write_json(plan_path, plan)
    monkeypatch.setattr(reuse, 'LEGACY_RECIPE', recipe_hash)
    monkeypatch.setattr(reuse, 'LEGACY_PLAN_SHA256', sha256(plan_path))
    monkeypatch.setattr(reuse, 'LEGACY_ORCHESTRATOR', old['implementation_sha256']['scripts/data/prepare_pipeline.py'])
    current = copy.deepcopy(old)
    current['processing_version'] = reuse.VERSION
    current['window_selection_policy'] = reuse.window_policy(current['settings'])
    current['implementation_sha256']['scripts/data/prepare_pipeline.py'] = '1' * 64
    current['implementation_sha256']['scripts/data/prepare_resources.py'] = '2' * 64
    return {'data': data, 'directory': directory, 'plan_path': plan_path,
            'old': old, 'new': current, 'recipe_hash': recipe_hash,
            'budget': Budget(tmp_path, stage_root=tmp_path / 'staging'),
            'new_hash': prepare._key(current)}


def test_only_audited_coordinator_transition_is_approved(legacy_fixture):
    fixture = legacy_fixture
    result = reuse.approved_legacy(fixture['new'], fixture['data'])
    assert result['directory'] == fixture['directory']
    assert result['recipe_sha256'] == fixture['recipe_hash']
    assert result['plan_sha256'] == sha256(fixture['plan_path'])


@pytest.mark.parametrize(('path', 'changed'), [
    (('source_sha256',), 'changed-terrain'),
    (('buildings_sha256',), 'changed-buildings'),
    (('geometry_sha256',), 'changed-extent'),
    (('resolution_m',), 10),
    (('tile_metres',), 10000),
    (('footprint_terrain_halo_m',), 20),
    (('settings', 'halo_m'), 20),
    (('settings', 'sample_spacing_m'), 20),
    (('settings', 'max_triangle_edge_m'), 10),
    (('settings', 'max_points_per_tile'), 200000),
    (('settings', 'tile_size'), 128),
    (('implementation_sha256', 'src/seoul_visibility/terrain.py'), 'different-native'),
    (('implementation_sha256', 'src/seoul_visibility/prepare.py'), 'different-native'),
    (('dependencies', 'gdal'), 'different-gdal'),
    (('dependencies', 'scipy'), 'different-scipy'),
    (('building_base_method',), 'centroid'),
])
def test_semantic_or_native_mismatch_refuses_reuse(legacy_fixture, path, changed):
    value = copy.deepcopy(legacy_fixture['new'])
    target = value
    for key in path[:-1]: target = target[key]
    target[path[-1]] = changed
    with pytest.raises(ValueError, match='incompatible|window policy'):
        reuse.approved_legacy(value, legacy_fixture['data'])


def test_unapproved_window_policy_and_processing_version_refused(legacy_fixture):
    current = copy.deepcopy(legacy_fixture['new'])
    current['window_selection_policy']['minimum_pixels'] = 1
    with pytest.raises(ValueError, match='window policy'):
        reuse.approved_legacy(current, legacy_fixture['data'])
    current = copy.deepcopy(legacy_fixture['new'])
    current['processing_version'] = 'unreviewed-version'
    with pytest.raises(ValueError, match='version is not audited'):
        reuse.approved_legacy(current, legacy_fixture['data'])


def test_corrupt_plan_and_rehashed_mismatched_recipe_refused(legacy_fixture, monkeypatch):
    path = legacy_fixture['plan_path']
    original = path.read_bytes()
    path.write_bytes(original + b' ')
    with pytest.raises(ValueError, match='plan changed'):
        reuse.approved_legacy(legacy_fixture['new'], legacy_fixture['data'])
    changed = json.loads(original)
    changed['recipe']['resolution_m'] = 10
    write_json(path, changed)
    # Matching a newly computed file fingerprint cannot repair a recipe mismatch.
    monkeypatch.setattr(reuse, 'LEGACY_PLAN_SHA256', sha256(path))
    with pytest.raises(ValueError, match='recipe hash'):
        reuse.approved_legacy(legacy_fixture['new'], legacy_fixture['data'])


def test_parent_symlink_escape_refused_before_reading_plan(legacy_fixture, monkeypatch):
    data = legacy_fixture['data']
    retained = data.parent / 'synthetic-outside-prepared'
    (data / 'prepared').rename(retained)
    (data / 'prepared').symlink_to(retained, target_is_directory=True)
    monkeypatch.setattr(reuse, 'sha256', lambda *a: pytest.fail('Escaped plan was read before path validation'))
    with pytest.raises(ValueError, match='symlink'):
        reuse.approved_legacy(legacy_fixture['new'], data)


def make_index(fixture):
    source = fixture['directory'] / 'sample-index'
    source.mkdir()
    path = source / 'terrain_samples.sqlite'
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE points(id INTEGER PRIMARY KEY,x REAL,y REAL,z REAL)')
        connection.execute('INSERT INTO points VALUES(1,200000,550000,10)')
        connection.execute('CREATE TABLE fixture_payload(value BLOB)')
        connection.execute('INSERT INTO fixture_payload VALUES(?)', (b'x' * 65536,))
    info = {'recipe_sha256': fixture['recipe_hash'], 'sha256': sha256(path), 'sample_count': 1}
    write_json(source / 'terrain_samples.json', info)
    write_json(source / 'owner.json', {'recipe_sha256': fixture['recipe_hash'],
        'kind': 'terrain_sample_index_bundle', 'owns_directory': True})
    legacy = reuse.approved_legacy(fixture['new'], fixture['data'])
    destination = fixture['data'] / 'prepared' / fixture['new_hash'][:16] / 'sample-index'
    destination.parent.mkdir()
    return legacy, source, destination


def test_index_reuse_preserves_bytes_metadata_inode_and_staging_accounting(legacy_fixture, monkeypatch):
    fixture = legacy_fixture
    legacy, source, destination = make_index(fixture)
    original_receipt = (source / 'terrain_samples.json').read_bytes()
    index = source / 'terrain_samples.sqlite'
    original_link = os.link
    observed = []
    def inspected_link(old, new, **kwargs):
        before = fixture['budget'].snapshot()
        original_link(old, new, **kwargs)
        after = fixture['budget'].snapshot()
        charge = max(Path(old).stat().st_size, Path(old).stat().st_blocks * 512)
        assert after['temporary_bytes'] >= before['temporary_bytes'] + charge
        assert after['accounted_bytes'] - before['accounted_bytes'] < charge
        observed.append(True)
    monkeypatch.setattr(reuse.os, 'link', inspected_link)
    result = reuse.migrate_index(legacy, destination, fixture['new_hash'], fixture['budget'])
    assert observed and result['reuse_provenance']['status'] == 'reused_validated'
    assert index.stat().st_ino == (destination / index.name).stat().st_ino
    assert (source / 'terrain_samples.json').read_bytes() == original_receipt
    assert json.loads((destination / 'terrain_samples.json').read_text())['recipe_sha256'] == fixture['new_hash']


def test_index_first_link_interruption_has_prior_ownership_and_preserves_retry(legacy_fixture, monkeypatch):
    fixture = legacy_fixture
    legacy, source, destination = make_index(fixture)
    original_bytes = (source / 'terrain_samples.json').read_bytes()
    original_link = os.link
    def interrupted_link(old, new, **kwargs):
        parent = Path(new).parent
        owner = json.loads((parent / 'owner.json').read_text())
        intent = json.loads((parent / 'migration-intent.json').read_text())
        assert owner['owns_directory'] and intent['status'] == 'owned_before_hardlink_creation'
        original_link(old, new, **kwargs)
        raise InterruptedError('synthetic first hardlink interruption')
    monkeypatch.setattr(reuse.os, 'link', interrupted_link)
    with pytest.raises(InterruptedError):
        reuse.migrate_index(legacy, destination, fixture['new_hash'], fixture['budget'])
    attempt = next(destination.parent.glob('sample-index.migration-*.partial'))
    retained = attempt / 'terrain_samples.sqlite'
    inode = retained.stat().st_ino
    monkeypatch.setattr(reuse.os, 'link', original_link)
    reuse.migrate_index(legacy, destination, fixture['new_hash'], fixture['budget'])
    assert retained.exists() and retained.stat().st_ino == inode
    assert (destination / retained.name).stat().st_ino == inode
    assert (source / 'terrain_samples.json').read_bytes() == original_bytes


def test_valid_complete_index_attempt_recovers_atomic_publication(legacy_fixture, monkeypatch):
    fixture = legacy_fixture
    legacy, source, destination = make_index(fixture)
    original_rename = os.rename
    def interrupted_rename(old, new):
        if Path(new) == destination:
            raise InterruptedError('synthetic before bundle publication')
        return original_rename(old, new)
    monkeypatch.setattr(reuse.os, 'rename', interrupted_rename)
    with pytest.raises(InterruptedError):
        reuse.migrate_index(legacy, destination, fixture['new_hash'], fixture['budget'])
    attempt = next(destination.parent.glob('sample-index.migration-*.partial'))
    assert prepare._validate_index_bundle(attempt, fixture['new_hash'])['sha256'] == sha256(source / 'terrain_samples.sqlite')
    monkeypatch.setattr(reuse.os, 'rename', original_rename)
    recovered = prepare._resume_index_bundle(destination.parent, fixture['new_hash'], fixture['budget'])
    assert recovered['reuse_provenance']['status'] == 'reused_validated'
    assert not attempt.exists() and destination.is_dir()


def test_corrupt_index_receipt_or_content_never_migrates(legacy_fixture):
    fixture = legacy_fixture
    legacy, source, destination = make_index(fixture)
    index = source / 'terrain_samples.sqlite'
    with index.open('ab') as stream: stream.write(b'synthetic corruption')
    before = sha256(index)
    with pytest.raises(ValueError, match='changed'):
        reuse.migrate_index(legacy, destination, fixture['new_hash'], fixture['budget'])
    assert sha256(index) == before and not destination.exists()


def make_tile(fixture):
    from seoul_visibility.terrain import create_raster
    grid = {'id': 'synthetic', 'crs': 'EPSG:5186', 'resolution_m': 5, 'width': 4, 'height': 4,
            'bounds': [200000, 550000, 200020, 550020], 'transform': [200000, 5, 0, 550020, 0, -5]}
    halo = prepare._halo_grid(grid, 10)
    source = fixture['directory'] / 'tiles' / grid['id']
    (source / 'terrain-work').mkdir(parents=True)
    products = {}
    for name in ['dtm', 'terrain_quality', 'contract_mask', 'surface', 'occupancy', 'quality']:
        path = source / (name + '.tif')
        dataset = create_raster(path, grid)
        dataset.GetRasterBand(1).Fill(10)
        dataset.FlushCache();dataset = None
        products[name] = {'path': path.name, 'sha256': sha256(path), 'bytes': path.stat().st_size}
    ground = source / 'terrain-work/dtm.tif'
    dataset = create_raster(ground, halo)
    dataset.GetRasterBand(1).Fill(10)
    dataset.FlushCache();dataset = None
    window_count = math.ceil(halo['width'] / 256) * math.ceil(halo['height'] / 256)
    processing = {'tin_builds': window_count, 'tiles_completed': window_count}
    terrain = {'recipe_sha256': fixture['recipe_hash'], 'grid': grid,
        'products': {k: products[k] for k in ['dtm', 'terrain_quality', 'contract_mask']},
        'terrain_halo_grid': halo, 'terrain_halo': {'path': 'terrain-work/dtm.tif', 'sha256': sha256(ground)},
        'terrain_processing': processing, 'cleanup': [{'path': 'historical-only', 'bytes': 7}]}
    surface = {'recipe_sha256': fixture['recipe_hash'], 'grid': grid,
        'products': {k: products[k] for k in ['surface', 'occupancy', 'quality']},
        'cleanup': [{'path': 'historical-only', 'bytes': 11}]}
    tile = {**copy.deepcopy(terrain), 'products': products, 'surface_processing': surface}
    for name, value in [('terrain.json', terrain), ('surface.json', surface), ('tile.json', tile)]:
        write_json(source / name, value)
    selection = {'effective_window_pixels': 256, 'grid': grid, 'halo_grid': halo,
                 'attempts': [], 'source_domain_nodata': False}
    destination = fixture['data'] / 'prepared' / fixture['new_hash'][:16] / 'tiles' / grid['id']
    legacy = reuse.approved_legacy(fixture['new'], fixture['data'])
    return legacy, source, destination, selection


def test_tile_reuse_preserves_raster_inodes_and_original_receipts(legacy_fixture):
    fixture = legacy_fixture
    legacy, source, destination, selection = make_tile(fixture)
    receipts = {name: (source / name).read_bytes() for name in ['terrain.json', 'surface.json', 'tile.json']}
    result = reuse.migrate_tile(legacy, destination, fixture['new_hash'], selection, fixture['budget'])
    assert result['cleanup'] == [] and result['surface_processing']['cleanup'] == []
    assert result['reuse_provenance']['status'] == 'reused_validated'
    for name, before in receipts.items(): assert (source / name).read_bytes() == before
    for relative in ['dtm.tif', 'surface.tif', 'terrain-work/dtm.tif']:
        assert (source / relative).stat().st_ino == (destination / relative).stat().st_ino
    assert prepare.validate_tile(destination, fixture['new_hash'])['effective_terrain_window_pixels'] == 256


@pytest.mark.parametrize('change', ['window', 'grid', 'halo'])
def test_incompatible_selected_tile_is_recomputed_or_refused(legacy_fixture, change):
    fixture = legacy_fixture
    legacy, source, destination, selection = make_tile(fixture)
    if change == 'window':
        selection['effective_window_pixels'] = 128
        assert reuse.migrate_tile(legacy, destination, fixture['new_hash'], selection, fixture['budget']) is None
    else:
        selection[change if change == 'grid' else 'halo_grid'] = copy.deepcopy(selection[change if change == 'grid' else 'halo_grid'])
        selection[change if change == 'grid' else 'halo_grid']['transform'][0] += 5
        with pytest.raises(ValueError, match='grid|halo'):
            reuse.migrate_tile(legacy, destination, fixture['new_hash'], selection, fixture['budget'])
    assert not destination.exists() and (source / 'tile.json').exists()


@pytest.mark.parametrize('relative', ['dtm.tif', 'terrain-work/dtm.tif'])
def test_corrupt_source_raster_or_halo_never_migrates(legacy_fixture, relative):
    fixture = legacy_fixture
    legacy, source, destination, selection = make_tile(fixture)
    path = source / relative
    with path.open('ab') as stream: stream.write(b'synthetic corrupt raster trailer')
    before = sha256(path)
    with pytest.raises(ValueError, match='Corrupt|corrupt'):
        reuse.migrate_tile(legacy, destination, fixture['new_hash'], selection, fixture['budget'])
    assert sha256(path) == before and not destination.exists()


def test_tile_owner_precedes_first_link_and_retry_retains_interrupted_alias(legacy_fixture, monkeypatch):
    fixture = legacy_fixture
    legacy, source, destination, selection = make_tile(fixture)
    original_link = os.link
    retained = []
    def interrupted_link(old, new, **kwargs):
        attempt = Path(new).parent
        while not attempt.name.endswith('.partial'): attempt = attempt.parent
        assert json.loads((attempt / 'owner.json').read_text())['kind'] == 'citywide_prepare_tile'
        assert (attempt / 'migration-intent.json').is_file()
        original_link(old, new, **kwargs);retained.append(Path(new))
        raise InterruptedError('synthetic interrupted tile link')
    monkeypatch.setattr(reuse.os, 'link', interrupted_link)
    with pytest.raises(InterruptedError):
        reuse.migrate_tile(legacy, destination, fixture['new_hash'], selection, fixture['budget'])
    monkeypatch.setattr(reuse.os, 'link', original_link)
    reuse.migrate_tile(legacy, destination, fixture['new_hash'], selection, fixture['budget'])
    assert retained[0].exists() and (source / 'tile.json').exists()
    assert prepare.validate_tile(destination, fixture['new_hash'])['reuse_provenance']['status'] == 'reused_validated'


def test_staging_refusal_happens_before_any_link(legacy_fixture, monkeypatch):
    fixture = legacy_fixture
    legacy, source, destination = make_index(fixture)
    fixture['budget'].stage_limit = 16 * 1024**2
    monkeypatch.setattr(reuse.os, 'link', lambda *a, **k: pytest.fail('Link created after staging refusal'))
    with pytest.raises(ResourceBudgetError, match='temporary'):
        reuse.migrate_index(legacy, destination, fixture['new_hash'], fixture['budget'])
    assert not destination.exists() and (source / 'terrain_samples.sqlite').is_file()


@pytest.mark.parametrize('evidence', ['explicit_window', 'tin_count', 'receipt_disagreement', 'source_domain'])
def test_inconsistent_old_window_or_processing_receipts_refuse_migration(legacy_fixture, evidence):
    fixture = legacy_fixture
    legacy, source, destination, selection = make_tile(fixture)
    terrain = json.loads((source / 'terrain.json').read_text())
    tile = json.loads((source / 'tile.json').read_text())
    if evidence == 'explicit_window':
        terrain['effective_terrain_window_pixels'] = 128
    elif evidence == 'tin_count':
        terrain['terrain_processing']['tin_builds'] += 1
        tile['terrain_processing'] = copy.deepcopy(terrain['terrain_processing'])
    elif evidence == 'receipt_disagreement':
        tile['terrain_processing']['synthetic_unrecorded_change'] = True
    else:
        selection['source_domain_nodata'] = True
    write_json(source / 'terrain.json', terrain)
    write_json(source / 'tile.json', tile)
    before = (source / 'tile.json').read_bytes()
    with pytest.raises(ValueError, match='window|receipts disagree|source-domain'):
        reuse.migrate_tile(legacy, destination, fixture['new_hash'], selection, fixture['budget'])
    assert (source / 'tile.json').read_bytes() == before and not destination.exists()


def test_ancestor_symlink_data_root_refused(legacy_fixture, monkeypatch):
    data = legacy_fixture['data']
    alias = data.parent / 'synthetic-alias'
    alias.symlink_to(data.parent, target_is_directory=True)
    monkeypatch.setattr(reuse, 'sha256', lambda *a: pytest.fail('Noncanonical root was read before validation'))
    with pytest.raises(ValueError, match='symlink'):
        reuse.approved_legacy(legacy_fixture['new'], alias / data.name)
