#!/usr/bin/env python3
"""Bounded, reproducible GDAL 3.8.4 Edge-mode radial dependency experiment."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import time

import numpy as np
from osgeo import gdal

from seoul_visibility.backend import viewshed


SOURCE = "https://github.com/OSGeo/gdal/blob/v3.8.4/alg/viewshed.cpp"


def scenario(radius: float, resolution: float, offset: tuple[int, int] = (0, 0),
             *, seed: int = 20260907, kind: str = "rough") -> tuple:
    half = math.ceil(radius / resolution) + 4
    row, col = half + offset[0], half + offset[1]
    size = 2 * half + 1 + 2 * max(map(abs, offset))
    gt = (192500., resolution, 0., 559000., 0., -resolution)
    target = (gt[0] + (col + .5) * resolution, gt[3] - (row + .5) * resolution)
    dr, dc = np.arange(size) - row, np.arange(size) - col
    distance = np.hypot(dr[:, None], dc[None, :]) * resolution
    rng = np.random.default_rng(seed)
    if kind == "flat":
        surface = np.zeros((size, size), dtype=np.float32)
    else:
        surface = rng.uniform(0, 24, (size, size)).astype(np.float32)
        surface[(dr[:, None] == -3) & (dc[None, :] >= -6)] = 45
        surface[(dc[None, :] == 4) & (dr[:, None] >= -9)] = 60
    surface[row, col] = 0
    return surface, gt, target, distance


def output_disk(array: np.ndarray, gt: tuple, target: tuple, radius: float) -> np.ndarray:
    xs = gt[0] + (np.arange(array.shape[1]) + .5) * gt[1] - target[0]
    ys = gt[3] + (np.arange(array.shape[0]) + .5) * gt[5] - target[1]
    return ys[:, None] ** 2 + xs[None, :] ** 2 <= radius ** 2


def perturbation_case(radius: float, resolution: float, curvature: float,
                      offset: tuple[int, int], kind: str) -> dict:
    surface, gt, target, distance = scenario(radius, resolution, offset, kind=kind)
    original, output_gt = viewshed(surface, gt, "EPSG:5186", target, 35., 1.7, radius, curvature)
    inside = output_disk(original, output_gt, target, radius)
    rng = np.random.default_rng(16439)
    changes = []
    for padding in ("minus_billion", "plus_billion", "float32_nodata", "random_extreme"):
        modified = surface.copy()
        outside = distance > radius
        if padding == "random_extreme":
            modified[outside] = rng.uniform(-1e12, 1e12, int(outside.sum())).astype(np.float32)
        else:
            modified[outside] = {"minus_billion": -1e9, "plus_billion": 1e9,
                                 "float32_nodata": np.finfo(np.float32).min}[padding]
        result, changed_gt = viewshed(modified, gt, "EPSG:5186", target, 35., 1.7, radius, curvature)
        assert output_gt == changed_gt and result.shape == original.shape
        differences = int(np.count_nonzero(result[inside] != original[inside]))
        changes.append({"outside_padding": padding, "inside_differences": differences})
        assert differences == 0, (radius, resolution, curvature, offset, kind, padding, differences)
    return {"radius_m": radius, "resolution_m": resolution, "curvature": curvature,
            "target_offset_cells": offset, "terrain": kind, "input_shape": surface.shape,
            "inside_pixels": int(inside.sum()), "outside_pixels_varied": int((distance > radius).sum()),
            "comparisons": changes}


def boundary_blocker_case() -> dict:
    """Outside ring cannot change boundary endpoints; immediate inner wall must."""
    radius, resolution = 100., 5.
    surface, gt, target, distance = scenario(radius, resolution, kind="flat")
    baseline, output_gt = viewshed(surface, gt, "EPSG:5186", target, 2., 1.7, radius, 0.)
    inside = output_disk(baseline, output_gt, target, radius)
    ring = (distance > radius) & (distance <= radius + resolution)
    outside_wall = surface.copy()
    outside_wall[ring] = 1e9
    changed, _ = viewshed(outside_wall, gt, "EPSG:5186", target, 2., 1.7, radius, 0.)
    outside_differences = int(np.count_nonzero(changed[inside] != baseline[inside]))
    assert outside_differences == 0
    source_row = round((gt[3] - target[1]) / resolution - .5)
    source_col = round((target[0] - gt[0]) / resolution - .5)
    # All four cardinal walls are one cell before a receiver exactly on R.
    inner_wall = outside_wall.copy()
    steps = int(radius / resolution)
    receiver_states = []
    for dr, dc in ((0, 1), (0, -1), (1, 0), (-1, 0)):
        inner_wall[source_row + dr * (steps - 1), source_col + dc * (steps - 1)] = 1000.
    blocked, blocked_gt = viewshed(inner_wall, gt, "EPSG:5186", target, 2., 1.7, radius, 0.)
    for dr, dc in ((0, 1), (0, -1), (1, 0), (-1, 0)):
        x, y = target[0] + dc * radius, target[1] - dr * radius
        col = round((x - blocked_gt[0]) / resolution - .5)
        row = round((blocked_gt[3] - y) / resolution - .5)
        receiver_states.append({"offset_cells": [dr * steps, dc * steps],
                                "baseline": int(baseline[row, col]), "with_inner_wall": int(blocked[row, col])})
        assert baseline[row, col] == 1 and blocked[row, col] == 0
    # Pythagorean radius cell in every quadrant: predecessor (11,15)
    # is just inside the 20-cell circle; (13,16) is just outside.
    diagonal_results = []
    for sr, sc in ((1, 1), (1, -1), (-1, 1), (-1, -1)):
        diagonal = surface.copy()
        diagonal[source_row + sr * 16, source_col + sc * 13] = 1e9
        unaffected, diag_gt = viewshed(diagonal, gt, "EPSG:5186", target, 2., 1.7, radius, 0.)
        diagonal[source_row + sr * 15, source_col + sc * 11] = 1000.
        shadow, _ = viewshed(diagonal, gt, "EPSG:5186", target, 2., 1.7, radius, 0.)
        x, y = target[0] + sc * 12 * resolution, target[1] - sr * 16 * resolution
        c = round((x - diag_gt[0]) / resolution - .5)
        r = round((diag_gt[3] - y) / resolution - .5)
        assert unaffected[r, c] == 1 and shadow[r, c] == 0
        diagonal_results.append({"receiver_offset_cells": [sr * 16, sc * 12],
                                 "outside_wall_state": int(unaffected[r, c]),
                                 "inside_predecessor_wall_state": int(shadow[r, c])})
    return {"radius_m": radius, "outside_ring_cells": int(ring.sum()),
            "outside_ring_inside_differences": outside_differences,
            "cardinal_boundary_receivers": receiver_states, "diagonal_boundary_receivers": diagonal_results}


def run_probe() -> dict:
    version = gdal.VersionInfo("RELEASE_NAME")
    if version != "3.8.4":
        raise RuntimeError(f"Source proof is specific to GDAL 3.8.4; installed {version}. Re-audit its source first")
    started = time.perf_counter()
    cases = []
    for radius, resolution in ((1., 5.), (5., 5.), (25., 5.), (99.9, 5.), (100., 5.),
                               (103.75, 5.), (40., 2.), (5000., 5.)):
        for curvature in (0., 6 / 7, 1.):
            # Keep 5km experiment bounded; other cases exercise all variants.
            variants = [((0, 0), "rough")] if radius == 5000 else [((0, 0), "flat"), ((2, -1), "rough")]
            for offset, kind in variants:
                cases.append(perturbation_case(radius, resolution, curvature, offset, kind))
    boundary = boundary_blocker_case()
    return {"backend": f"GDAL/{version}/GVM_Edge/GVOT_NORMAL", "source": SOURCE,
            "source_lines": {"radial_filter": [59, 78], "source_quantization": [253, 258],
                             "axis_scan": [405, 493], "quadrant_predecessors": [544, 790]},
            "proof_scope": "square north-up grid; effective source cell center; finite positive maxDistance; Edge normal mode",
            "finding": "Every Edge predecessor decreases one or both absolute source-relative pixel offsets, so its Euclidean center distance is smaller. Range checks precede horizon propagation. By induction, in-radius results depend only on in-radius cells. Full scanline reads do not create horizon dependencies on out-of-range corners.",
            "limitations": ["Version-specific native raster-model dependency statement, not a guarantee of physical visibility accuracy.",
                            "Input NoData inside the dependency disk remains unsafe and must be rejected before GDAL.",
                            "Tiny-radius adjacent cells may be initialized visible outside the radius; callers must apply their radius mask.",
                            "Rotated/sheared grids and other versions/modes are outside this proof.",
                            "A conservative one-cell validation halo is acceptable but is not required by this predecessor proof."],
            "seed": 20260907, "case_count": len(cases), "perturbation_comparisons": len(cases) * 4,
            "inside_differences_total": sum(c["inside_differences"] for case in cases for c in case["comparisons"]),
            "cases": cases, "boundary_obstacle_tests": boundary, "elapsed_s": time.perf_counter() - started}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("reports/gdal-radial-dependencies"))
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = args.output_dir / "probe.json"
    if output.exists():
        raise FileExistsError(f"Preserving existing report: {output}; use a new output directory")
    report = run_probe()
    output.write_text(json.dumps(report, indent=2) + "\n")
    text = (f"GDAL {gdal.VersionInfo('RELEASE_NAME')} radial dependency experiment\n\n"
            f"{report['case_count']} scenarios / {report['perturbation_comparisons']} extreme outside-circle comparisons; "
            f"inside differences: {report['inside_differences_total']}. Four cardinal and four diagonal boundary receivers "
            "retained outside-wall visibility and became blocked with an immediate inside predecessor wall. "
            "Includes a 5km radius on a 5m grid, 2m data, fractional radii, shifted source cells and curvature 0, 6/7, 1.\n\n"
            f"{report['finding']} [Official v3.8.4 source]({SOURCE}), lines 59–78, 93–110, 405–493, 544–790. "
            "The engine supports square north-up grids and snaps targets to cell centers. This conclusion applies only "
            "to the inspected version and mode. Unknown data in the dependency disk still requires rejection. "
            "Changing irrelevant corner values for the native input does not fill or repair the source dataset.\n\n"
            "These tests establish native dependencies, not exact column LOS or real-data accuracy.\n")
    (args.output_dir / "probe.md").write_text(text)
    print(json.dumps({k: report[k] for k in ("case_count", "perturbation_comparisons", "inside_differences_total", "elapsed_s")}))


if __name__ == "__main__":
    main()
