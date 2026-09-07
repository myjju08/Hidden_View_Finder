from __future__ import annotations

import hashlib
import sqlite3

import numpy as np
from osgeo import ogr, osr
import pytest

from seoul_visibility.errors import ConfigurationError
from seoul_visibility.terrain import _build_sample_index, _contour_samples, _validation_spots


def test_regular_arclength_follows_curved_line_and_keeps_endpoints():
    # A finely digitized right angle: arclength must follow both legs.
    vertices = [(x, 0.0, 12.5) for x in np.linspace(0, 5, 51)]
    vertices += [(5.0, y, 12.5) for y in np.linspace(0.1, 5, 50)]
    actual = list(_contour_samples(vertices, 3.0, "regular_arclength"))
    np.testing.assert_allclose(actual, [(0, 0, 12.5), (3, 0, 12.5), (5, 1, 12.5),
                                        (5, 4, 12.5), (5, 5, 12.5)], atol=1e-12)
    assert actual[0] == vertices[0] and actual[-1] == vertices[-1]
    assert len(actual) < len(vertices) / 10
    assert all(point[2] == 12.5 for point in actual)


def test_varying_geometry_z_rejected_in_regular_mode_and_extremum_retained():
    vertices = [(0.0, 0.0, 0.0), (1.0, 0.0, 20.0), (2.0, 0.0, 0.0)]
    with pytest.raises(ConfigurationError, match="varying geometry Z"):
        list(_contour_samples(vertices, 10.0, "regular_arclength"))
    assert list(_contour_samples(vertices, 10.0, "preserve_vertices")) == vertices


def test_regular_repeated_vertices_and_zero_length_contour():
    points = [(0.0, 0.0, 6.0), (0.0, 0.0, 6.0), (0.0, 10.0, 6.0)]
    assert list(_contour_samples(points, 5.0, "regular_arclength")) == [
        (0.0, 0.0, 6.0), (0.0, 5.0, 6.0), (0.0, 10.0, 6.0)]
    assert list(_contour_samples(points[:2], 5.0, "regular_arclength")) == points[:2]


def test_regular_sample_index_preserves_raw_geometry_and_elevation(tmp_path):
    path = tmp_path / "contours.gpkg"
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(str(path))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(5186)
    layer = ds.CreateLayer("contours", srs, ogr.wkbLineString)
    layer.CreateField(ogr.FieldDefn("elevation", ogr.OFTReal))
    feature = ogr.Feature(layer.GetLayerDefn())
    geometry = ogr.Geometry(ogr.wkbLineString)
    for x in np.linspace(200000, 200005, 51):
        geometry.AddPoint_2D(float(x), 550000)
    for y in np.linspace(550000.1, 550005, 50):
        geometry.AddPoint_2D(200005, float(y))
    feature.SetGeometry(geometry)
    feature.SetField("elevation", 12.5)
    layer.CreateFeature(feature)
    feature = geometry = layer = ds = None
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    config = {"sources": [{"path": str(path), "layer": "contours", "elevation_field": "elevation"}],
              "sample_spacing_m": 3.0, "contour_sampling": "regular_arclength"}
    index = tmp_path / "points.sqlite"
    stats = _build_sample_index(config, {"crs": "EPSG:5186", "resolution_m": 5}, index)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert stats["sample_count"] == 5
    with sqlite3.connect(index) as connection:
        rows = connection.execute("SELECT x,y,z FROM points ORDER BY id").fetchall()
    assert rows[0] == (200000, 550000, 12.5)
    assert rows[-1] == (200005, 550005, 12.5)
    assert set(row[2] for row in rows) == {12.5}


def test_validation_is_bounded_reproducible_and_spatially_distributed():
    with sqlite3.connect(":memory:") as connection:
        connection.execute("CREATE TABLE points(id INTEGER PRIMARY KEY,x REAL,y REAL,z REAL,holdout INTEGER)")
        rows = [(1 + y * 20 + x, float(x), float(y), x + y, 1) for y in range(20) for x in range(20)]
        connection.executemany("INSERT INTO points VALUES(?,?,?,?,?)", rows)
        selected, report = _validation_spots(connection, 25, 0)
        repeated, repeated_report = _validation_spots(connection, 25, 0)
        assert selected == repeated and report == repeated_report
        assert len(selected) == 25 and report["available"] == 400
        cells = {(min(4, int(x / 19 * 5)), min(4, int(y / 19 * 5))) for x, y, _ in selected}
        assert len(cells) == 25
        assert _validation_spots(connection, 0, 0)[0] == []
        assert len(_validation_spots(connection, 500, 0)[0]) == 400
        with pytest.raises(ConfigurationError, match="max_validation_spots"):
            _validation_spots(connection, -1, 0)


def _indexed_fixture(directory, *, planar=False):
    import json
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "terrain_samples.sqlite"
    rng = np.random.default_rng(21)
    points = rng.uniform([-100, -295], [275, 100], size=(600, 2))
    z = .01 * points[:, 0] + .02 * points[:, 1]
    if not planar:
        z += np.sin(points[:, 0] / 20)
    with sqlite3.connect(path) as connection:
        connection.executescript("CREATE TABLE points(id INTEGER PRIMARY KEY,x REAL,y REAL,z REAL,holdout INTEGER); CREATE VIRTUAL TABLE spatial USING rtree(id,minx,maxx,miny,maxy);")
        for i, ((x, y), height) in enumerate(zip(points, z), 1):
            connection.execute("INSERT INTO points VALUES(?,?,?,?,?)", (i, x, y, height, int(i % 5 == 0)))
            connection.execute("INSERT INTO spatial VALUES(?,?,?,?,?)", (i, x, x, y, y))
    (directory / "terrain_samples.json").write_text(json.dumps({"sample_count": 600, "spot_count": 600,
        "sample_spacing_m": 10, "contour_sampling": "preserve_vertices"}))
    return path


def test_single_tin_seam_probes_equal_legacy_neighbor_triangulations(tmp_path):
    from osgeo import gdal
    from seoul_visibility.terrain import _interpolate_tiles, _linear_supported, _local_points, iter_tiles, NODATA
    path = _indexed_fixture(tmp_path)
    grid = {"width": 35, "height": 39, "resolution_m": 5, "crs": "EPSG:5186",
            "transform": [0, 5, 0, 0, 0, -5], "bounds": [0, -195, 175, 0]}
    expected = np.full((39, 35), NODATA, dtype=np.float32)
    vertical, horizontal = [], []
    with sqlite3.connect(path) as connection:
        for x, y, w, h in iter_tiles(grid, 16):
            xmin, ymax = x * 5, -y * 5
            xmax, ymin = xmin + w * 5, ymax - h * 5
            xx, yy = xmin + (np.arange(w) + .5) * 5, ymax - (np.arange(h) + .5) * 5
            mx, my = np.meshgrid(xx, yy)
            p = _local_points(connection, (xmin - 50, ymin - 50, xmax + 50, ymax + 50), 10000)
            values = _linear_supported(p, np.column_stack((mx.ravel(), my.ravel())), 75).reshape(h, w)
            valid = np.isfinite(values)
            if x > 0:
                other = _local_points(connection, (xmin - 80 - 50, ymin - 50, xmin + 50, ymax + 50), 10000)
                adjacent = _linear_supported(other, np.column_stack((np.full(h, xx[0]), yy)), 75)
                both = np.isfinite(adjacent) & valid[:, 0]
                if both.any():
                    vertical.append(float(np.max(np.abs(adjacent[both] - values[both, 0]))))
            if y > 0:
                other = _local_points(connection, (xmin - 50, ymax - 50, xmax + 50, ymax + 80 + 50), 10000)
                adjacent = _linear_supported(other, np.column_stack((xx, np.full(w, yy[0]))), 75)
                both = np.isfinite(adjacent) & valid[0]
                if both.any():
                    horizontal.append(float(np.max(np.abs(adjacent[both] - values[0, both]))))
            values[~valid] = NODATA
            expected[y:y+h, x:x+w] = values
        output = tmp_path / "optimized.tif"
        report = _interpolate_tiles(connection, grid, output, 16, 50, 75, 10000, lambda: None)
    dataset = gdal.Open(str(output))
    np.testing.assert_array_equal(dataset.ReadAsArray(), expected)
    assert report["tin_builds"] == 9
    assert report["seam_check"]["vertical_edges_compared"] == len(vertical)
    assert report["seam_check"]["horizontal_edges_compared"] == len(horizontal)
    assert report["seam_check"]["max_same_point_difference_m"] == pytest.approx(max(vertical + horizontal), abs=1e-12)


def test_closed_raster_and_validation_resume_after_interruption(tmp_path, monkeypatch):
    import json
    from osgeo import gdal
    import seoul_visibility.terrain as terrain
    _indexed_fixture(tmp_path, planar=True)
    grid = {"width": 35, "height": 39, "resolution_m": 5, "crs": "EPSG:5186",
            "transform": [0, 5, 0, 0, 0, -5], "bounds": [0, -195, 175, 0]}
    config = {"kind": "samples", "sources": [], "sample_spacing_m": 10, "tile_size": 16,
              "halo_m": 100, "max_triangle_edge_m": 75, "max_validation_spots": 30, "validation_seed": 0}
    output = tmp_path / "dtm.tif"
    def interrupt_after_checkpoint():
        checkpoint = tmp_path / "terrain_validation_checkpoint.json"
        if checkpoint.exists() and json.loads(checkpoint.read_text())["processed"] >= 20:
            raise RuntimeError("deliberate validation interruption")
    with pytest.raises(RuntimeError, match="deliberate validation interruption"):
        terrain.prepare_terrain(config, grid, output, interrupt_after_checkpoint)
    raster = gdal.Open(str(output))
    assert np.any(raster.ReadAsArray() != raster.GetRasterBand(1).GetNoDataValue())
    raster = None
    before = hashlib.sha256(output.read_bytes()).hexdigest()
    def must_not_rebuild(*args, **kwargs):
        raise AssertionError("A durable raster checkpoint must not be recomputed")
    monkeypatch.setattr(terrain, "_interpolate_tiles", must_not_rebuild)
    report = terrain.prepare_terrain(config, grid, output)
    assert report["raster_checkpoint_reused"] is True
    assert report["validation_resumed_at"] == 20
    assert report["heldout_spots"]["evaluated"] + report["heldout_spots"]["unsupported"] == 30
    assert report["heldout_spots"]["rmse_m"] < 1e-12
    assert hashlib.sha256(output.read_bytes()).hexdigest() == before
