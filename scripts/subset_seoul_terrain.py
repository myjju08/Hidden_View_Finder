#!/usr/bin/env python3
"""Make a bounded, indexed Seoul contour/spot subset; optionally prepare a pilot DTM.

Source vectors and elevation attributes are preserved. Contour geometry is
clipped in the destination CRS so long citywide features do not escape the halo.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import time

from osgeo import gdal, ogr
from pyproj import Transformer

from seoul_visibility.resources import preflight, tree_bytes
from seoul_visibility.terrain import prepare_terrain, _build_sample_index

from acquire_terrain import ARCHIVE_SHA256, VERTICAL_REFERENCE, acquire

SOURCE_ROOT = Path("data/acquisition/terrain_research/source")
WORK_BOUNDS = [192500.0, 547500.0, 204000.0, 559000.0]
PILOT_BOUNDS = [195500.0, 550500.0, 200500.0, 555500.0]


def _json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".writing")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    os.replace(temporary, path)


def _hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def subset(output: Path, bounds: list[float], halo_m: float, data_root: Path) -> dict:
    if len(bounds) != 4 or not all(math.isfinite(x) for x in bounds) or bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
        raise ValueError("Bounds must be finite ordered xmin,ymin,xmax,ymax in EPSG:5186")
    if not math.isfinite(halo_m) or halo_m < 0:
        raise ValueError("Input halo must be finite and nonnegative")
    output = output.resolve()
    data_root = data_root.resolve()
    if not output.is_relative_to(data_root):
        raise ValueError("Output must be inside accounted project data root")
    # Verify the immutable archive and every retained source member before reuse;
    # geometry-only hashes would miss altered DBF elevation attributes.
    acquire(SOURCE_ROOT.parent, data_root)
    extent = [bounds[0] - halo_m, bounds[1] - halo_m, bounds[2] + halo_m, bounds[3] + halo_m]
    report_path = output.with_suffix(".provenance.json")
    if output.exists():
        if report_path.exists():
            report = json.loads(report_path.read_text())
            if report["source_archive_sha256"] == ARCHIVE_SHA256 and report["clip_bounds_epsg5186"] == extent and report["output_sha256"] == _hash(output):
                return {**report, "reused": True}
        raise FileExistsError(f"Existing subset cannot be verified; preserved: {output}")
    budget = preflight(data_root, additional_bytes=150_000_000, temporary_bytes=150_000_000)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.stem + ".writing.gpkg")
    if temporary.exists():
        raise FileExistsError(f"Pre-existing temporary artifact preserved: {temporary}")
    gdal.UseExceptions()
    start = time.perf_counter()
    # Densified inverse bounds form a native source filter; the exact crop is
    # performed after horizontal reprojection by GDAL's -clipdst operation.
    source_extent = Transformer.from_crs(5186, 5174, always_xy=True).transform_bounds(*extent, densify_pts=21)
    layers = []
    sources = [(SOURCE_ROOT / "등고선 5000/N3L_F001.shp", "contours", "CONT", "PROMOTE_TO_MULTI"),
               (SOURCE_ROOT / "표고 5000/N3P_F002.shp", "spots", "NUME", "POINT")]
    try:
        for source_path, name, field, geometry in sources:
            source = gdal.OpenEx(str(source_path.resolve()), gdal.OF_VECTOR, open_options=["ENCODING=CP949"])
            if source.GetLayer(0).GetSpatialRef().GetAuthorityCode(None) != "5174":
                raise ValueError("Original terrain CRS differs from inspected EPSG:5174")
            dataset = gdal.VectorTranslate(str(temporary), source, options=gdal.VectorTranslateOptions(
                format="GPKG", accessMode="update" if temporary.exists() else None,
                dstSRS="EPSG:5186", spatFilter=source_extent, clipDst=extent,
                layerName=name, geometryType=geometry, dim="XY", transactionSize=1000,
                layerCreationOptions=["SPATIAL_INDEX=YES"],
            ))
            dataset = None
            original_layer = source.GetLayer(0)
            original_values = {feature["UFID"]: float(feature[field]) for feature in original_layer}
            verification = gdal.OpenEx(str(temporary), gdal.OF_VECTOR)
            layer = verification.GetLayerByName(name)
            if layer is None or layer.GetSpatialRef().GetAuthorityCode(None) != "5186":
                raise ValueError("Output CRS/layer verification failed")
            count, vertices = 0, 0
            minimum, maximum = math.inf, -math.inf
            for feature in layer:
                geom = feature.GetGeometryRef()
                envelope = geom.GetEnvelope()
                if envelope[0] < extent[0] - 1e-6 or envelope[1] > extent[2] + 1e-6 or envelope[2] < extent[1] - 1e-6 or envelope[3] > extent[3] + 1e-6:
                    raise ValueError("Clipped geometry escaped requested input halo")
                elevation = float(feature[field])
                if not math.isfinite(elevation) or elevation != original_values[feature["UFID"]]:
                    raise ValueError("Supplied elevation changed during horizontal subset/reprojection")
                count += 1
                minimum, maximum = min(minimum, elevation), max(maximum, elevation)
                vertices += geom.GetPointCount() or sum(geom.GetGeometryRef(i).GetPointCount() for i in range(geom.GetGeometryCount()))
            if count == 0:
                raise ValueError(f"No {name} features inside requested input halo")
            layers.append({"layer": name, "source_path": str(source_path.resolve()), "source_sha256": _hash(source_path),
                           "elevation_field": field, "feature_count": count, "vertex_count": vertices,
                           "elevation_min_m": minimum, "elevation_max_m": maximum,
                           "elevation_attribute_unchanged": True, "geometry_clipped_to_input_halo": True})
            layer = verification = original_layer = source = None
            preflight(data_root)
        os.rename(temporary, output)
    except Exception:
        # Only newly created artifacts belonging to this invocation are removed.
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(str(temporary) + suffix).unlink(missing_ok=True)
        raise
    report = {"source_archive_sha256": ARCHIVE_SHA256, "output": str(output), "output_sha256": _hash(output),
              "output_bytes": output.stat().st_size, "source_crs": "EPSG:5174", "crs": "EPSG:5186",
              "requested_bounds_epsg5186": bounds, "input_halo_m": halo_m, "clip_bounds_epsg5186": extent,
              "vertical_reference": VERTICAL_REFERENCE, "vertical_transform": "none; original CONT/NUME metres retained",
              "layers": layers, "seconds": time.perf_counter() - start, "preflight": budget, "reused": False}
    _json(report_path, report)
    return report


def pilot(data_root: Path) -> dict:
    directory = data_root / "seoul/pilot-terrain-regular"
    # Reuse the already clipped source subset, while a separate derived output
    # prevents the old preserve-every-vertex sample index from being reused.
    source = data_root / "seoul/pilot-terrain/terrain_input.gpkg"
    source_report = subset(source, PILOT_BOUNDS, 1000.0, data_root)
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / "dtm.tif"
    report_path = directory / "terrain_validation.json"
    if output.exists():
        if report_path.exists():
            report = json.loads(report_path.read_text())
            if report.get("output_sha256") == _hash(output):
                return report
        raise FileExistsError(f"Unverified existing pilot terrain preserved: {output}")
    config = {"kind": "samples", "units": "m", "vertical_reference": VERTICAL_REFERENCE,
              "sources": [{"path": str(source.resolve()), "layer": x["layer"], "crs": "EPSG:5186",
                           "elevation_field": x["elevation_field"], "units": "m", "vertical_reference": VERTICAL_REFERENCE}
                          for x in source_report["layers"]],
              "sample_spacing_m": 10.0, "max_sample_points": 2_000_000, "max_points_per_tile": 100_000,
              "max_triangle_edge_m": 250.0, "halo_m": 500.0, "tile_size": 256,
              "contour_sampling": "regular_arclength", "max_validation_spots": 500, "validation_seed": 0,
              "duplicate_elevation_tolerance_m": 0.01}
    bounds = PILOT_BOUNDS
    grid = {"crs": "EPSG:5186", "resolution_m": 5.0, "bounds": bounds,
            "width": int((bounds[2] - bounds[0]) / 5), "height": int((bounds[3] - bounds[1]) / 5),
            "transform": [bounds[0], 5.0, 0.0, bounds[3], 0.0, -5.0]}
    _json(directory / "terrain_config.json", {"terrain": config, "grid": grid})
    preflight(data_root, additional_bytes=500_000_000, temporary_bytes=500_000_000)
    start = time.perf_counter()
    last_message = [start]
    def check() -> None:
        preflight(data_root)
        now = time.perf_counter()
        if now - last_message[0] > 30:
            print(json.dumps({"stage": "pilot_terrain", "elapsed_s": now - start,
                              "artifact_bytes": tree_bytes(directory)}), flush=True)
            last_message[0] = now
    temporary = output.with_name("dtm.writing.tif")
    try:
        validation = prepare_terrain(config, grid, temporary, check)
    except Exception as exc:
        if not (directory / "terrain_raster_checkpoint.json").exists():
            temporary.unlink(missing_ok=True)
        _json(directory / "terrain_failure.json", {"error_type": type(exc).__name__, "message": str(exc),
              "seconds": time.perf_counter() - start, "status": "not_ready", "source_archive_sha256": ARCHIVE_SHA256})
        raise
    os.rename(temporary, output)
    report = {"status": "terrain_only_not_engine_ready", "output": str(output.resolve()), "grid": grid,
              "source_archive_sha256": ARCHIVE_SHA256, "seconds": time.perf_counter() - start,
              "validation": validation, "output_bytes": output.stat().st_size, "output_sha256": _hash(output),
              "input_subset": source_report, "limitations": ["No building input included yet; not a visibility-engine manifest",
              "Unconstrained sampled-contour local TIN approximation", "Input halo is distinct from TIN tile halo"]}
    _json(report_path, report)
    return report


def full(data_root: Path) -> dict:
    """Prepare the explicit 500 m-support local product, preserving the pilot."""
    directory = data_root / "seoul/full-terrain-500m"
    source = data_root / "seoul/inputs/terrain_local.gpkg"
    source_report = subset(source, WORK_BOUNDS, 1000.0, data_root)
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / "dtm.tif"
    report_path = directory / "terrain_validation.json"
    if output.exists():
        if report_path.exists():
            report = json.loads(report_path.read_text())
            if report.get("output_sha256") == _hash(output):
                return report
        raise FileExistsError(f"Unverified existing full terrain preserved: {output}")
    config = {"kind": "samples", "units": "m", "vertical_reference": VERTICAL_REFERENCE,
              "sources": [{"path": str(source.resolve()), "layer": x["layer"], "crs": "EPSG:5186",
                           "elevation_field": x["elevation_field"], "units": "m", "vertical_reference": VERTICAL_REFERENCE}
                          for x in source_report["layers"]],
              "sample_spacing_m": 10.0, "max_sample_points": 2_000_000, "max_points_per_tile": 100_000,
              "max_triangle_edge_m": 500.0, "halo_m": 1000.0,
              "contour_sampling": "regular_arclength", "max_validation_spots": 500, "validation_seed": 0,
              "duplicate_elevation_tolerance_m": 0.01,
              "support_justification": "Pilot interior convex-hull triangles with 254-325 m edges were unsupported by 250 m limit; explicitly allow at most 500 m edges, retaining NoData outside support/hulls"}
    bounds = WORK_BOUNDS
    grid = {"crs": "EPSG:5186", "resolution_m": 5.0, "bounds": bounds,
            "width": int((bounds[2] - bounds[0]) / 5), "height": int((bounds[3] - bounds[1]) / 5),
            "transform": [bounds[0], 5.0, 0.0, bounds[3], 0.0, -5.0]}
    budget = preflight(data_root, additional_bytes=750_000_000, temporary_bytes=750_000_000)
    started = time.perf_counter()
    last_message = [started]
    def check() -> None:
        preflight(data_root)
        now = time.perf_counter()
        if now - last_message[0] > 30:
            raster_progress = directory / "terrain_raster_progress.json"
            validation_progress = directory / "terrain_validation_checkpoint.json"
            print(json.dumps({"stage": "full_terrain", "elapsed_s": now - started,
                              "artifact_bytes": tree_bytes(directory),
                              "raster_progress": json.loads(raster_progress.read_text()) if raster_progress.exists() else None,
                              "validation_progress": json.loads(validation_progress.read_text()) if validation_progress.exists() else None}), flush=True)
            last_message[0] = now
    index_path = directory / "terrain_samples.sqlite"
    info_path = directory / "terrain_samples.json"
    index_started = time.perf_counter()
    if not (index_path.exists() and info_path.exists()):
        # This path is dedicated to this source/grid/sampling configuration.
        index_path.unlink(missing_ok=True)
        summary = _build_sample_index(config, grid, index_path, check)
        _json(info_path, summary)
    index_seconds = time.perf_counter() - index_started
    counts_by_size = {}
    # Count the actual indexed points in every required tile+halo before Qhull.
    # Native RTree counts are bounded and avoid materializing coordinate grids.
    with sqlite3.connect(index_path) as connection:
        for tile_size in (256, 128, 64, 32, 16):
            maximum = 0
            for y in range(0, grid["height"], tile_size):
                for x in range(0, grid["width"], tile_size):
                    xmin = bounds[0] + x * 5 - 1000
                    xmax = bounds[0] + min(x + tile_size, grid["width"]) * 5 + 1000
                    ymax = bounds[3] - y * 5 + 1000
                    ymin = bounds[3] - min(y + tile_size, grid["height"]) * 5 - 1000
                    count = connection.execute("SELECT count(*) FROM spatial WHERE maxx>=? AND minx<=? AND maxy>=? AND miny<=?",
                                               (xmin, xmax, ymin, ymax)).fetchone()[0]
                    maximum = max(maximum, count)
            counts_by_size[str(tile_size)] = maximum
            if maximum <= config["max_points_per_tile"]:
                config["tile_size"] = tile_size
                break
        else:
            raise RuntimeError("Actual 1000 m halos exceed point budget even at tile16; no DTM interpolation attempted")
    _json(directory / "terrain_config.json", {"terrain": config, "grid": grid,
                                               "max_indexed_points_by_tile_size": counts_by_size})
    print(json.dumps({"stage": "full_terrain_index_preflight", "index_seconds": index_seconds,
                      "max_indexed_points_by_tile_size": counts_by_size, "chosen_tile_size": config["tile_size"]}), flush=True)
    temporary = output.with_name("dtm.writing.tif")
    dtm_started = time.perf_counter()
    try:
        validation = prepare_terrain(config, grid, temporary, check)
    except Exception as exc:
        if not (directory / "terrain_raster_checkpoint.json").exists():
            temporary.unlink(missing_ok=True)
        _json(directory / "terrain_failure.json", {"error_type": type(exc).__name__, "message": str(exc),
              "seconds": time.perf_counter() - started, "status": "not_ready", "source_archive_sha256": ARCHIVE_SHA256})
        raise
    os.rename(temporary, output)
    report = {"status": "terrain_only_not_engine_ready", "output": str(output.resolve()), "grid": grid,
              "source_archive_sha256": ARCHIVE_SHA256, "seconds": time.perf_counter() - started,
              "timings_s": {"index_build_or_resume": index_seconds, "dtm_and_validation": time.perf_counter() - dtm_started},
              "max_indexed_points_by_tile_size": counts_by_size, "validation": validation,
              "output_bytes": output.stat().st_size, "output_sha256": _hash(output),
              "input_subset": source_report, "preflight": budget,
              "limitations": ["No buildings included in this terrain-only product",
                              "Explicit 500 m interpolation support is approximate; original pilot250m is preserved",
                              "NoData retained outside local convex hull or 500 m triangle-edge support"]}
    _json(report_path, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/seoul/inputs/terrain_local.gpkg"))
    parser.add_argument("--bounds", nargs=4, type=float, default=WORK_BOUNDS)
    parser.add_argument("--halo-m", type=float, default=1000.0)
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    parser.add_argument("--prepare-pilot", action="store_true")
    parser.add_argument("--prepare-full", action="store_true")
    args = parser.parse_args()
    print(json.dumps(subset(args.output, args.bounds, args.halo_m, args.data_root), ensure_ascii=False, indent=2), flush=True)
    if args.prepare_pilot:
        print(json.dumps(pilot(args.data_root), ensure_ascii=False, indent=2), flush=True)
    if args.prepare_full:
        print(json.dumps(full(args.data_root), ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
