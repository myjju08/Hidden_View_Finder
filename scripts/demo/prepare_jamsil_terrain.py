#!/usr/bin/env python3
"""Prepare a bounded, independently sampled 5 m Jamsil DTM from local Seoul maps.

This uses the existing bounded TIN implementation and preserves all older
products. No missing river/lake terrain is filled or extrapolated. Run from the
repository root with the native-GDAL environment; source acquisition verifies
the existing immutable city-only archive before reuse.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import time

REPOSITORY = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY / "scripts"))
sys.path.insert(0, str(REPOSITORY / "src"))

import numpy as np
from osgeo import gdal
from pyproj import Transformer

from acquire_terrain import ARCHIVE_SHA256, VERTICAL_REFERENCE
from subset_seoul_terrain import _hash, _json, subset
from seoul_visibility.resources import preflight, tree_bytes
from seoul_visibility.terrain import _build_sample_index, prepare_terrain

BOUNDS = [205560.0, 542395.0, 212560.0, 549395.0]
TARGET_LON_LAT = [127.1025, 37.5125]


def terrain_coverage(output: Path, target: list[float]) -> dict:
    """Report supported square/circular terrain windows, not full engine coverage."""
    dataset = gdal.Open(str(output), gdal.GA_ReadOnly)
    band = dataset.GetRasterBand(1)
    array = band.ReadAsArray()
    valid = np.isfinite(array) & (array != band.GetNoDataValue())
    transform = dataset.GetGeoTransform()
    x, y = Transformer.from_crs(4326, 5186, always_xy=True).transform(*target)
    xx = transform[0] + (np.arange(array.shape[1]) + 0.5) * transform[1]
    yy = transform[3] + (np.arange(array.shape[0]) + 0.5) * transform[5]
    radial_squared = (xx[None, :] - x) ** 2 + (yy[:, None] - y) ** 2
    missing_rows, missing_columns = np.where(~valid)
    nearest = (float(np.sqrt(radial_squared[missing_rows, missing_columns].min()))
               if len(missing_rows) else None)
    radii = {}
    for radius in (500, 750, 1000, 1200, 1500, 1800, 2000, 2500, 3000):
        guard_radius = radius + 15
        square = ((np.abs(xx[None, :] - x) <= guard_radius)
                  & (np.abs(yy[:, None] - y) <= guard_radius))
        circle = radial_squared <= guard_radius ** 2
        radii[str(radius)] = {
            "terrain_unknown_in_square_with_15m_guard": int((square & ~valid).sum()),
            "terrain_unknown_in_circle_with_15m_guard": int((circle & ~valid).sum()),
        }
    row = int(math.floor((y - transform[3]) / transform[5]))
    column = int(math.floor((x - transform[0]) / transform[1]))
    return {"target_lon_lat": target, "target_xy_epsg5186": [x, y],
            "target_dtm_m": float(array[row, column]) if valid[row, column] else None,
            "valid_cells": int(valid.sum()), "unknown_cells": int((~valid).sum()),
            "valid_fraction": float(valid.mean()), "nearest_unknown_cell_center_m": nearest,
            "query_radii_m": radii,
            "scope": "Terrain support only; buildings and complete native dependency window require separate validation."}


def prepare(data_root: Path) -> dict:
    gdal.UseExceptions()
    data_root = data_root.resolve()
    directory = data_root / "seoul/jamsil-terrain"
    source = directory / "terrain_input.gpkg"
    output = directory / "dtm.tif"
    report_path = directory / "terrain_validation.json"
    started = time.perf_counter()
    budget = preflight(data_root, additional_bytes=400_000_000, temporary_bytes=400_000_000)
    source_report = subset(source, BOUNDS, 1000.0, data_root)
    if output.exists():
        if report_path.exists():
            report = json.loads(report_path.read_text())
            if report.get("output_sha256") == _hash(output) and report.get("grid", {}).get("bounds") == BOUNDS:
                return {**report, "reused": True}
        raise FileExistsError(f"Unverified existing output preserved: {output}")
    grid = {"crs": "EPSG:5186", "resolution_m": 5.0, "bounds": BOUNDS,
            "width": 1400, "height": 1400,
            "transform": [BOUNDS[0], 5.0, 0.0, BOUNDS[3], 0.0, -5.0]}
    config = {"kind": "samples", "units": "m", "vertical_reference": VERTICAL_REFERENCE,
              "sources": [{"path": str(source.resolve()), "layer": layer["layer"],
                           "crs": "EPSG:5186", "elevation_field": layer["elevation_field"],
                           "units": "m", "vertical_reference": VERTICAL_REFERENCE}
                          for layer in source_report["layers"]],
              "source_sha256": source_report["output_sha256"],
              "sample_spacing_m": 10.0, "max_sample_points": 2_000_000,
              "max_points_per_tile": 100_000, "max_triangle_edge_m": 500.0,
              "halo_m": 1000.0, "contour_sampling": "regular_arclength",
              "max_validation_spots": 100, "validation_seed": 0,
              "duplicate_elevation_tolerance_m": 0.01,
              "support_justification": "Explicit 500 m maximum triangle edge as in the existing central Seoul screening product; unsupported water/river spans remain NoData."}
    index_path = directory / "terrain_samples.sqlite"
    info_path = directory / "terrain_samples.json"
    config_path = directory / "terrain_config.json"
    last_progress = [started]

    def check() -> None:
        preflight(data_root)
        now = time.perf_counter()
        if now - last_progress[0] >= 30:
            print(json.dumps({"stage": "jamsil_terrain", "elapsed_s": now - started,
                              "artifact_bytes": tree_bytes(directory)}), flush=True)
            last_progress[0] = now

    if config_path.exists():
        saved = json.loads(config_path.read_text())
        saved_config = {key: value for key, value in saved["terrain"].items() if key != "tile_size"}
        if saved_config != config or saved["grid"] != grid:
            raise ValueError("Existing preparation configuration differs; preserve it and use a new directory.")
    if not (index_path.exists() and info_path.exists()):
        if index_path.exists():
            raise FileExistsError(f"Unverified existing sample index preserved: {index_path}")
        info = _build_sample_index(config, grid, index_path, check)
        _json(info_path, info)
    counts = {}
    with sqlite3.connect(index_path) as connection:
        for size in (256, 128, 64, 32, 16):
            maximum = 0
            for row in range(0, 1400, size):
                for column in range(0, 1400, size):
                    bounds = (BOUNDS[0] + column * 5 - 1000,
                              BOUNDS[0] + min(column + size, 1400) * 5 + 1000,
                              BOUNDS[3] - min(row + size, 1400) * 5 - 1000,
                              BOUNDS[3] - row * 5 + 1000)
                    count = connection.execute(
                        "SELECT count(*) FROM spatial WHERE maxx>=? AND minx<=? AND maxy>=? AND miny<=?", bounds
                    ).fetchone()[0]
                    maximum = max(maximum, count)
            counts[str(size)] = maximum
            if maximum <= config["max_points_per_tile"]:
                config["tile_size"] = size
                break
        else:
            raise RuntimeError("Even 16-pixel tile input halos exceed the explicit point cap")
    _json(config_path, {"terrain": config, "grid": grid, "max_indexed_points_by_tile_size": counts})
    print(json.dumps({"stage": "index_ready", "max_indexed_points_by_tile_size": counts,
                      "elapsed_s": time.perf_counter() - started}), flush=True)
    temporary = directory / "dtm.writing.tif"
    try:
        validation = prepare_terrain(config, grid, temporary, check)
        os.rename(temporary, output)
    except Exception as exc:
        _json(directory / "terrain_failure.json", {"status": "not_ready", "error": str(exc),
                                                   "type": type(exc).__name__})
        raise
    report = {"status": "terrain_only_not_engine_ready", "grid": grid,
              "output": str(output), "output_bytes": output.stat().st_size,
              "output_sha256": _hash(output), "source_archive_sha256": ARCHIVE_SHA256,
              "input_subset": source_report, "preflight": budget,
              "seconds": time.perf_counter() - started, "validation": validation,
              "coverage": terrain_coverage(output, TARGET_LON_LAT),
              "limitations": ["Unconstrained sampled-contour TIN is an approximation, not 5 m elevation accuracy.",
                              "Map contour and spot elevations retain their original metre attributes; no vertical conversion.",
                              "No buildings in this terrain-only product; not an engine manifest.",
                              "NoData outside convex hull or 500 m triangle-edge support remains unknown.",
                              "One hundred spatially distributed held-out spots are a diagnostic subset, not a population accuracy claim."],
              "reused": False}
    _json(report_path, report)
    preflight(data_root)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    arguments = parser.parse_args()
    print(json.dumps(prepare(arguments.data_root), ensure_ascii=False, indent=2))
