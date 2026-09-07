#!/usr/bin/env python3
"""Measure installed GDAL behavior and compare it with closed-column LOS.

Run from the repository after installation:
    python scripts/validate_backend.py --output reports/backend_validation.json

The report concerns deterministic synthetic data, not Seoul accuracy.  Hard
regressions cover only unambiguous cases; disagreement counts are reported
separately for models that intentionally use different surface conventions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import sys
import time

import numpy as np
from osgeo import gdal, osr

from seoul_visibility.backend import BACKEND_ID, viewshed
from seoul_visibility.reference import reference_los


def native(surface, transform, xy, source_height, eye_height=1.7, curvature=0.0, mode=gdal.GVM_Edge):
    """Call installed native API directly so the quantization probe is independent."""
    gdal.UseExceptions()
    source = gdal.GetDriverByName("MEM").Create("", surface.shape[1], surface.shape[0], 1, gdal.GDT_Float32)
    source.SetGeoTransform(transform)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(5186)
    source.SetProjection(srs.ExportToWkt())
    source.GetRasterBand(1).WriteArray(surface)
    radius = 2 * max(surface.shape) * abs(transform[1])
    result = gdal.ViewshedGenerate(source.GetRasterBand(1), "MEM", "", [], *xy,
        source_height, eye_height, 1, 0, 2, 3, curvature, mode, radius,
        heightMode=gdal.GVOT_NORMAL, options=[])
    assert result is not None
    array, output_transform = result.ReadAsArray(), result.GetGeoTransform()
    result = None
    source = None
    return array, output_transform


def xy(transform, row, col, col_offset=0.5, row_offset=0.5):
    return transform[0] + (col + col_offset) * transform[1], transform[3] + (row + row_offset) * transform[5]


def probe_quantization(seed):
    rng = np.random.default_rng(seed)
    surface = np.where(rng.random((31, 31)) < 0.18, rng.uniform(2, 35, (31, 31)), 0).astype(np.float32)
    surface[15, 15] = 7
    transform = (200000, 5, 0, 550000, 0, -5)
    offsets = [(0.01, 0.01), (0.25, 0.75), (0.5, 0.5), (0.75, 0.25), (0.99, 0.99)]
    arrays, records = [], []
    for col_offset, row_offset in offsets:
        point = xy(transform, 15, 15, col_offset, row_offset)
        array, output_transform = native(surface, transform, point, 20)
        arrays.append(array)
        records.append({"pixel_fraction": [col_offset, row_offset], "requested_xy": point,
                        "output_sha256": hashlib.sha256(array.tobytes()).hexdigest(),
                        "output_transform": output_transform})
    same = all(np.array_equal(arrays[0], array) for array in arrays[1:])
    assert same, "Installed GDAL source subcell behavior differs: review engine quantization"
    neighbor, _ = native(surface, transform, xy(transform, 15, 16, 0.01, 0.5), 20)
    mode_differences = {}
    for name, mode in (("GVM_Diagonal", gdal.GVM_Diagonal), ("GVM_Max", gdal.GVM_Max), ("GVM_Min", gdal.GVM_Min)):
        mode_array, _ = native(surface, transform, xy(transform, 15, 15), 20, mode=mode)
        mode_differences[name] = int(np.count_nonzero(mode_array != arrays[0]))
    return {
        "same_cell_offsets_identical": same,
        "records": records,
        "effective_xy_convention": xy(transform, 15, 15),
        "adjacent_cell_changed_pixels": int(np.count_nonzero(neighbor != arrays[0])),
        "source_elevation_m": 7,
        "observer_height_above_source_cell_m": 20,
        "resolved_absolute_source_elevation_m": 27,
        "mode_changed_pixels_vs_edge": mode_differences,
        "interpretation": "Measured same-cell invariance; effective location is reported at the containing cell center. No subpixel source precision is claimed.",
        "source_code_review": "GDAL 3.8.4 casts inverse-transformed XY to integer pixel indices, adds the source cell elevation to observerHeight, and uses index-distance geometry. GVM_Edge uses horizon propagation with edge interpolation; it is not full-column supercover LOS.",
    }


def compare_case(name, surface, transform, source_rc, absolute_z, eye_height=1.7, curvature=0.0, sample_size=None, seed=0, hard=False):
    source_xy = xy(transform, *source_rc)
    states, output_transform = viewshed(surface, transform, "EPSG:5186", source_xy,
        absolute_z - float(surface[source_rc]), eye_height, 2 * max(surface.shape) * abs(transform[1]), curvature)
    assert states.shape == surface.shape and tuple(output_transform) == tuple(transform)
    # Synthetic terrain is zero; all nonzero samples model obstacles.  Only
    # open-ground endpoints are compared, preserving the reversed mapping.
    eligible = np.argwhere(surface == 0)
    if sample_size is not None and len(eligible) > sample_size:
        eligible = eligible[np.random.default_rng(seed).choice(len(eligible), sample_size, replace=False)]
    false_visible = false_blocked = 0
    examples = []
    for row, col in eligible:
        endpoint = (*xy(transform, int(row), int(col)), eye_height)
        reference = reference_los(surface, transform, (*source_xy, absolute_z), endpoint, curvature)
        assert reference is not None
        visible = states[row, col] == 1
        if bool(visible) != reference:
            false_visible += bool(visible)
            false_blocked += not bool(visible)
            if len(examples) < 12:
                examples.append({"row": int(row), "col": int(col), "gdal_visible": bool(visible), "reference_visible": reference})
    if hard:
        assert false_visible == false_blocked == 0, f"Unambiguous case {name} disagrees"
    return {"case": name, "shape": list(surface.shape), "resolution_m": abs(transform[1]),
            "source_row_col": list(source_rc), "source_absolute_z_m": absolute_z,
            "eye_height_m": eye_height, "curvature_coefficient": curvature,
            "sampled_open_ground_cells": len(eligible), "false_visible": int(false_visible),
            "false_blocked": int(false_blocked), "hard_regression": hard, "examples": examples}


def run(seed=20260907):
    started = time.perf_counter()
    quantization = probe_quantization(seed)
    transform = (200000, 5, 0, 550000, 0, -5)
    flat = np.zeros((31, 31), np.float32)
    wall = flat.copy()
    wall[:, 15] = 30
    low_wall = flat.copy()
    low_wall[:, 15] = 1
    roof = flat.copy()
    roof[14:17, 5:9] = 10
    corner = np.zeros((5, 5), np.float32)
    corner[1, 2] = 4
    rng = np.random.default_rng(seed)
    random_surface = np.where(rng.random((41, 41)) < 0.12, rng.uniform(2, 35, (41, 41)), 0).astype(np.float32)
    random_surface[20, 20] = 0
    cases = [
        compare_case("flat_unobstructed", flat, transform, (15, 6), 1.7, hard=True),
        compare_case("tall_continuous_wall", wall, transform, (15, 6), 1.7, hard=True),
        compare_case("low_wall_no_eye_height_on_obstacles", low_wall, transform, (15, 6), 1.7, hard=True),
        compare_case("roof_target_on_surface", roof, transform, (15, 6), 10),
        compare_case("roof_target_above_surface", roof, transform, (15, 6), 14),
        compare_case("thin_corner_touch", corner, transform, (0, 0), 2, eye_height=2),
        compare_case("random_columns", random_surface, transform, (20, 20), 25, sample_size=500, seed=seed),
    ]
    large_flat = np.zeros((101, 101), np.float32)
    large_transform = (190000, 200, 0, 560000, 0, -200)
    for curvature in (0, 6 / 7, 1):
        cases.append(compare_case(f"curvature_{curvature:g}", large_flat, large_transform, (50, 50), 1.7,
            curvature=curvature, sample_size=150, seed=seed, hard=curvature == 0))
    return {
        "source_region": "deterministic synthetic rasters; no Seoul measurements",
        "backend": BACKEND_ID, "gdal_version": gdal.VersionInfo("RELEASE_NAME"),
        "python": sys.version, "platform": platform.platform(), "numpy": np.__version__, "seed": seed,
        "api_signature": gdal.ViewshedGenerate.__doc__,
        "selected_mode": "GVM_Edge", "earth_diameter_m": 12756274.0,
        "quantization_probe": quantization, "comparisons": cases,
        "total_false_visible": sum(case["false_visible"] for case in cases),
        "total_false_blocked": sum(case["false_blocked"] for case in cases),
        "comparison_interpretation": [
            "False-visible and false-blocked label disagreement relative to the closed-column reference, not measured real-world error.",
            "Columns retain full source and destination cell intervals, closed corner/edge touches, and endpoint contact tolerance 1e-6 m. GDAL propagates a horizon between raster samples.",
            "Roof source intervals, thin/corner obstacles, near-grazing rays, and propagated horizon interpolation can therefore disagree. No visibility-bound guarantee is asserted.",
            "The three simple flat/wall cases and flat-model long-distance case are hard regression checks. Other counts are diagnostic; review examples and model differences before relying on a shortlisted sightline.",
        ],
        "primary_sources": [
            "https://gdal.org/en/stable/programs/gdal_viewshed.html",
            "https://gdal.org/en/stable/api/gdal_alg.html",
            "https://raw.githubusercontent.com/OSGeo/gdal/v3.8.4/alg/viewshed.cpp",
        ],
        "elapsed_s": time.perf_counter() - started,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("reports/backend_validation.json"))
    parser.add_argument("--seed", type=int, default=20260907)
    args = parser.parse_args()
    report = run(args.seed)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "false_visible": report["total_false_visible"],
                      "false_blocked": report["total_false_blocked"], "elapsed_s": report["elapsed_s"]}))


if __name__ == "__main__":
    main()
