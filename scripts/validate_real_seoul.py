#!/usr/bin/env python3
"""Validate a prepared local point viewshed against the closed-column reference.

Example:
  .venv/bin/python scripts/validate_real_seoul.py --manifest data/seoul/pilot/manifest.json \
      --output-dir reports/seoul-pilot-validation --radius 1000 --png

The default target is a hypothetical point 100 m above bare earth near
Gwanghwamun, not a surveyed landmark height. Native/reference disagreements
measure differences between raster models, never real-world Seoul accuracy.
"""
from __future__ import annotations

import time

PROCESS_ENTERED = time.perf_counter()

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import sys

import numpy as np
from osgeo import gdal
from pyproj import Transformer

from seoul_visibility import TargetPoint, VisibilityEngine
from seoul_visibility.reference import reference_los, traversed_cells
from seoul_visibility.resources import preflight
from seoul_visibility.types import State


def state_counts(values: np.ndarray) -> dict[str, int]:
    return {state.name.lower(): int(np.count_nonzero(values == state)) for state in State}


def read_window(manifest_path: Path, product: dict, name: str, window: list[int]) -> np.ndarray:
    """Read independently opened GDAL handles, retaining every occluding cell."""
    dataset = gdal.OpenEx(str((manifest_path.parent / product[name]).resolve()),
                         gdal.OF_RASTER | gdal.OF_READONLY)
    if dataset is None:
        raise RuntimeError(f"Cannot open prepared {name}")
    band = dataset.GetRasterBand(1)
    array = band.ReadAsArray(*window)
    if array is None:
        raise RuntimeError(f"Cannot read {name} computational window")
    nodata = band.GetNoDataValue()
    if nodata is not None:
        if name in {"dtm", "surface"}:
            array[array == nodata] = np.nan
        else:
            array[array == nodata] = 255 if name == "occupancy" else 0
    dataset = None
    return array


def stratified_sample(states: np.ndarray, count: int, seed: int) -> np.ndarray:
    """Balanced native classes; draw the remainder from available eligible cells."""
    rng = np.random.default_rng(seed)
    selected = []
    for state, requested in ((State.VISIBLE, (count + 1) // 2), (State.BLOCKED, count // 2)):
        candidates = np.flatnonzero(states.ravel() == state)
        selected.extend(rng.choice(candidates, min(requested, len(candidates)), replace=False).tolist())
    remaining = min(count, int(np.count_nonzero(states < State.EXCLUDED))) - len(selected)
    if remaining:
        candidates = np.setdiff1d(np.flatnonzero(states.ravel() < State.EXCLUDED), selected)
        selected.extend(rng.choice(candidates, remaining, replace=False).tolist())
    rng.shuffle(selected)
    return np.asarray(selected, dtype=np.int64)


def coverage_region(shape: tuple[int, int], transform: tuple, effective: dict,
                    radius: float, resolution: float, coverage: dict, backend: dict) -> np.ndarray:
    """Independently verify the declared, version-specific native dependencies.

    Arrays retain original NoData outside this region. Reference LOS therefore
    still returns unknown if a sampled ray unexpectedly touches such a cell.
    """
    strategy = coverage.get("strategy", "full_rectangle_v1")
    if strategy == "full_rectangle_v1":
        required = np.ones(shape, dtype=bool)
    elif strategy == "radius_plus_one_cell_v1":
        if backend["backend_version"] != "3.8.4" or backend["backend_mode"] != "GVM_Edge/GVOT_NORMAL":
            raise AssertionError("Radial coverage was used with an unaudited GDAL implementation")
        if transform[2] != 0 or transform[4] != 0 or transform[1] != resolution or transform[5] != -resolution:
            raise AssertionError("Radial dependency proof requires square north-up pixels")
        if coverage["required_radius_m"] != radius + resolution:
            raise AssertionError("Declared coverage radius omits the conservative one-cell halo")
        xs = transform[0] + (np.arange(shape[1]) + .5)*resolution - effective["x"]
        ys = transform[3] - (np.arange(shape[0]) + .5)*resolution - effective["y"]
        required = ys[:, None]**2 + xs[None, :]**2 <= (radius+resolution)**2 + 1e-9
    else:
        raise AssertionError(f"Unrecognized coverage strategy: {strategy}")
    if "required_cells" in coverage and np.count_nonzero(required) != coverage["required_cells"]:
        raise AssertionError("Required region does not match engine coverage count")
    return required


def column_diagnostic(surface: np.ndarray, dtm: np.ndarray, occupancy: np.ndarray,
                      transform: tuple, start: tuple[float, float, float],
                      end: tuple[float, float, float], curvature: float, diameter: float,
                      global_offset: tuple[int, int]) -> dict:
    """Explain full traversed-column clearance without changing either model.

    This mirrors the reference's explicit clearance polynomial, using its
    independent traversal helper so narrow intervals and corner contacts remain
    present. Only the first blocker and lowest-clearance column are retained.
    """
    distance = math.hypot(end[0]-start[0], end[1]-start[1])
    q = curvature*distance**2/diameter
    dz = end[2]-start[2]
    stationary = (q-dz)/(2*q) if q else None
    first = worst = None
    blocked = corner_blocked = traversed = 0
    for row, col, enter, leave in traversed_cells(surface.shape, transform, start[:2], end[:2]):
        traversed += 1
        ts = [enter, leave]
        if stationary is not None and enter < stationary < leave:
            ts.append(stationary)
        values = [start[2]+t*dz-float(surface[row, col])-q*t*(1-t) for t in ts]
        index = int(np.argmin(values))
        clearance, at = values[index], ts[index]
        record = {"global_row": row+global_offset[1], "global_col": col+global_offset[0],
                  "local_row": row, "local_col": col,
                  "center_x": transform[0]+(col+.5)*transform[1],
                  "center_y": transform[3]+(row+.5)*transform[5],
                  "dtm_m": float(dtm[row, col]), "surface_m": float(surface[row, col]),
                  "occupancy": int(occupancy[row, col]),
                  "obstruction_kind": "building column" if occupancy[row, col] else "terrain column",
                  "enter_t": float(enter), "exit_t": float(leave), "minimum_at_t": float(at),
                  "minimum_clearance_m": float(clearance),
                  "interval_path_length_m": float(distance*(leave-enter)),
                  "zero_length_corner_or_edge_contact": abs(leave-enter) <= 1e-12,
                  "near_grazing_within_0_25m": abs(clearance) <= .25}
        if worst is None or clearance < worst["minimum_clearance_m"]:
            worst = record
        if clearance < -1e-6:
            blocked += 1
            corner_blocked += record["zero_length_corner_or_edge_contact"]
            if first is None:
                first = record
    if first:
        contact = "zero-length corner/edge contact" if first["zero_length_corner_or_edge_contact"] else "nonzero traversed interval"
        explanation = (f"The closed-column reference intersects a {first['obstruction_kind']} over a {contact}; "
                       f"its first blocking interval reaches clearance {first['minimum_clearance_m']:.6f} m. "
                       "The native horizon method returned a different state; these source-cell values explain the "
                       "reference decision but do not establish which model matches a surveyed real sightline.")
    else:
        explanation = ("Every closed-column interval meets the reference contact tolerance; the native horizon "
                       "method returned a different state. No reference blocker was fabricated to force agreement.")
    return {"ray_distance_m": distance, "traversed_column_count": traversed,
            "blocking_column_count": blocked, "zero_length_blocking_contacts": corner_blocked,
            "first_reference_blocker": first, "lowest_clearance_column": worst,
            "contact_tolerance_m": 1e-6, "clearance_formula": "z0 + t*(z1-z0) - S - k*d^2*t*(1-t)/earth_diameter",
            "explanation": explanation}


def optional_png(result, samples: list[dict], path: Path) -> dict:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.colors import BoundaryNorm, ListedColormap
        from matplotlib.patches import Patch
    except ImportError as exc:
        return {"status": "unavailable", "reason": str(exc)}
    began = time.perf_counter()
    colors = ["#3b4654", "#49bb83", "#ededed", "#e3b54b"]
    cmap = ListedColormap(colors)
    figure, ax = plt.subplots(figsize=(10, 10))
    figure.subplots_adjust(left=.12, right=.985, bottom=.12, top=.90)
    left, bottom, right, top = result.bounds
    ax.imshow(result.states, extent=(left, right, bottom, top), origin="upper",
              cmap=cmap, norm=BoundaryNorm([-.5, .5, 1.5, 2.5, 3.5], 4), interpolation="nearest")
    target = result.metadata["target"]["effective"]
    ax.scatter([target["x"]], [target["y"]], marker="*", s=190, color="#d94b35",
               edgecolors="white", linewidths=.8, zorder=3)
    disagreed = [sample for sample in samples if sample["dense_state"] != sample["reference_state"]]
    if disagreed:
        ax.scatter([sample["x"] for sample in disagreed], [sample["y"] for sample in disagreed],
                   s=19, facecolors="none", edgecolors="#cf2083", linewidths=.9, label="Sample model disagreement")
    handles = [Patch(facecolor=color, label=state.name.title()) for state, color in zip(State, colors)]
    if disagreed:
        handles.append(Patch(facecolor="none", edgecolor="#cf2083", label="Sample model disagreement"))
    ax.legend(handles=handles, loc="lower left", framealpha=.95)
    requested = result.metadata["target"]["requested"]
    ax.set_title(f"Point visibility screening | hypothetical target {requested['height_m']:g} m {requested['height_reference'].upper()}\n"
                 f"Radius {result.metadata['radius_m']:g} m | {result.resolution_m:g} m grid | "
                 f"{result.metadata['quality']['classification']}")
    ax.set_xlabel(f"Easting (m), {result.crs}")
    ax.set_ylabel("Northing (m)")
    ax.ticklabel_format(axis="both", style="plain", useOffset=False)
    figure.text(.5, .025, "Only the specified point. Public access unverified. No field accuracy validation.\n"
                "Boundary: © OpenStreetMap contributors (ODbL), when supplied by the prepared data.",
                ha="center", va="bottom", fontsize=8)
    figure.savefig(path, dpi=150, facecolor="white")
    plt.close(figure)
    return {"status": "written", "path": str(path), "bytes": path.stat().st_size,
            "seconds": time.perf_counter() - began, "basemap": "none; local state raster only"}


def validate(args: argparse.Namespace) -> dict:
    gdal.UseExceptions()
    manifest_path = args.manifest.resolve()
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("source_kind") == "synthetic" and not args.allow_synthetic:
        raise ValueError("Synthetic manifest supplied to real-data validator; use --allow-synthetic only for script smoke testing")
    products = [product for product in manifest["products"].values()
                if float(product["resolution_m"]) == args.resolution]
    if len(products) != 1:
        raise ValueError("Requested independently prepared resolution is unavailable")
    product = products[0]
    target = TargetPoint(args.lon, args.lat, args.height, "agl")
    settings = {"target": target, "radius_m": args.radius, "eye_height_m": args.eye_height,
                "resolution_m": args.resolution, "curvature_coefficient": args.curvature}
    first_open = time.perf_counter()
    with VisibilityEngine.from_manifest(manifest_path) as engine:
        opening_seconds = time.perf_counter() - first_open
        first = engine.visible_from_target(**settings, use_cache=False)
        repeat = engine.visible_from_target(**settings, use_cache=False)
        cached_miss = engine.visible_from_target(**settings, use_cache=True)
        cached_hit = engine.visible_from_target(**settings, use_cache=True)
        equality = {"repeated_uncached_states_equal": np.array_equal(first.states, repeat.states),
                    "cache_population_states_equal": np.array_equal(first.states, cached_miss.states),
                    "cache_hit_states_equal": np.array_equal(first.states, cached_hit.states),
                    "all_grid_transforms_equal": all(first.transform == x.transform
                                                     for x in (repeat, cached_miss, cached_hit)),
                    "cache_hit_reported": cached_hit.metadata["cache_status"] == "memory_hit"}
        if not all(equality.values()):
            raise AssertionError(f"Dense repeat/cache validation failed: {equality}")
        if first.states.dtype != np.uint8 or not np.isin(first.states, [0, 1, 2, 3]).all():
            raise AssertionError("Dense raster violates compact explicit-state contract")
        sampled = stratified_sample(first.states, args.samples, args.seed)
        if not len(sampled):
            raise AssertionError("No eligible observer cells exist in this result")
        rows, cols = np.divmod(sampled, first.states.shape[1])
        xs = first.transform[0] + (cols + .5) * first.transform[1]
        ys = first.transform[3] + (rows + .5) * first.transform[5]
        to_lonlat = Transformer.from_crs(first.crs, "EPSG:4326", always_xy=True)
        to_xy = Transformer.from_crs("EPSG:4326", first.crs, always_xy=True)
        lons, lats = to_lonlat.transform(xs, ys)
        round_x, round_y = to_xy.transform(lons, lats)
        roundtrip_error = float(np.max(np.hypot(round_x-xs, round_y-ys)))
        if roundtrip_error > 1e-5:
            raise AssertionError(f"Cell-center WGS84 roundtrip drift: {roundtrip_error} m")
        sparse_started = time.perf_counter()
        sparse = engine.check_observers(target, np.column_stack([lons, lats]),
                                        eye_height_m=args.eye_height, resolution_m=args.resolution,
                                        curvature_coefficient=args.curvature)
        sparse_seconds = time.perf_counter() - sparse_started

        # Independently read the already-prepared computational window once.
        read_started = time.perf_counter()
        window = first.metadata["computational_window"]
        dtm = read_window(manifest_path, product, "dtm", window)
        surface = read_window(manifest_path, product, "surface", window)
        quality = read_window(manifest_path, product, "quality", window)
        occupancy = read_window(manifest_path, product, "occupancy", window)
        bad = (~np.isfinite(dtm) | ~np.isfinite(surface) | ((quality & 3) != 3)
               | ((quality & 248) != 0) | (occupancy > 1))
        gt = product["transform"]
        local = (gt[0]+window[0]*args.resolution, args.resolution, 0,
                 gt[3]-window[1]*args.resolution, 0, -args.resolution)
        required = coverage_region(surface.shape, local, first.metadata["target"]["effective"],
                                   args.radius, args.resolution, first.metadata.get("coverage", {}), first.metadata)
        if np.any(required & bad):
            raise AssertionError("Native dense query accepted incomplete required computational coverage")
        if np.any(required & (occupancy == 0) & (np.abs(surface-dtm) > 1e-6)) or np.any(required & (surface < dtm-1e-6)):
            raise AssertionError("Prepared data violates open-ground/obstruction surface contract")
        unused_unknown = int(np.count_nonzero(~required & bad))
        if unused_unknown != first.metadata.get("coverage", {}).get("unknown_unused_padding_cells", 0):
            raise AssertionError("Engine unknown corner count differs from independent original-raster reads")
        out_col = round((first.transform[0]-local[0])/args.resolution)
        out_row = round((local[3]-first.transform[3])/args.resolution)
        h, w = first.states.shape
        out_slice = np.s_[out_row:out_row+h, out_col:out_col+w]
        eligible = first.states < State.EXCLUDED
        occupied_eligible = int(np.count_nonzero(eligible & (occupancy[out_slice] != 0)))
        boundary_eligible = 0
        if product.get("output_mask"):
            boundary = read_window(manifest_path, product, "output_mask", window)[out_slice]
            boundary_eligible = int(np.count_nonzero(eligible & (boundary == 0)))
        eff = first.metadata["target"]["effective"]
        dx = first.transform[0] + (np.arange(w)+.5)*args.resolution-eff["x"]
        dy = first.transform[3] - (np.arange(h)+.5)*args.resolution-eff["y"]
        outside_radius = int(np.count_nonzero(eligible & ((dy[:, None]**2+dx[None, :]**2) > args.radius**2+1e-9)))
        if occupied_eligible or boundary_eligible or outside_radius:
            raise AssertionError("Dense output contains ineligible observer cells")
        read_seconds = time.perf_counter() - read_started
        absolute = first.metadata["target"]["absolute_elevation_m"]
        endpoint_target = (eff["x"], eff["y"], absolute)
        reference_started = time.perf_counter()
        samples = []
        reciprocity_checks = min(12, len(sampled))
        reciprocity_failures = 0
        for index, (row, col, x, y, lon, lat) in enumerate(zip(rows, cols, xs, ys, lons, lats)):
            rr = int(row)+out_row
            cc = int(col)+out_col
            endpoint_observer = (float(x), float(y), float(dtm[rr, cc])+args.eye_height)
            visible = reference_los(surface, local, endpoint_target, endpoint_observer,
                                    args.curvature, engine.earth_diameter_m)
            reference_state = State.UNKNOWN if visible is None else State.VISIBLE if visible else State.BLOCKED
            if index < reciprocity_checks:
                reciprocal = reference_los(surface, local, endpoint_observer, endpoint_target,
                                           args.curvature, engine.earth_diameter_m)
                reciprocity_failures += reciprocal != visible
            samples.append({"row": int(row), "col": int(col), "x": float(x), "y": float(y),
                            "lon": float(lon), "lat": float(lat),
                            "observer_absolute_elevation_m": endpoint_observer[2],
                            "dense_state": int(first.states[row, col]),
                            "sparse_state": int(sparse.states[index]),
                            "sparse_reason": sparse.reasons[index], "reference_state": int(reference_state)})
            if samples[-1]["dense_state"] != samples[-1]["reference_state"]:
                samples[-1]["column_diagnostic"] = column_diagnostic(surface, dtm, occupancy, local,
                    endpoint_target, endpoint_observer, args.curvature, engine.earth_diameter_m,
                    (window[0], window[1]))
        reference_seconds = time.perf_counter() - reference_started
        false_visible = sum(x["dense_state"] == State.VISIBLE and x["reference_state"] == State.BLOCKED for x in samples)
        false_blocked = sum(x["dense_state"] == State.BLOCKED and x["reference_state"] == State.VISIBLE for x in samples)
        sparse_disagreements = sum(x["sparse_state"] != x["reference_state"] for x in samples)
        if reciprocity_failures or sparse_disagreements:
            raise AssertionError(f"Reference contract regression: reciprocity={reciprocity_failures}, "
                                 f"public sparse API vs direct cell-center reference={sparse_disagreements}")
        report = {"status": "passed_with_native_reference_disagreements" if false_visible or false_blocked else "passed",
                  "generated_utc": datetime.now(timezone.utc).isoformat(), "manifest": str(manifest_path),
                  "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                  "source_kind": manifest.get("source_kind", "local"), "source_data_version": manifest["data_version"],
                  "target_note": "Hypothetical explicit point height above DTM; not a surveyed landmark height",
                  "target": first.metadata["target"], "resolution_m": first.resolution_m,
                  "radius_m": args.radius, "eye_height_m": args.eye_height, "curvature_coefficient": args.curvature,
                  "crs": first.crs, "transform": first.transform, "bounds": first.bounds,
                  "shape": list(first.states.shape), "dense_states": state_counts(first.states),
                  "dense_output_sha256": hashlib.sha256(first.states.tobytes()).hexdigest(),
                  "dense_cache_checks": equality, "quality": first.metadata["quality"],
                  "coverage": first.metadata.get("coverage", {"strategy": "full_rectangle_v1"}),
                  "independent_coverage_checks": {"required_cells": int(np.count_nonzero(required)),
                      "unknown_required_cells": int(np.count_nonzero(required & bad)),
                      "unknown_unrequired_cells": unused_unknown,
                      "reference_input": "Original prepared surface values; unknown corners remain NaN, never padded"},
                  "output_mask_checks": {"building_cells_eligible": occupied_eligible,
                                         "outside_boundary_eligible": boundary_eligible, "outside_radius_eligible": outside_radius},
                  "sampling": {"seed": args.seed, "requested": args.samples, "actual": len(samples),
                               "method": "stratified native visible/blocked; random order retained; not prevalence-weighted",
                               "dense_class_counts": dict(Counter(State(x["dense_state"]).name.lower() for x in samples)),
                               "observer_convention": "exact output cell centers projected to WGS84 for public sparse API",
                               "max_wgs84_roundtrip_error_m": roundtrip_error},
                  "comparison": {"false_visible_relative_to_column_reference": int(false_visible),
                                 "false_blocked_relative_to_column_reference": int(false_blocked),
                                 "unknown_reference": sum(x["reference_state"] == State.UNKNOWN for x in samples),
                                 "public_sparse_vs_direct_reference_disagreements": int(sparse_disagreements),
                                 "reciprocity_checked": reciprocity_checks, "reciprocity_failures": reciprocity_failures,
                                 "interpretation": "Model disagreement, not measured Seoul error; GDAL interpolates horizons, reference intersects closed full-cell columns including endpoint intervals/corner touches."},
                  "timings": {"imports_and_argument_setup_s": first_open-PROCESS_ENTERED,
                              "initial_open_s": opening_seconds, "uncached_first": first.timings,
                              "uncached_repeat": repeat.timings, "cache_population_miss": cached_miss.timings,
                              "cache_hit": cached_hit.timings, "public_sparse_s": sparse_seconds,
                              "independent_reference_read_s": read_seconds, "direct_reference_s": reference_seconds},
                  "backend": {key: first.metadata[key] for key in ("backend", "backend_version", "backend_mode", "earth_diameter_m")},
                  "environment": {"python": sys.version, "platform": platform.platform(), "numpy": np.__version__},
                  "source_provenance": first.metadata.get("source_provenance", {}),
                  "limitations": first.metadata["limitations"] + [
                      "No field or surveyed observer-target LOS ground truth was supplied; this is software/model validation only.",
                      "Balanced class sampling does not estimate a citywide false-visible or false-blocked rate.",
                      "Repeated runs may use warm filesystem/GDAL caches; no cold-disk claim or OS-cache flush.",
                      "Cache-hit latency is reported separately from newly computed dense queries."],
                  "samples": samples}
        if args.png:
            report["png"] = optional_png(first, samples, args.artifact_dir / "visibility.png")
        if args.export_geotiff:
            began = time.perf_counter()
            # Include all project bytes if exporting under reports rather than data.
            project_root = Path(__file__).resolve().parents[1]
            destination = first.export_geotiff(args.artifact_dir / "visibility.tif", project_data_root=project_root)
            report["geotiff_export"] = {"path": str(destination), "bytes": destination.stat().st_size,
                                        "seconds": time.perf_counter()-began}
        report["total_script_s"] = time.perf_counter()-PROCESS_ENTERED
    return report


def diagnose_existing(report_path: Path, output_dir: Path) -> dict:
    """Add a separate bounded diagnosis while preserving the original report."""
    prior = json.loads(report_path.read_text())
    manifest_path = Path(prior["manifest"])
    if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != prior["manifest_sha256"]:
        raise ValueError("Original validation manifest changed; rerun validation against its new data version")
    manifest = json.loads(manifest_path.read_text())
    resolution = prior["resolution_m"]
    products = [p for p in manifest["products"].values() if float(p["resolution_m"]) == resolution]
    if len(products) != 1 or manifest["data_version"] != prior["source_data_version"]:
        raise ValueError("Prepared product no longer matches the validation report")
    product = products[0]
    eff = prior["target"]["effective"]
    half = math.ceil(prior["radius_m"]/resolution)+1
    window = [eff["col"]-half, eff["row"]-half, 2*half+1, 2*half+1]
    dtm = read_window(manifest_path, product, "dtm", window)
    surface = read_window(manifest_path, product, "surface", window)
    occupancy = read_window(manifest_path, product, "occupancy", window)
    quality = read_window(manifest_path, product, "quality", window)
    gt = product["transform"]
    local = (gt[0]+window[0]*resolution, resolution, 0,
             gt[3]-window[1]*resolution, 0, -resolution)
    bad = (~np.isfinite(surface) | ~np.isfinite(dtm) | ((quality & 3) != 3)
           | ((quality & 248) != 0) | (occupancy > 1))
    required = coverage_region(surface.shape, local, eff, prior["radius_m"], resolution,
                               prior.get("coverage", {}), prior["backend"])
    if np.any(required & bad):
        raise ValueError("Previously validated required computation region is no longer complete")
    start = (eff["x"], eff["y"], prior["target"]["absolute_elevation_m"])
    disagreements = [sample for sample in prior["samples"] if sample["dense_state"] != sample["reference_state"]]
    if len(disagreements) > 200:
        raise ValueError("Diagnostic is restricted to at most 200 previously sampled disagreements")
    diagnosed = []
    started = time.perf_counter()
    for sample in disagreements:
        end = (sample["x"], sample["y"], sample["observer_absolute_elevation_m"])
        reference = reference_los(surface, local, start, end, prior["curvature_coefficient"], prior["backend"]["earth_diameter_m"])
        recomputed = State.UNKNOWN if reference is None else State.VISIBLE if reference else State.BLOCKED
        if int(recomputed) != sample["reference_state"]:
            raise AssertionError("Stored reference decision changed; do not diagnose different inputs as the original run")
        diagnosed.append({**sample, "column_diagnostic": column_diagnostic(surface, dtm, occupancy, local,
            start, end, prior["curvature_coefficient"], prior["backend"]["earth_diameter_m"], (window[0], window[1]))})
    result = {"status": "diagnosed_model_disagreements", "original_report": str(report_path.resolve()),
              "original_report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
              "manifest": str(manifest_path), "source_data_version": manifest["data_version"],
              "target": prior["target"], "radius_m": prior["radius_m"], "resolution_m": resolution,
              "curvature_coefficient": prior["curvature_coefficient"], "compared_samples": prior["sampling"]["actual"],
              "original_comparison": prior["comparison"], "diagnosed_count": len(diagnosed),
              "diagnostics": diagnosed, "diagnostic_seconds": time.perf_counter()-started,
              "interpretation": "Concrete prepared-column obstruction evidence, not field validation or a resolution of the native/reference approximation difference."}
    (output_dir / "disagreement_diagnostics.json").write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf8")
    lines = ["# Native/reference disagreement diagnostics", "",
             f"Diagnosed **{len(diagnosed)}** disagreements from **{prior['sampling']['actual']}** sampled eligible cells. "
             "The original validation report and output rasters were preserved.", "",
             "| Observer cell | Native / reference | First blocking column | Interval length | Minimum clearance |",
             "| --- | --- | --- | ---: | ---: |"]
    for sample in diagnosed:
        first = sample["column_diagnostic"]["first_reference_blocker"]
        native = State(sample["dense_state"]).name.lower()
        reference = State(sample["reference_state"]).name.lower()
        blocker = f"{first['obstruction_kind']} ({first['global_row']}, {first['global_col']})" if first else "none"
        length = f"{first['interval_path_length_m']:.6f} m" if first else "—"
        clearance = f"{first['minimum_clearance_m']:.6f} m" if first else "—"
        lines.append(f"| ({sample['row']}, {sample['col']}) | {native} / {reference} | {blocker} | {length} | {clearance} |")
    lines.extend(["", "These are full traversed pixel-column intervals with terrain, building tops, endpoint offsets, "
                  "and the same symmetric curvature setting retained. Negative clearance below -0.000001 m blocks "
                  "the reference ray. JSON records contain original DTM/surface values, occupancy, global cell location, "
                  "entry/exit ray fractions, first blocker, and worst clearance. A zero-length corner/edge contact is "
                  "identified separately from a ray passing through a cell interior.", "",
                  "GDAL propagates an interpolated horizon and the reference tests closed constant-height columns. "
                  "The recorded obstacles substantiate the reference decisions; they do not establish which model "
                  "matches a surveyed physical line of sight. Estimated building heights and the configured maximum "
                  "DTM base remain approximate source assumptions.", ""])
    (output_dir / "disagreement_diagnostics.md").write_text("\n".join(lines), encoding="utf8")
    return result


def write_markdown(report: dict, path: Path) -> None:
    comp = report["comparison"]
    counts = report["dense_states"]
    lines = ["# Prepared point-visibility output validation", "",
             f"Status: **{report['status']}**. Input classification: **{report['quality']['classification']}**.", "",
             "The target is a hypothetical explicit point above DTM, not a measured landmark elevation. "
             "This validates code and raster-model consistency; no field LOS accuracy was measured.", "",
             f"Manifest: `{report['manifest']}`. Data version: `{report['source_data_version']}`.", "",
             f"Grid **{report['resolution_m']:g} m**, radius **{report['radius_m']:g} m**, "
             f"eye height **{report['eye_height_m']:g} m**, curvature coefficient **{report['curvature_coefficient']:.9g}**.", "",
             "| Output state | Cells |", "| --- | ---: |"]
    lines.extend(f"| {state} | {count:,} |" for state, count in counts.items())
    if "independent_coverage_checks" in report:
        checked = report["independent_coverage_checks"]
        coverage = report["coverage"]
        lines.extend(["", f"Independent source-raster reads verified **{checked['required_cells']:,}** required cells "
                      f"with **{checked['unknown_required_cells']:,}** unknown cells, using `{coverage['strategy']}`. "
                      f"**{checked['unknown_unrequired_cells']:,}** unknown cells occur outside that dependency region. "
                      "The reference input retained those original unknown values. The JSON records the validated radius, "
                      "version-specific dependency evidence and native padding policy.", ""])
    lines.extend(["", "Repeated uncached rasters and the cached raster matched exactly; the repeated cache query reported a memory hit. "
                  "No building-occupied, outside-radius, or outside-output-boundary cell was eligible.", "",
                  f"Compared **{report['sampling']['actual']}** eligible cell centers against the independent full-column reference: "
                  f"**{comp['false_visible_relative_to_column_reference']} false-visible** and "
                  f"**{comp['false_blocked_relative_to_column_reference']} false-blocked** native/reference disagreements. "
                  "These counts concern two surface conventions, not observed real-world errors. "
                  "Samples are balanced by native state and do not estimate prevalence-weighted accuracy.", "",
                  f"Public sparse API versus direct reference disagreements: **{comp['public_sparse_vs_direct_reference_disagreements']}**. "
                  f"Reference reciprocity failures: **{comp['reciprocity_failures']} / {comp['reciprocity_checked']}**. "
                  f"Unknown reference rays: **{comp['unknown_reference']}**.", "",
                  "| Timing | Seconds |", "| --- | ---: |",
                  f"| Initial data opening | {report['timings']['initial_open_s']:.6f} |",
                  f"| First uncached query | {report['timings']['uncached_first']['total_s']:.6f} |",
                  f"| Repeated uncached query | {report['timings']['uncached_repeat']['total_s']:.6f} |",
                  f"| Exact result-cache hit | {report['timings']['cache_hit']['total_s']:.6f} |",
                  f"| Public sparse batch | {report['timings']['public_sparse_s']:.6f} |",
                  f"| Direct reference plus reciprocity | {report['timings']['direct_reference_s']:.6f} |", "",
                  "These are validation-run timings, not a 30-run benchmark or cold-disk measurement. "
                  "The JSON report preserves exact requested/effective target positions, sampling coordinates, classifications, "
                  "source provenance, native version, and detailed timing breakdowns.", ""])
    diagnosed = [sample for sample in report["samples"] if sample.get("column_diagnostic")]
    if diagnosed:
        lines.extend(["Sampled disagreement evidence from the same prepared obstruction surface:", "",
                      "| Observer row, col | Native / reference | First reference blocker | Minimum clearance |",
                      "| --- | --- | --- | ---: |"])
        for sample in diagnosed[:12]:
            first = sample["column_diagnostic"]["first_reference_blocker"]
            blocker = f"{first['obstruction_kind']} ({first['global_row']}, {first['global_col']})" if first else "none"
            minimum = first or sample["column_diagnostic"]["lowest_clearance_column"]
            lines.append(f"| {sample['row']}, {sample['col']} | {State(sample['dense_state']).name.lower()} / "
                         f"{State(sample['reference_state']).name.lower()} | {blocker} | {minimum['minimum_clearance_m']:.6f} m |")
        lines.extend(["", "The JSON records retain complete per-sample first-blocker and worst-clearance evidence, "
                      "including DTM, surface, occupancy, ray entry/exit fractions, and zero-length corner contact flags. "
                      "These values substantiate reference decisions; they do not settle the native approximation's real-world accuracy.", ""])
    if report.get("png", {}).get("status") == "written":
        image_path = os.path.relpath(report["png"]["path"], path.parent)
        lines.extend([f"![Local point visibility raster]({image_path})", ""])
    path.write_text("\n".join(lines), encoding="utf8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--manifest", type=Path)
    source.add_argument("--diagnose-existing", type=Path, help="Write separate blocker diagnostics for an existing validation JSON")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--artifact-dir", type=Path, help="Optional separate PNG/GeoTIFF directory inside the project")
    parser.add_argument("--lon", type=float, default=126.9777)
    parser.add_argument("--lat", type=float, default=37.578)
    parser.add_argument("--height", type=float, default=100, help="Explicit metres above bare earth, not roof")
    parser.add_argument("--radius", type=float, default=1000)
    parser.add_argument("--eye-height", type=float, default=1.7)
    parser.add_argument("--resolution", type=float, default=5)
    parser.add_argument("--curvature", type=float, default=6/7)
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20260907)
    parser.add_argument("--png", action="store_true")
    parser.add_argument("--export-geotiff", action="store_true")
    parser.add_argument("--allow-synthetic", action="store_true", help="Explicit script smoke-test mode; never label synthetic input real")
    args = parser.parse_args()
    args.artifact_dir = args.artifact_dir or args.output_dir
    if not 1 <= args.samples <= 200:
        parser.error("--samples must be 1..200 for bounded reference validation")
    project_root = Path(__file__).resolve().parents[1]
    if not args.output_dir.resolve().is_relative_to(project_root):
        parser.error("--output-dir must be inside this project for complete storage accounting")
    if not args.artifact_dir.resolve().is_relative_to(project_root):
        parser.error("--artifact-dir must be inside this project for complete storage accounting")
    for name in ("validation.json", "validation.md", "validation_failure.json", "disagreement_diagnostics.json", "disagreement_diagnostics.md"):
        if (args.output_dir / name).exists():
            parser.error(f"Existing report/output would be overwritten: {args.output_dir / name}; choose a fresh output directory")
    for name in ("visibility.png", "visibility.tif"):
        if (args.artifact_dir / name).exists():
            parser.error(f"Existing artifact would be overwritten: {args.artifact_dir / name}; choose a fresh artifact directory")
    preflight(project_root, additional_bytes=32*1024**2, temporary_bytes=32*1024**2)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if args.png or args.export_geotiff:
        args.artifact_dir.mkdir(parents=True, exist_ok=True)
    if args.diagnose_existing:
        report = diagnose_existing(args.diagnose_existing, args.output_dir)
        print(json.dumps({"status": report["status"], "diagnosed_count": report["diagnosed_count"],
                          "report": str(args.output_dir / "disagreement_diagnostics.json")}, indent=2))
        return 0
    try:
        report = validate(args)
    except Exception as exc:
        failure = {"status": "failed", "error_type": type(exc).__name__, "error": str(exc),
                   "manifest": str(args.manifest), "target": {"lon": args.lon, "lat": args.lat, "height_m": args.height,
                                                               "height_reference": "agl"}, "radius_m": args.radius}
        (args.output_dir / "validation_failure.json").write_text(json.dumps(failure, indent=2)+"\n")
        print(json.dumps(failure), file=sys.stderr)
        return 1
    (args.output_dir / "validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf8")
    write_markdown(report, args.output_dir / "validation.md")
    print(json.dumps({"status": report["status"], "states": report["dense_states"],
                      "comparison": report["comparison"], "report": str(args.output_dir / "validation.json")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
