from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from osgeo import gdal, ogr, osr
import pytest

from seoul_visibility.errors import ConfigurationError, ResourceBudgetError
from seoul_visibility.inspect import inspect_paths
from seoul_visibility.prepare import plan, prepare
from seoul_visibility.terrain import _linear_supported


def raster(path: Path, values: np.ndarray, *, resolution=5, nodata=-9999) -> Path:
    ds = gdal.GetDriverByName("GTiff").Create(str(path), values.shape[1], values.shape[0], 1, gdal.GDT_Float32)
    ds.SetGeoTransform([200000, resolution, 0, 550000, 0, -resolution])
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(5186)
    ds.SetProjection(srs.ExportToWkt())
    ds.GetRasterBand(1).SetNoDataValue(nodata)
    ds.GetRasterBand(1).SetUnitType("m")
    ds.GetRasterBand(1).WriteArray(values)
    ds = None
    return path


def vectors(path: Path, rows: list[tuple[str, float | None, int | None]], *, crs=True) -> Path:
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(str(path))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(5186)
    layer = ds.CreateLayer("footprints", srs if crs else None, ogr.wkbUnknown)
    layer.CreateField(ogr.FieldDefn("measured", ogr.OFTReal))
    layer.CreateField(ogr.FieldDefn("above", ogr.OFTInteger))
    for wkt, height, floors in rows:
        f = ogr.Feature(layer.GetLayerDefn())
        f.SetGeometry(ogr.CreateGeometryFromWkt(wkt))
        if height is not None:
            f.SetField("measured", height)
        if floors is not None:
            f.SetField("above", floors)
        layer.CreateFeature(f)
    ds = None
    return path


def rectangle(x1, y1, x2, y2):
    return f"POLYGON(({x1} {y1},{x2} {y1},{x2} {y2},{x1} {y2},{x1} {y1}))"


@pytest.fixture
def config(tmp_path):
    raw = raster(tmp_path / "dtm.tif", np.full((20, 20), 100, dtype=np.float32))
    return {"data_root": str(tmp_path), "output_dir": str(tmp_path / "prepared"),
            "crs": "EPSG:5186", "vertical_reference": "fictional orthometric test datum",
            "resolution_m": 5, "bounds": [200000, 549900, 200100, 550000],
            "source_kind": "synthetic",
            "terrain": {"kind": "raster", "path": str(raw), "units": "m", "bare_earth_verified": True,
                        "vertical_reference": "fictional orthometric test datum"},
            "buildings": {"assume_empty": True, "justification": "deterministic empty fixture",
                          "coverage_bounds": [200000, 549900, 200100, 550000]},
            "storage_policy": {"minimum_free_bytes": 0}}


def set_buildings(config, path, **extra):
    config["buildings"] = {"path": str(path), "layer": "footprints", "height_field": "measured",
                           "height_is_agl": True, "units": "m",
                           "vertical_reference": config["vertical_reference"],
                           "coverage_bounds": config["bounds"], **extra}


def read_product(manifest_path, name):
    manifest = json.loads(manifest_path.read_text())
    ds = gdal.Open(str(manifest_path.parent / manifest["products"]["5"][name]))
    return ds.ReadAsArray()


def test_raster_prepare_inspect_and_resume(config):
    inventory = inspect_paths([config["terrain"]["path"]])
    assert inventory["sources"][0]["bands"][0]["min"] == 100
    assert inventory["sources"][0]["source_date"] is None
    report = plan(config)
    assert report["grid"]["transform"] == [200000, 5, 0, 550000, 0, -5]
    manifest = prepare(config)
    assert np.all(read_product(manifest, "dtm") == 100)
    assert np.all(read_product(manifest, "quality") == 3)
    assert np.all(read_product(manifest, "occupancy") == 0)
    assert prepare(config) == manifest
    assert not list(manifest.parent.glob("*.writing*"))


def test_buildings_overlap_holes_invalid_and_duplicate(config, tmp_path):
    outer_with_hole = "POLYGON((200010 549910,200090 549910,200090 549990,200010 549990,200010 549910),(200035 549935,200035 549965,200065 549965,200065 549935,200035 549935))"
    low = rectangle(200010, 549910, 200030, 549990)
    bowtie = "POLYGON((200075 549940,200085 549960,200075 549960,200085 549940,200075 549940))"
    path = vectors(tmp_path / "buildings.gpkg", [(outer_with_hole, 30, None), (low, 10, None),
                                                (low, 10, None), (bowtie, 40, None)])
    set_buildings(config, path)
    manifest = prepare(config)
    surface, occupied = read_product(manifest, "surface"), read_product(manifest, "occupancy")
    assert surface[10, 3] == 130  # later lower footprint must not replace roof
    assert surface[10, 10] == 100 and occupied[10, 10] == 0  # courtyard survives
    assert np.max(surface) == 140
    details = json.loads(manifest.read_text())["processing"]["buildings"]
    assert details["duplicates_removed"] == 1
    assert details["invalid_repaired"] == 1


def test_unknown_height_and_estimate_flags(config, tmp_path):
    path = vectors(tmp_path / "buildings.gpkg", [(rectangle(200010, 549910, 200030, 549930), 0, 4),
                                                (rectangle(200060, 549960, 200080, 549980), None, None)])
    set_buildings(config, path, floors_field="above", floors_are_above_ground=True, floor_height_m=3)
    manifest = prepare(config)
    q, s = read_product(manifest, "quality"), read_product(manifest, "surface")
    assert q[16, 4] & 4
    assert s[16, 4] == 112
    assert q[6, 14] & 8


def test_missing_terrain_and_partial_building_coverage(config):
    values = np.full((20, 20), 100, dtype=np.float32)
    values[10, 10] = -9999
    raster(Path(config["terrain"]["path"]), values)
    config["buildings"]["coverage_bounds"] = [200010, 549910, 200090, 549990]
    manifest = prepare(config)
    quality = read_product(manifest, "quality")
    assert quality[10, 10] & 1 == 0
    assert quality[0, 0] & 2 == 0
    assert quality[4, 4] == 3


def test_units_datum_fields_resolution_and_budget_errors(config, tmp_path):
    config["terrain"]["units"] = "unknown"
    with pytest.raises(ConfigurationError, match="units"):
        plan(config)
    config["terrain"]["units"] = "m"
    config["terrain"]["vertical_reference"] = "ellipsoidal"
    with pytest.raises(ConfigurationError, match="vertical"):
        plan(config)
    config["terrain"]["vertical_reference"] = config["vertical_reference"]
    config["resolution_m"] = 2
    with pytest.raises(ConfigurationError, match="upscale"):
        prepare(config)
    config["resolution_m"] = 5
    config["storage_policy"]["total_budget_bytes"] = 1
    with pytest.raises(ResourceBudgetError):
        prepare(config)
    assert Path(config["terrain"]["path"]).exists()
    assert not Path(config["output_dir"]).exists()


def test_linear_tin_support_and_narrow_triangle():
    points = np.array([[0, 0, 0], [10, 0, 10], [0, 10, 20]], dtype=float)
    values = _linear_supported(points, np.array([[2, 3], [11, 0]]), 20)
    assert values[0] == pytest.approx(8)
    assert np.isnan(values[1])
    assert np.isnan(_linear_supported(points, np.array([[2, 3]]), 5)[0])


def test_contour_spot_prepare_and_duplicate_conflict(config, tmp_path):
    path = tmp_path / "spots.gpkg"
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(str(path))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(5186)
    layer = ds.CreateLayer("spots", srs, ogr.wkbPoint)
    layer.CreateField(ogr.FieldDefn("z", ogr.OFTReal))
    for x, y in [(199950, 549850), (200150, 549850), (199950, 550050), (200150, 550050), (200050, 549950)]:
        feature = ogr.Feature(layer.GetLayerDefn())
        feature.SetGeometry(ogr.CreateGeometryFromWkt(f"POINT({x} {y})"))
        feature.SetField("z", 100 + (x - 200000) / 10)
        layer.CreateFeature(feature)
    ds = None
    config["terrain"] = {"kind": "samples", "sources": [{"path": str(path), "layer": "spots", "elevation_field": "z"}],
                         "units": "m", "vertical_reference": config["vertical_reference"],
                         "max_sample_points": 1000, "max_triangle_edge_m": 300, "halo_m": 600, "tile_size": 16}
    manifest = prepare(config)
    assert read_product(manifest, "dtm")[10, 10] == pytest.approx(105.25)
    details = json.loads(manifest.read_text())["processing"]["terrain"]
    assert details["heldout_spots"]["evaluated"] == 1
    assert details["heldout_spots"]["rmse_m"] < 1e-8


def test_plan_checks_mapping_before_creating_any_stage(config, tmp_path):
    path = vectors(tmp_path / "buildings.gpkg", [(rectangle(200010, 549910, 200030, 549930), 20, None)])
    set_buildings(config, path, height_field="no_such_field")
    with pytest.raises(ConfigurationError, match="does not exist"):
        plan(config)
    assert not list(tmp_path.glob(".prepare-*"))
    config["buildings"]["height_field"] = "measured"
    config["buildings"].pop("height_is_agl")
    with pytest.raises(ConfigurationError, match="height_is_agl"):
        plan(config)
    config["buildings"]["height_is_agl"] = True
    ds = gdal.Open(config["terrain"]["path"], gdal.GA_Update)
    ds.GetRasterBand(1).SetUnitType("ft")
    ds = None
    with pytest.raises(ConfigurationError, match="unit"):
        plan(config)


def test_source_axis_order_and_unknown_crs(config, tmp_path):
    from pyproj import Transformer
    path = tmp_path / "lonlat.gpkg"
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(str(path))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    layer = ds.CreateLayer("footprints", srs, ogr.wkbPolygon)
    layer.CreateField(ogr.FieldDefn("measured", ogr.OFTReal))
    transform = Transformer.from_crs(5186, 4326, always_xy=True)
    points = [transform.transform(x, y) for x, y in [(200010, 549910), (200030, 549910),
                                                    (200030, 549930), (200010, 549930), (200010, 549910)]]
    text = ",".join(f"{x:.12f} {y:.12f}" for x, y in points)
    feature = ogr.Feature(layer.GetLayerDefn())
    feature.SetGeometry(ogr.CreateGeometryFromWkt(f"POLYGON(({text}))"))
    feature.SetField("measured", 20)
    layer.CreateFeature(feature)
    ds = None
    set_buildings(config, path)
    manifest = prepare(config)
    assert read_product(manifest, "surface")[16, 4] == 120
    unknown = vectors(tmp_path / "unknown.gpkg", [(rectangle(200010, 549910, 200030, 549930), 20, None)], crs=False)
    set_buildings(config, unknown)
    with pytest.raises(ConfigurationError, match="unknown CRS"):
        plan(config)


def test_resumable_stages_and_source_invalidation(config, tmp_path, monkeypatch):
    import importlib
    module = importlib.import_module("seoul_visibility.prepare")
    original = module._prepare_surfaces
    def interrupted(*args, **kwargs):
        raise RuntimeError("simulated interruption")
    monkeypatch.setattr(module, "_prepare_surfaces", interrupted)
    with pytest.raises(RuntimeError, match="simulated"):
        prepare(config)
    assert not Path(config["output_dir"]).exists()
    staged = next(tmp_path.glob(".prepare-*/dtm.tif"))
    created = staged.stat().st_mtime_ns
    unrelated = tmp_path / "unrelated.writing.tif"
    unrelated.write_text("preserve me")
    monkeypatch.setattr(module, "_prepare_surfaces", original)
    manifest = prepare(config)
    assert (manifest.parent / "dtm.tif").stat().st_mtime_ns == created
    assert unrelated.read_text() == "preserve me"
    ds = gdal.Open(config["terrain"]["path"], gdal.GA_Update)
    ds.GetRasterBand(1).WriteArray(np.array([[101]], dtype=np.float32), 10, 10)
    ds = None
    with pytest.raises(ConfigurationError, match="already exists"):
        prepare(config)
    assert read_product(manifest, "dtm")[10, 10] == 100


def test_slope_conflict_and_truncated_footprint(config, tmp_path):
    values = np.full((20, 20), 100, dtype=np.float32)
    values[:, 3:5] = 135
    raster(Path(config["terrain"]["path"]), values)
    path = vectors(tmp_path / "buildings.gpkg", [(rectangle(200005, 549910, 200040, 549940), 10, None),
                                                (rectangle(200090, 549910, 200120, 549940), 20, None)])
    set_buildings(config, path)
    manifest = prepare(config)
    quality = read_product(manifest, "quality")
    assert quality[14, 4] & 16
    assert quality[14, 19] & 8


def test_korean_encoding_and_shapefile_sidecars(tmp_path):
    from seoul_visibility.inspect import fingerprint
    path = tmp_path / "korean.shp"
    ds = ogr.GetDriverByName("ESRI Shapefile").CreateDataSource(str(path))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(5186)
    layer = ds.CreateLayer("korean", srs, ogr.wkbPolygon, options=["ENCODING=CP949"])
    layer.CreateField(ogr.FieldDefn("높이", ogr.OFTReal))
    feature = ogr.Feature(layer.GetLayerDefn())
    feature.SetGeometry(ogr.CreateGeometryFromWkt(rectangle(200010, 549910, 200030, 549930)))
    feature.SetField("높이", 20)
    layer.CreateFeature(feature)
    ds = None
    path.with_suffix(".cpg").unlink()
    inventory = inspect_paths([path], encoding="CP949")["sources"][0]
    assert inventory["layers"][0]["fields"]["높이"]["min"] == 20
    suffixes = {Path(file["path"]).suffix for file in fingerprint(path)["files"]}
    assert {".shp", ".dbf", ".prj", ".shx"} <= suffixes


def test_duplicate_terrain_conflicts_are_rejected(config, tmp_path):
    path = tmp_path / "conflict.gpkg"
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(str(path))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(5186)
    layer = ds.CreateLayer("spots", srs, ogr.wkbPoint)
    layer.CreateField(ogr.FieldDefn("z", ogr.OFTReal))
    for x, y, z in [(200000, 549900, 100), (200100, 549900, 100),
                    (200000, 550000, 100), (200000, 549900, 102)]:
        feature = ogr.Feature(layer.GetLayerDefn())
        feature.SetGeometry(ogr.CreateGeometryFromWkt(f"POINT({x} {y})"))
        feature.SetField("z", z)
        layer.CreateFeature(feature)
    ds = None
    config["terrain"] = {"kind": "samples", "sources": [{"path": str(path), "elevation_field": "z"}],
                         "units": "m", "vertical_reference": config["vertical_reference"], "max_sample_points": 1000}
    with pytest.raises(ConfigurationError, match="Conflicting duplicate"):
        prepare(config)
    assert not Path(config["output_dir"]).exists()


def test_dxf_actual_entity_layers_and_explicit_geometry_z_selection(config, tmp_path):
    path = tmp_path / "terrain.dxf"
    ds = ogr.GetDriverByName("DXF").CreateDataSource(str(path))
    layer = ds.CreateLayer("entities", geom_type=ogr.wkbUnknown)
    for name, text in [("contours", "LINESTRING Z (199950 549850 100,200150 549850 100)"),
                       ("contours", "LINESTRING Z (199950 550050 100,200150 550050 100)"),
                       ("spots", "POINT Z (200050 549950 100)"),
                       ("annotations", "LINESTRING Z (200000 549920 999,200100 549980 999)")]:
        feature = ogr.Feature(layer.GetLayerDefn())
        feature.SetField("Layer", name)
        feature.SetGeometry(ogr.CreateGeometryFromWkt(text))
        layer.CreateFeature(feature)
    ds = None
    inventory = inspect_paths([path])["sources"][0]
    entity_layers = inventory["layers"][0]["dxf_entity_layers"]
    assert entity_layers["contours"]["features"] == 2
    assert entity_layers["spots"]["z_nonzero_vertices"] == 1
    source = {"path": str(path), "crs": "EPSG:5186", "elevation_from_z": True}
    config["terrain"] = {"kind": "samples", "sources": [source], "units": "m",
                         "vertical_reference": config["vertical_reference"], "max_sample_points": 1000}
    with pytest.raises(ConfigurationError, match="cad_layers"):
        plan(config)
    source["cad_layers"] = ["contours", "spots"]
    manifest = prepare(config)
    assert np.all(read_product(manifest, "dtm") == 100)


def test_whitespace_unknown_datum_rejected(config):
    config["vertical_reference"] = " unknown "
    with pytest.raises(ConfigurationError, match="vertical reference"):
        plan(config)
