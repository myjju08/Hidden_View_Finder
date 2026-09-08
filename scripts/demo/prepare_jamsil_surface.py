#!/usr/bin/env python3
"""Prepare a separate Jamsil screening surface from inspected local sources.

No network access and no source edits. Run prepare_jamsil_terrain.py first, then
this script without --prepare to inspect the concrete storage/processing plan.
The published GBA height estimates remain unchanged, including the severe
underestimate at Lotte World Tower. The manifest records that limitation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import resource
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from osgeo import gdal, ogr  # noqa: E402
from pyproj import Transformer  # noqa: E402

from acquire_terrain import VERTICAL_REFERENCE  # noqa: E402
from seoul_visibility.prepare import plan, prepare  # noqa: E402
from seoul_visibility.resources import preflight  # noqa: E402

DATA = ROOT / "data"
BOUNDS = [205560, 542395, 212560, 549395]
TARGET = {"lon": 127.1025, "lat": 37.5125, "height_m": 555, "height_reference": "agl"}


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_record(path: Path, value: dict) -> None:
    body = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != body:
            raise FileExistsError(f"Existing differing record preserved: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".writing")
    with temporary.open("x", encoding="utf-8") as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def subset_buildings() -> tuple[Path, dict]:
    source = DATA / "acquisition/buildings/gba_seoul_2025.gpkg"
    catalog_path = source.with_suffix(".source.json")
    if not source.is_file() or not catalog_path.is_file():
        raise FileNotFoundError("Acquire the inspected local GBA city subset first; no empty-building fallback is permitted")
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    source_hash = digest(source)
    if source_hash != catalog["sha256"]:
        raise ValueError("GBA source fingerprint differs from acquisition provenance; source preserved")
    target = DATA / "seoul/jamsil-inputs/buildings_local.gpkg"
    record_path = target.with_suffix(".provenance.json")
    if target.exists():
        metadata = json.loads(record_path.read_text(encoding="utf-8"))
        if metadata["source_sha256"] != source_hash or metadata["output_sha256"] != digest(target):
            raise ValueError("Jamsil building subset fingerprint mismatch; existing files preserved")
        return target, metadata

    selection = [BOUNDS[0] - 1000, BOUNDS[1] - 1000, BOUNDS[2] + 1000, BOUNDS[3] + 1000]
    lonlat = Transformer.from_crs(5186, 4326, always_xy=True).transform_bounds(*selection, densify_pts=21)
    domain = catalog["bbox_lon_lat"]
    if not (domain[0] <= lonlat[0] and domain[1] <= lonlat[1] and domain[2] >= lonlat[2] and domain[3] >= lonlat[3]):
        raise ValueError("Required Jamsil building selection extends outside the acquired source domain")
    resource_plan = preflight(DATA, additional_bytes=150_000_000, temporary_bytes=100_000_000)
    source_ds = ogr.Open(str(source))
    source_layer = source_ds.GetLayer(0)
    source_crs = source_layer.GetSpatialRef()
    if source_crs is None or not source_crs.IsGeographic():
        raise ValueError("Expected inspected geographic source CRS; inspect changed source metadata before reprojection")
    schema = {source_layer.GetLayerDefn().GetFieldDefn(i).GetName():
              source_layer.GetLayerDefn().GetFieldDefn(i).GetTypeName()
              for i in range(source_layer.GetLayerDefn().GetFieldCount())}
    if "height_m" not in schema or catalog.get("height_units") != "m" or catalog.get("height_reference") != "AGL":
        raise ValueError("Explicit GBA AGL height_m metre mapping no longer matches the source")
    source_crs_wkt = source_crs.ExportToWkt()
    source_ds = None

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name("buildings_local.writing.gpkg")
    if temporary.exists():
        raise FileExistsError(f"Existing partial selection preserved for inspection: {temporary}")
    started = time.perf_counter()
    gdal.UseExceptions()
    result = gdal.VectorTranslate(str(temporary), str(source), options=gdal.VectorTranslateOptions(
        format="GPKG", dstSRS="EPSG:5186", spatFilter=lonlat,
        layerName="buildings", geometryType="PROMOTE_TO_MULTI", dim="XY",
        layerCreationOptions=["SPATIAL_INDEX=YES"], transactionSize=1000))
    layer = result.GetLayer(0)
    count = layer.GetFeatureCount()
    missing = invalid = holes = 0
    minimum, maximum = math.inf, -math.inf
    at_target = []
    tx, ty = Transformer.from_crs(4326, 5186, always_xy=True).transform(TARGET["lon"], TARGET["lat"])
    point = ogr.Geometry(ogr.wkbPoint)
    point.AddPoint_2D(tx, ty)
    for feature in layer:
        height = feature.GetField("height_m")
        usable = height is not None and math.isfinite(height) and height > 0
        missing += not usable
        if usable:
            minimum, maximum = min(minimum, height), max(maximum, height)
        geometry = feature.GetGeometryRef()
        invalid += not geometry.IsValid()
        holes += any(geometry.GetGeometryRef(i).GetGeometryCount() > 1 for i in range(geometry.GetGeometryCount()))
        if geometry.Intersects(point):
            at_target.append({"source": feature.GetField("source"), "source_id": feature.GetField("source_id"),
                              "height_m": height, "height_is_estimated": True})
    result.FlushCache()
    result = layer = None
    os.replace(temporary, target)
    metadata = {
        "source": str(source), "source_sha256": source_hash,
        "source_record": str(catalog_path), "output_sha256": digest(target), "features": count,
        "selection_bounds_epsg5186": selection, "selection_bbox_wgs84": lonlat,
        "source_acquired_domain_wgs84": domain,
        "source_crs_wkt": source_crs_wkt, "output_crs": "EPSG:5186", "source_schema": schema,
        "geometry_policy": "Full intersecting ordinary 2D footprints retained; multipart polygons and holes retained; no geometry clipping",
        "invalid_geometry_before_engine_repair": invalid, "features_with_holes": holes,
        "height_field": "height_m", "height_reference": "AGL", "height_units": "m",
        "height_policy": "Published GBA ML height estimates unchanged; all estimated; no floor conversion or target-building replacement",
        "unresolved_heights": int(missing), "height_min_m": minimum, "height_max_m": maximum,
        "source_dates": "Height imagery primarily 2019, 2018 fallback; mixed footprint dates; 2025 publication",
        "target_probe": TARGET, "target_intersecting_footprints": at_target,
        "target_height_limitation": "Published GBA footprints at the Lotte World Tower target have heights far below the 555 m structural height. Retaining these estimates under-models the tower obstruction. A 555 m AGL target is an approximate top point, not a surveyed absolute apex elevation or proof of whole-tower visibility.",
        "coverage_limitation": "Acquired dataset domain covers the complete computational grid; completeness/currentness of real building detection is unverified",
        "elapsed_s": time.perf_counter() - started, "preflight": resource_plan,
    }
    write_record(record_path, metadata)
    return target, metadata


def make_config(buildings: Path, inspection: dict) -> dict:
    dtm = DATA / "seoul/jamsil-terrain/dtm.tif"
    validation = dtm.parent / "terrain_validation.json"
    if not dtm.exists() or not validation.exists():
        raise FileNotFoundError("Jamsil terrain is not complete: run scripts/demo/prepare_jamsil_terrain.py and inspect terrain_validation.json")
    terrain_validation = json.loads(validation.read_text(encoding="utf-8"))
    ds = gdal.Open(str(dtm))
    expected = (205560, 5, 0, 549395, 0, -5)
    if ds.RasterXSize != 1400 or ds.RasterYSize != 1400 or ds.GetGeoTransform() != expected:
        raise ValueError("Terrain grid differs from the fixed, aligned Jamsil 5 m bounds")
    ds = None
    boundary = DATA / "acquisition/boundary/seoul_boundary_osm_20260907.geojson"
    base_justification = (
        "Explicit screening approximation: highest valid DTM under the full all-touched footprint plus unchanged supplied AGL estimate. "
        "This may overestimate roof elevation relative to a median base and falsely block rays; no guaranteed visibility bound. "
        "Missing support remains unresolved and strict query windows reject it.")
    return {
        "data_root": str(DATA), "output_dir": str(DATA / "seoul/processed/jamsil-gba-maximum"),
        "crs": "EPSG:5186", "vertical_reference": VERTICAL_REFERENCE,
        "resolution_m": 5, "bounds": BOUNDS, "source_kind": "local",
        "terrain": {"kind": "raster", "path": str(dtm), "bare_earth_verified": True,
                    "units": "m", "vertical_reference": VERTICAL_REFERENCE, "source_date": "2023",
                    "coverage_boundary": {"path": str(boundary)}},
        "buildings": {"path": str(buildings), "layer": "buildings", "height_field": "height_m",
                      "height_is_agl": True, "height_is_estimated": True,
                      "height_estimation_method": "Published GBA ML-derived AGL heights, primarily 2019 imagery (2018 fallback); release 2025",
                      "units": "m", "vertical_reference": VERTICAL_REFERENCE,
                      "coverage_bounds": BOUNDS, "base_estimation_method": "maximum",
                      "base_estimation_justification": base_justification,
                      "invalid_geometry": "repair", "source_date": "2018/2019 heights; 2025 release"},
        "output_boundary": {"path": str(boundary)},
        "provenance": {
            "terrain_catalog": "https://data.seoul.go.kr/dataList/OA-22241/F/1/datasetView.do",
            "terrain_credit": "Seoul Open Data / NGII 2023 contours and spot heights; KOGL Type 1",
            "terrain_validation_path": str(validation), "terrain_validation": terrain_validation,
            "buildings_catalog": "https://github.com/zhu-xlab/GlobalBuildingAtlas",
            "building_subset_inspection": inspection,
            "boundary_credit": "© OpenStreetMap contributors, ODbL 1.0, relation 2297418; retrieved 2026-09-07",
            "demonstration_target": TARGET,
        },
        "limitations": [
            "REAL INPUTS / APPROXIMATE: 2023 contour terrain and estimated GBA building heights; not a current measured-height survey.",
            inspection["target_height_limitation"],
            "5 m cell spacing is not 5 m physical accuracy. Contour TIN is unconstrained; all-touched footprints can close narrow gaps.",
            "GBA omits or misestimates some structures. Trees, walls, balconies, construction and atmospheric visibility are not modeled.",
            "Unknown terrain support, partially supported footprints and unresolved heights are retained and reject affected query windows.",
            "No vertical datum conversion; original NGII Incheon mean sea level source convention retained. AGL means height above DTM, not roof.",
            "Only a specified target point is analyzed. No whole-tower, whole-lake, public-access or scene-composition guarantee.",
            "GBA height estimates and some footprints: CC BY-NC 4.0; OSM/Microsoft footprints: ODbL. Retain source-specific attribution and terms.",
            base_justification,
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inspect-only", action="store_true", help="Select and inspect building inputs without requiring the DTM")
    parser.add_argument("--prepare", action="store_true", help="Execute after printing the concrete plan")
    args = parser.parse_args()
    buildings, inspection = subset_buildings()
    print(json.dumps({"building_inspection": inspection}, ensure_ascii=False, indent=2), flush=True)
    if args.inspect_only:
        return
    config = make_config(buildings, inspection)
    config_path = DATA / "seoul/jamsil-inputs/surface_config.json"
    write_record(config_path, config)
    report = plan(config_path)
    report_path = config_path.with_name("surface_plan.json")
    if not report_path.exists():
        write_record(report_path, report)
    print(json.dumps({"config": str(config_path), "plan": report}, ensure_ascii=False, indent=2), flush=True)
    if args.prepare:
        started = time.perf_counter()
        manifest = prepare(config_path)
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1024 if sys.platform != "darwin" else 1)
        execution = {"manifest": str(manifest), "prepare_or_verified_resume_s": time.perf_counter() - started,
                     "process_peak_rss_bytes": peak, "preflight_after": preflight(DATA, additional_bytes=0)}
        execution_path = config_path.with_name("surface_preparation.json")
        if not execution_path.exists():
            write_record(execution_path, execution)
        print(json.dumps(execution, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
