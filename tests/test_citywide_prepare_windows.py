"""Synthetic, bounded planner fixtures; no network or real-data preparation."""
import hashlib
import sqlite3

import pytest
from shapely.geometry import box, shape

from scripts.data.prepare_resources import select_windows, processing_extents


def synthetic_points(path):
    with sqlite3.connect(path) as connection:
        connection.executescript('CREATE TABLE points(id INTEGER PRIMARY KEY,x REAL,y REAL,z REAL);'
            'CREATE VIRTUAL TABLE spatial USING rtree(id,minx,maxx,miny,maxy);')
        identifier = 0
        for x in range(20, 301, 20):
            for y in range(20, 301, 20):
                identifier += 1
                connection.execute('INSERT INTO points VALUES(?,?,?,?)', (identifier, x, y, 10))
                connection.execute('INSERT INTO spatial VALUES(?,?,?,?,?)', (identifier, x, x, y, y))
    return {'id': 'synthetic', 'crs': 'EPSG:5186', 'resolution_m': 5,
        'width': 64, 'height': 64, 'bounds': [0, 0, 320, 320], 'transform': [0, 5, 0, 320, 0, -5]}


def test_window_halving_changes_batches_only_and_keeps_index_immutable(tmp_path):
    index = tmp_path / 'synthetic.sqlite'
    grid = synthetic_points(index)
    before = hashlib.sha256(index.read_bytes()).hexdigest()
    settings = {'tile_size': 64, 'halo_m': 10, 'max_points_per_tile': 100}
    selected = select_windows(index, [grid], settings, box(*grid['bounds']))['synthetic']
    assert selected['effective_window_pixels'] == 32
    assert selected['attempts'][0]['maximum_samples'] == 225
    assert selected['attempts'][1]['maximum_samples'] <= 100
    assert selected['grid'] == grid
    assert selected['halo_grid']['bounds'] == [-10, -10, 330, 330]
    assert selected['halo_grid']['resolution_m'] == 5
    assert settings == {'tile_size': 64, 'halo_m': 10, 'max_points_per_tile': 100}
    assert hashlib.sha256(index.read_bytes()).hexdigest() == before


def test_original_window_reused_when_exact_count_fits(tmp_path):
    index = tmp_path / 'synthetic.sqlite'
    grid = synthetic_points(index)
    selected = select_windows(index, [grid], {'tile_size': 64, 'halo_m': 10, 'max_points_per_tile': 300},
                              box(*grid['bounds']))['synthetic']
    assert selected['effective_window_pixels'] == 64
    assert len(selected['attempts']) == 1


def test_window_floor_refuses_without_reducing_halo_or_sample_density(tmp_path):
    from seoul_visibility.acquisition_safety import AcquisitionError

    index = tmp_path / 'synthetic.sqlite'
    grid = synthetic_points(index)
    with pytest.raises(AcquisitionError, match='still exceeds.*16 pixels'):
        select_windows(index, [grid], {'tile_size': 64, 'halo_m': 10, 'max_points_per_tile': 3},
                       box(*grid['bounds']))


def test_source_domain_nodata_shortcut_avoids_density_queries(tmp_path):
    index = tmp_path / 'synthetic.sqlite'
    grid = synthetic_points(index)
    selected = select_windows(index, [grid], {'tile_size': 64, 'halo_m': 10, 'max_points_per_tile': 3},
                              box(1000, 1000, 1100, 1100))['synthetic']
    assert selected['source_domain_nodata'] and selected['attempts'] == []
    assert selected['effective_window_pixels'] == 64


def test_processing_extent_reports_both_halos_and_exact_union_geometry():
    grid = {'id': 'synthetic', 'crs': 'EPSG:5186', 'resolution_m': 5,
        'width': 4, 'height': 4, 'bounds': [200000, 550000, 200020, 550020],
        'transform': [200000, 5, 0, 550020, 0, -5]}
    source = box(*grid['bounds'])
    result = processing_extents([grid], 10, 15, source, source.buffer(5), source.buffer(10))
    query = result['conservative_all_workspace_query_union']
    assert query['bounds_epsg5186'] == [199975, 549975, 200045, 550045]
    assert query['area_m2'] == 70 * 70
    assert query['outside_declared_terrain_halo_area_m2'] > 0
    assert query['geometry_crs'] == 'OGC:CRS84'
    assert shape(query['geometry_wgs84']).is_valid
    assert result['actual_tin_workspace_count'] == 1
