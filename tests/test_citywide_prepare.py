"""Synthetic fixtures; never trigger acquisition or a citywide raster build."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.data.prepare_pipeline import tile_grids, _halo_grid


def test_full_extent_preserved_at_five_metres():
    grids = list(tile_grids([198765, 548765, 211234, 561234]))
    assert len(grids) == 16
    assert min(g['bounds'][0] for g in grids) <= 198765
    assert min(g['bounds'][1] for g in grids) <= 548765
    assert max(g['bounds'][2] for g in grids) >= 211234
    assert max(g['bounds'][3] for g in grids) >= 561234
    assert all(g['width'] == g['height'] == 1000 for g in grids)
    assert all(g['resolution_m'] == 5 for g in grids)


def test_resolution_not_silently_coarsened():
    with pytest.raises(ValueError, match='5 m'):
        list(tile_grids([0, 0, 5000, 5000], resolution=10))


def test_footprint_halo_preserves_source_contract_and_grid():
    grid = next(tile_grids([200000, 550000, 205000, 555000]))
    expanded = _halo_grid(grid, 1000)
    assert expanded['bounds'] == [199000, 549000, 206000, 556000]
    assert expanded['width'] == expanded['height'] == 1400
    assert expanded['resolution_m'] == 5
    assert grid['bounds'] == [200000, 550000, 205000, 555000]


def test_source_domain_holes_remain_nodata(tmp_path):
    import numpy as np
    from osgeo import gdal
    from pyproj import Transformer
    from shapely.geometry import box, mapping
    from shapely.ops import transform
    from seoul_visibility.terrain import create_raster, NODATA
    from scripts.data.prepare_pipeline import _mask_terrain

    grid = {'crs': 'EPSG:5186', 'resolution_m': 5, 'width': 2, 'height': 2,
            'bounds': [200000, 550000, 200010, 550010], 'transform': [200000, 5, 0, 550010, 0, -5]}
    raw = tmp_path / 'raw.tif'
    dataset = create_raster(raw, grid)
    # One genuinely unsupported cell inside the declared terrain domain.
    dataset.GetRasterBand(1).WriteArray(np.array([[20, 25], [NODATA, 25]], dtype=np.float32))
    dataset = None
    to_ll = Transformer.from_crs(5186, 4326, always_xy=True).transform
    geometry = tmp_path / 'extent.json'
    geometry.write_text(json.dumps({'type': 'FeatureCollection', 'features': [
        {'type': 'Feature', 'properties': {'role': role}, 'geometry': mapping(transform(to_ll, polygon))}
        for role, polygon in [('recommendation', box(200000, 550000, 200005, 550010)),
                              ('obstruction_support', box(*grid['bounds']))]]}))
    checks = []
    report = _mask_terrain(raw, tmp_path, grid, geometry, lambda: checks.append(True))
    assert report['cell_counts']['requested_support_cells'] == 4
    assert report['cell_counts']['inside_seoul_cells'] == 2
    assert report['cell_counts']['terrain_valid_support_cells'] == 1
    assert report['support_missing_area_m2'] == 75
    quality = gdal.Open(str(tmp_path / 'quality.tif')).ReadAsArray()
    assert quality.tolist() == [[1, 0], [0, 0]]
    assert checks


def test_disjoint_halo_nodata_shortcut_matches_domain_mask(tmp_path, monkeypatch):
    import numpy as np
    from osgeo import gdal
    from shapely.geometry import box, mapping
    from shapely.ops import transform
    from pyproj import Transformer
    from seoul_visibility.terrain import create_raster, NODATA
    from scripts.data.prepare_pipeline import _source_aware_terrain, _mask_terrain

    grid = {'id': 'synthetic', 'crs': 'EPSG:5186', 'resolution_m': 5, 'width': 4, 'height': 4,
        'bounds': [200000, 550000, 200020, 550020], 'transform': [200000, 5, 0, 550020, 0, -5]}
    to_ll = Transformer.from_crs(5186, 4326, always_xy=True).transform
    def geometry_file(admin, name):
        path = tmp_path / name
        path.write_text(json.dumps({'type': 'FeatureCollection', 'features': [
            {'type': 'Feature', 'properties': {'role': role}, 'geometry': mapping(transform(to_ll, polygon))}
            for role, polygon in [('recommendation', admin),
                ('obstruction_support', box(199990, 549990, 200130, 550030))]]}))
        return path
    geometry = geometry_file(box(200100, 550000, 200120, 550020), 'disjoint.json')
    monkeypatch.setattr('seoul_visibility.terrain.prepare_terrain', lambda *a, **k: pytest.fail('Unsupported halo triggered interpolation'))
    shortcut = tmp_path / 'shortcut'; shortcut.mkdir()
    result = _source_aware_terrain({}, grid, shortcut / 'raw.tif', geometry, lambda: None)
    assert result['interpolation_performed'] is False
    shortcut_report = _mask_terrain(shortcut / 'raw.tif', shortcut, grid, geometry, lambda: None)
    # A hypothetical finite raw interpolation outside the inspected source
    # domain must be erased by the normal mask. Shortcut outputs match it.
    reference = tmp_path / 'reference'; reference.mkdir()
    raw = create_raster(reference / 'raw.tif', grid)
    raw.GetRasterBand(1).Fill(77)
    raw = None
    reference_report = _mask_terrain(reference / 'raw.tif', reference, grid, geometry, lambda: None)
    for name in ['dtm', 'quality', 'contract_mask']:
        actual = gdal.Open(str(shortcut / (name + '.tif'))).ReadAsArray()
        expected = gdal.Open(str(reference / (name + '.tif'))).ReadAsArray()
        assert np.array_equal(actual, expected)
    assert shortcut_report['cell_counts'] == reference_report['cell_counts']
    assert np.all(gdal.Open(str(shortcut / 'dtm.tif')).ReadAsArray() == NODATA)
    # A final tile outside Seoul can still have a halo that intersects Seoul;
    # that case must retain the existing interpolation path.
    calls = []
    monkeypatch.setattr('seoul_visibility.terrain.prepare_terrain', lambda *a, **k: calls.append(a) or {'fixture': True})
    geometry = geometry_file(box(200025, 550005, 200030, 550010), 'halo-intersects.json')
    result = _source_aware_terrain({}, _halo_grid(grid, 10), tmp_path / 'unused.tif', geometry, lambda: None)
    assert result == {'fixture': True} and len(calls) == 1


def _synthetic_surface_source(tmp_path):
    import numpy as np
    from osgeo import ogr, osr
    from shapely.geometry import box, mapping
    from pyproj import Transformer
    from shapely.ops import transform
    from seoul_visibility.terrain import create_raster, NODATA

    grid = {'id': 'synthetic', 'crs': 'EPSG:5186', 'resolution_m': 5, 'width': 4, 'height': 4,
        'bounds': [200000, 550000, 200020, 550020], 'transform': [200000, 5, 0, 550020, 0, -5]}
    ground = tmp_path / 'ground.tif'
    ds = create_raster(ground, grid)
    values = np.full((4, 4), 10, dtype=np.float32)
    values[1, 1] = NODATA
    ds.GetRasterBand(1).WriteArray(values)
    ds = None
    source = tmp_path / 'source.gpkg'
    ds = ogr.GetDriverByName('GPKG').CreateDataSource(str(source))
    srs = osr.SpatialReference(); srs.ImportFromEPSG(5186)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    layer = ds.CreateLayer('buildings', srs, ogr.wkbPolygon, options=['SPATIAL_INDEX=YES'])
    for name, dtype in [('source_id', ogr.OFTString), ('height_m', ogr.OFTReal),
        ('height_var', ogr.OFTReal), ('invalid_geometry', ogr.OFTInteger)]:
        layer.CreateField(ogr.FieldDefn(name, dtype))
    for fid, polygon, height in [
        (11, box(200000.5, 550015.5, 200004.5, 550019.5), 12),
        (12, box(200005.5, 550015.5, 200009.5, 550019.5), None),
        (13, box(200005.5, 550010.5, 200014.5, 550014.5), 20)]:
        feature = ogr.Feature(layer.GetLayerDefn())
        feature.SetFID(fid)
        feature.SetField('source_id', f'synthetic-{fid}')
        if height is not None: feature.SetField('height_m', height)
        feature.SetField('height_var', 2.5)
        feature.SetField('invalid_geometry', 0)
        feature.SetGeometry(ogr.CreateGeometryFromWkb(polygon.wkb))
        layer.CreateFeature(feature)
    feature = layer = ds = None
    boundary = tmp_path / 'support.json'
    to_ll = Transformer.from_crs(5186, 4326, always_xy=True).transform
    boundary.write_text(json.dumps({'type': 'FeatureCollection', 'features': [
        {'type': 'Feature', 'properties': {'role': 'obstruction_support'},
         'geometry': mapping(transform(to_ll, box(199999, 549999, 200015.1, 550021)))}]}))
    return grid, ground, source, boundary


def test_existing_surfaces_preserve_missing_ground_height_and_coverage(tmp_path, monkeypatch):
    import numpy as np
    from osgeo import gdal
    from seoul_visibility.acquisition_safety import Budget, sha256
    from seoul_visibility.terrain import NODATA
    from scripts.data.prepare_pipeline import _surface_stage

    grid, ground, source, boundary = _synthetic_surface_source(tmp_path)
    budget = Budget(tmp_path, stage_root=tmp_path / 'staging')
    job = {'grid': grid, 'halo_grid': grid, 'recipe_sha256': 'synthetic-recipe',
        'buildings': str(source), 'buildings_sha256': sha256(source),
        'support_bounds': grid['bounds'],
        'support_boundary': str(boundary), 'footprint_halo_m': 0}
    terrain = {'terrain_halo': {'path': ground.name, 'sha256': sha256(ground)}}
    # Synthetic interrupted crops have no receipt. Preserve their exact bytes
    # before recomputing, while retaining the independently validated ground.
    (tmp_path / 'surface.tif').write_bytes(b'synthetic interrupted surface')
    (tmp_path / 'quality.tif').write_bytes(b'synthetic interrupted quality')
    (tmp_path / 'owner.json').write_text(json.dumps({'recipe_sha256': 'synthetic-recipe',
        'owned_relative_paths': ['surface.tif', 'quality.tif']}))
    report = _surface_stage(job, tmp_path, terrain, budget, lambda: None)
    preserved = list(tmp_path.glob('.surface-interrupted-*.partial'))
    assert len(preserved) == 1
    assert (preserved[0] / 'surface.tif').read_bytes() == b'synthetic interrupted surface'
    assert (preserved[0] / 'quality.tif').read_bytes() == b'synthetic interrupted quality'
    assert sha256(ground) == terrain['terrain_halo']['sha256']
    quality = gdal.Open(str(tmp_path / 'quality.tif')).ReadAsArray()
    surface = gdal.Open(str(tmp_path / 'surface.tif')).ReadAsArray()
    assert surface[0, 0] == 22  # 10 m ground + 12 m estimated AGL.
    assert quality[0, 0] == 7
    assert quality[0, 1] == 15  # Missing height remains unresolved.
    assert surface[1, 1] == NODATA
    assert quality[1, 1] == 14
    assert quality[1, 2] == 15  # Partial ground support invalidates the whole roof.
    assert np.all((quality[:, 3] & 2) == 0)  # Never expand surveyed building area to the tile rectangle.
    assert report['building_normalization']['unresolved_heights'] == 2
    assert report['subset']['source_fid_preserved']
    assert source.exists() and ground.exists()
    assert report['cleanup']
    assert list(tmp_path.glob('cleanup.*.json'))
    # Verified surface checkpoint skips native extraction/rasterization.
    monkeypatch.setattr('scripts.data.prepare_pipeline._subset_buildings', lambda *a, **k: pytest.fail('Unexpected repeated subset'))
    reused = _surface_stage(job, tmp_path, terrain, budget, lambda: None)
    assert reused['products'] == report['products']


def test_surface_subset_checks_crs_and_preserves_original_ids(tmp_path):
    import sqlite3
    from osgeo import gdal
    from scripts.data.prepare_pipeline import _subset_buildings

    grid, ground, source, boundary = _synthetic_surface_source(tmp_path)
    output = tmp_path / 'subset.gpkg'
    report = _subset_buildings(source, output, grid, lambda: None)
    assert report['features'] == 3
    ds = gdal.OpenEx(str(output), gdal.OF_VECTOR)
    assert [(f.GetFID(), f.GetField('source_id')) for f in ds.GetLayerByName('buildings')] == [
        (11, 'synthetic-11'), (12, 'synthetic-12'), (13, 'synthetic-13')]
    ds = None
    with pytest.raises(ValueError, match='preserved'):
        _subset_buildings(source, output, grid, lambda: None)
    # Deliberately inconsistent synthetic metadata must be rejected, never
    # silently assigned a projected CRS based on coordinate magnitudes.
    with sqlite3.connect(source) as db:
        db.execute("UPDATE gpkg_geometry_columns SET srs_id=4326 WHERE table_name='buildings'")
        db.execute("UPDATE gpkg_contents SET srs_id=4326 WHERE table_name='buildings'")
    with pytest.raises(ValueError, match='CRS'):
        _subset_buildings(source, tmp_path / 'bad-crs.gpkg', grid, lambda: None)


def _synthetic_index_bundle(directory, recipe='synthetic-recipe'):
    import sqlite3
    from seoul_visibility.acquisition_safety import sha256

    directory.mkdir()
    index = directory / 'terrain_samples.sqlite'
    with sqlite3.connect(index) as connection:
        connection.execute('CREATE TABLE synthetic_fixture(value INTEGER)')
        connection.execute('INSERT INTO synthetic_fixture VALUES(42)')
    info = {'recipe_sha256': recipe, 'sha256': sha256(index), 'synthetic': True}
    (directory / 'terrain_samples.json').write_text(json.dumps(info))
    (directory / 'owner.json').write_text(json.dumps({'recipe_sha256': recipe,
        'kind': 'terrain_sample_index_bundle', 'owns_directory': True}))
    return info


@pytest.mark.parametrize('crash_after_rename', [False, True])
def test_index_pair_atomic_publication_recovers_interruption(tmp_path, monkeypatch, crash_after_rename):
    import os
    from seoul_visibility.acquisition_safety import Budget
    from scripts.data.prepare_pipeline import _publish_index_bundle, _resume_index_bundle

    budget = Budget(tmp_path, stage_root=tmp_path / 'staging')
    attempt = tmp_path / 'sample-index.synthetic.partial'
    expected = _synthetic_index_bundle(attempt)
    final = tmp_path / 'sample-index'
    rename = os.rename
    def interrupted(source, destination):
        if crash_after_rename:
            rename(source, destination)
        raise KeyboardInterrupt('Synthetic interruption at bundle publication')
    monkeypatch.setattr('scripts.data.prepare_pipeline.os.rename', interrupted)
    with pytest.raises(KeyboardInterrupt):
        _publish_index_bundle(attempt, final, 'synthetic-recipe', budget)
    present = final if crash_after_rename else attempt
    assert (present / 'terrain_samples.sqlite').is_file()
    assert (present / 'terrain_samples.json').is_file()
    monkeypatch.setattr('scripts.data.prepare_pipeline.os.rename', rename)
    assert _resume_index_bundle(tmp_path, 'synthetic-recipe', budget) == expected
    assert final.is_dir() and not attempt.exists()


@pytest.mark.parametrize('orphan_name', ['terrain_samples.sqlite', 'terrain_samples.json'])
def test_orphan_index_half_preserved_before_new_attempt(tmp_path, orphan_name):
    from seoul_visibility.acquisition_safety import Budget
    from scripts.data.prepare_pipeline import _resume_index_bundle

    budget = Budget(tmp_path, stage_root=tmp_path / 'staging')
    final = tmp_path / 'sample-index'; final.mkdir()
    (final / orphan_name).write_bytes(b'original synthetic orphan')
    (final / 'owner.json').write_text(json.dumps({'recipe_sha256': 'synthetic-recipe', 'owns_directory': True}))
    assert _resume_index_bundle(tmp_path, 'synthetic-recipe', budget) is None
    assert not final.exists()
    directories = list(tmp_path.glob('.index-interrupted-*.partial'))
    assert len(directories) == 1
    assert (directories[0] / 'sample-index' / orphan_name).read_bytes() == b'original synthetic orphan'
    manifest = json.loads((directories[0] / 'preservation.json').read_text())
    assert manifest['completed'] == ['sample-index'] and not manifest['deletion_performed']


def test_existing_index_bundle_never_replaced(tmp_path):
    from seoul_visibility.acquisition_safety import Budget, sha256
    from scripts.data.prepare_pipeline import _publish_index_bundle

    budget = Budget(tmp_path, stage_root=tmp_path / 'staging')
    final = tmp_path / 'sample-index'; _synthetic_index_bundle(final)
    expected = sha256(final / 'terrain_samples.sqlite')
    attempt = tmp_path / 'sample-index.second.partial'; _synthetic_index_bundle(attempt)
    with pytest.raises(ValueError, match='bundle preserved'):
        _publish_index_bundle(attempt, final, 'synthetic-recipe', budget)
    assert sha256(final / 'terrain_samples.sqlite') == expected
    assert attempt.is_dir()


def test_interrupted_orphan_preservation_retains_every_original(tmp_path, monkeypatch):
    import os
    from seoul_visibility.acquisition_safety import Budget
    from scripts.data.prepare_pipeline import _preserve_unvalidated

    budget = Budget(tmp_path, stage_root=tmp_path / 'staging')
    names = ['dtm.tif', 'terrain_quality.tif']
    for name in names: (tmp_path / name).write_bytes(('synthetic original ' + name).encode())
    (tmp_path / 'owner.json').write_text(json.dumps({'recipe_sha256': 'synthetic-recipe',
        'owned_relative_paths': names}))
    rename = os.rename
    def interrupted(source, destination):
        rename(source, destination)
        raise KeyboardInterrupt('Synthetic crash after first preservation move')
    monkeypatch.setattr('scripts.data.prepare_pipeline.os.rename', interrupted)
    with pytest.raises(KeyboardInterrupt):
        _preserve_unvalidated(tmp_path, names, budget, 'terrain', 'synthetic-recipe')
    monkeypatch.setattr('scripts.data.prepare_pipeline.os.rename', rename)
    _preserve_unvalidated(tmp_path, names, budget, 'terrain', 'synthetic-recipe')
    for name in names:
        copies = list(tmp_path.glob('.terrain-interrupted-*.partial/' + name))
        assert len(copies) == 1
        assert copies[0].read_bytes() == ('synthetic original ' + name).encode()
    assert all(not (tmp_path / name).exists() for name in names)


def test_native_crop_refuses_existing_output_before_write(tmp_path):
    from scripts.data.prepare_pipeline import _crop_raster

    output = tmp_path / 'dtm.tif'; output.write_bytes(b'synthetic existing crop')
    with pytest.raises(ValueError, match='Existing raster crop preserved'):
        _crop_raster(tmp_path / 'unused.tif', output, {}, {})
    assert output.read_bytes() == b'synthetic existing crop'


def test_unknown_orphan_is_preserved_in_place(tmp_path):
    from seoul_visibility.acquisition_safety import Budget
    from scripts.data.prepare_pipeline import _preserve_unvalidated, _register_tile_owner

    budget = Budget(tmp_path, stage_root=tmp_path / 'staging')
    (tmp_path / 'dtm.tif').write_bytes(b'synthetic user-owned file')
    with pytest.raises(ValueError, match='ownership receipt'):
        _preserve_unvalidated(tmp_path, ['dtm.tif'], budget, 'terrain', 'synthetic-recipe')
    with pytest.raises(ValueError, match='Unrecognized existing tile workspace'):
        _register_tile_owner(tmp_path, 'synthetic-recipe', budget)
    assert (tmp_path / 'dtm.tif').read_bytes() == b'synthetic user-owned file'
    assert not list(tmp_path.glob('.terrain-interrupted-*'))


def test_pending_initial_tile_owner_recovers_before_native_writes(tmp_path):
    from seoul_visibility.acquisition_safety import Budget
    from scripts.data.prepare_pipeline import _register_tile_owner, TILE_OWNED_PATHS

    staging = tmp_path / 'staging'; staging.mkdir()
    budget = Budget(tmp_path, stage_root=staging)
    expected = {'recipe_sha256': 'synthetic-recipe', 'kind': 'citywide_prepare_tile',
                'owned_relative_paths': TILE_OWNED_PATHS}
    (staging / 'owner.json.writing').write_text(json.dumps(expected))
    _register_tile_owner(staging, 'synthetic-recipe', budget)
    assert json.loads((staging / 'owner.json').read_text()) == expected
    assert not (staging / 'owner.json.writing').exists()
