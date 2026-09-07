"""Reproducible native-query benchmark; no privileged filesystem cache flushing."""

from __future__ import annotations

from contextlib import contextmanager
import csv
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import resource
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Iterator, Sequence
import uuid

import numpy as np
import psutil
from osgeo import gdal
from pyproj import Transformer

from . import TargetPoint, VisibilityEngine
from .resources import memory_preflight, preflight


def _directory_bytes(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _write_json_exclusive(path: Path, value: dict[str, Any]) -> None:
    """Atomically publish this tool's report without clobbering another file."""
    fd, temporary = tempfile.mkstemp(prefix=".visibility-benchmark-report-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2, sort_keys=True, default=lambda v: v.tolist() if hasattr(v, "tolist") else str(v), allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def machine_info(path: Path) -> dict[str, Any]:
    """Measure this process's machine/cgroup constraints rather than guess hardware."""
    memory = psutil.virtual_memory()
    cpu_model = platform.processor()
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.exists():
        for line in cpuinfo.read_text().splitlines():
            if line.startswith("model name"):
                cpu_model = line.partition(":")[2].strip()
                break
    cgroup: dict[str, Any] = {}
    for name in ("memory.max", "memory.current", "cpu.max", "cpuset.cpus.effective"):
        candidate = Path("/sys/fs/cgroup") / name
        if candidate.exists():
            cgroup[name] = candidate.read_text().strip()
    packages = {}
    for name in ("numpy", "pyproj", "psutil", "GDAL", "scipy", "seoul-point-visibility"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    storage: dict[str, Any] = {"type": "unknown (filesystem type does not identify physical storage)", "free_bytes": psutil.disk_usage(path).free}
    try:
        found = subprocess.run(["findmnt", "-T", str(path), "-n", "-o", "FSTYPE,SOURCE"], text=True, capture_output=True, check=False)
        storage["filesystem_and_source"] = found.stdout.strip() or "unknown"
    except OSError:
        pass
    return {
        "os": platform.platform(),
        "python": sys.version,
        "executable": sys.executable,
        "cpu_model": cpu_model,
        "logical_cpus": psutil.cpu_count(),
        "physical_cpus": psutil.cpu_count(logical=False),
        "cpu_affinity_count": len(psutil.Process().cpu_affinity()) if hasattr(psutil.Process(), "cpu_affinity") else None,
        "ram_total_bytes": memory.total,
        "ram_available_bytes": memory.available,
        "cgroup": cgroup,
        "storage": storage,
        "packages": packages,
        "gdal_release": gdal.VersionInfo("RELEASE_NAME"),
        "thread_environment": {key: os.environ.get(key) for key in ("GDAL_NUM_THREADS", "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")},
    }


@contextmanager
def _rss_peak() -> Iterator[dict[str, int]]:
    """Sample RSS in a helper thread; it does not touch any GDAL dataset handle."""
    process = psutil.Process()
    sample = {"baseline_rss_bytes": process.memory_info().rss, "peak_rss_bytes": process.memory_info().rss}
    stop = threading.Event()

    def monitor() -> None:
        while not stop.wait(0.01):
            sample["peak_rss_bytes"] = max(sample["peak_rss_bytes"], process.memory_info().rss)

    thread = threading.Thread(target=monitor, daemon=True)
    thread.start()
    try:
        yield sample
    finally:
        sample["peak_rss_bytes"] = max(sample["peak_rss_bytes"], process.memory_info().rss)
        stop.set()
        thread.join()


def _statistics(values: Sequence[float]) -> dict[str, float | int]:
    if not values:
        return {"count": 0}
    array = np.asarray(values, dtype=np.float64)
    return {"count": len(values), "median_s": float(np.median(array)), "p95_s": float(np.percentile(array, 95)), "min_s": float(array.min()), "max_s": float(array.max())}


def _startup() -> dict[str, Any]:
    # The child measures its imports; parent elapsed also includes process launch.
    # This is process startup with the existing filesystem cache, never cold disk.
    command = "import time,json;t=time.perf_counter();import seoul_visibility;print(json.dumps({'package_import_s':time.perf_counter()-t}))"
    started = time.perf_counter()
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, [str(Path(__file__).resolve().parent.parent), environment.get("PYTHONPATH")]))
    child = subprocess.run([sys.executable, "-c", command], capture_output=True, text=True, check=True, env=environment)
    return {"process_launch_and_import_s": time.perf_counter() - started, **json.loads(child.stdout)}


def _target_positions(product: dict[str, Any], radii: Sequence[float], seed: int) -> list[tuple[float, float]]:
    xmin, ymin, xmax, ymax = product["bounds"]
    resolution = float(product["resolution_m"])
    center = ((xmin + xmax) / 2, (ymin + ymax) / 2)
    maximum = min(xmax - xmin, ymax - ymin) / 2 - 3 * resolution
    fitting = [r for r in radii if r < maximum]
    if not fitting:
        return [center]
    available_offset = max(0.0, maximum - max(fitting))
    offset = min(750.0, 0.65 * available_offset)
    rng = np.random.default_rng(seed)
    # One central target and four independently placed points, in separate quadrants.
    positions = [center]
    for sx, sy in ((-1, -1), (-1, 1), (1, -1), (1, 1)):
        positions.append((center[0] + sx * offset * float(rng.uniform(0.55, 1.0)), center[1] + sy * offset * float(rng.uniform(0.55, 1.0))))
    return positions


def run_benchmark(
    manifest_path: str | Path,
    output_dir: str | Path,
    *,
    runs: int = 30,
    radii_m: Sequence[float] = (1000, 3000, 5000, 10000),
    resolutions_m: Sequence[float] = (5,),
    seed: int = 1729,
    target_height_m: float = 120.0,
    keep_export: bool = False,
) -> dict[str, Any]:
    """Measure uncached computation, exact cache lookup, sparse checks and export.

    At least thirty successful uncached samples per radius are needed for the
    intended median/p95 summary. Smaller runs are permitted for CLI smoke tests
    and prominently marked insufficient. Results stream to CSV as they complete;
    full raster results are not retained for every target or run.
    """
    if not isinstance(runs, int) or runs <= 0:
        raise ValueError("runs must be a positive integer")
    if not radii_m or any(not math.isfinite(r) or r <= 0 for r in radii_m):
        raise ValueError("Every benchmark radius must be finite and positive")
    if not math.isfinite(target_height_m) or target_height_m < 0:
        raise ValueError("target_height_m must be finite and nonnegative")
    manifest_path = Path(manifest_path).resolve()
    manifest = json.loads(manifest_path.read_text())
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    report_path = output / "benchmark.json"
    csv_path = output / "runs.csv"
    if report_path.exists() or csv_path.exists():
        raise FileExistsError("Benchmark reports already exist; use a fresh report directory")
    # Reports are small; export reserves one uint8 full-grid raster conservatively.
    largest_cells = max(p["width"] * p["height"] for p in manifest["products"].values())
    preflight(output.parent, additional_bytes=largest_cells + 8 * 1024**2, temporary_bytes=largest_cells)
    machine = machine_info(output)
    startup = _startup()
    inspection: dict[str, Any]
    try:
        from .inspect import inspect_paths

        before = time.perf_counter()
        prepared_paths = sorted({str(manifest_path.parent / p[k]) for p in manifest["products"].values() for k in ("dtm", "surface", "occupancy", "quality")})
        inspect_paths(prepared_paths)
        inspection = {"prepared_raster_inspection_s": time.perf_counter() - before, "inspected": "prepared raster metadata and blockwise missingness; raw-source inspection is a separate one-time operation"}
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        inspection = {"prepared_raster_inspection_s": None, "reason": str(exc)}
    before = time.perf_counter()
    engine = VisibilityEngine.from_manifest(manifest_path)
    opening_s = time.perf_counter() - before
    fields = ["phase", "resolution_m", "radius_m", "run", "target_id", "target_lon", "target_lat", "target_height_agl_m", "elapsed_s", "read_s", "dense_s", "mask_s", "total_s", "cache_lookup_s", "cache_status", "rows", "cols", "state_bytes", "baseline_rss_bytes", "peak_rss_bytes"]
    groups: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    target_records: list[dict[str, Any]] = []
    export_records: list[dict[str, Any]] = []
    sparse_records: list[dict[str, Any]] = []
    first_query_s = None
    transform = Transformer.from_crs(manifest["crs"], "EPSG:4326", always_xy=True)
    with csv_path.open("x", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fields)
        writer.writeheader()
        try:
            for resolution in resolutions_m:
                key = str(int(resolution)) if float(resolution).is_integer() else str(resolution)
                if key not in manifest["products"]:
                    skipped.append({"resolution_m": resolution, "reason": "Separately prepared resolution is unavailable; no upsampling performed."})
                    continue
                product = manifest["products"][key]
                positions = _target_positions(product, radii_m, seed)
                targets = [TargetPoint(*transform.transform(x, y), target_height_m, "agl") for x, y in positions]
                target_records.extend({"resolution_m": resolution, "id": i, "requested_xy": list(positions[i]), "lon": t.lon, "lat": t.lat, "height_agl_m": target_height_m} for i, t in enumerate(targets))
                xmin, ymin, xmax, ymax = product["bounds"]
                for radius in radii_m:
                    available = []
                    prewarm_s = []
                    for target_id, (target, (x, y)) in enumerate(zip(targets, positions)):
                        if min(x - xmin, xmax - x, y - ymin, ymax - y) < radius + 2 * resolution:
                            skipped.append({"resolution_m": resolution, "radius_m": radius, "target_id": target_id, "reason": "Insufficient rectangular input coverage for the computational window."})
                            continue
                        try:
                            before = time.perf_counter()
                            warmed = engine.visible_from_target(target, radius_m=radius, resolution_m=resolution, use_cache=False, offline=radius > 10000)
                            elapsed = time.perf_counter() - before
                            if first_query_s is None:
                                first_query_s = elapsed
                            prewarm_s.append(elapsed)
                            available.append(target_id)
                            del warmed
                        except (ValueError, RuntimeError) as exc:
                            skipped.append({"resolution_m": resolution, "radius_m": radius, "target_id": target_id, "reason": str(exc)})
                    if not available:
                        continue
                    timings: dict[str, list[float]] = {key: [] for key in ("elapsed_s", "read_s", "dense_s", "mask_s", "total_s")}
                    cache_timings: list[float] = []
                    group_peak = 0
                    last_result = None
                    for run in range(runs):
                        target_id = available[run % len(available)]
                        target = targets[target_id]
                        with _rss_peak() as rss:
                            before = time.perf_counter()
                            result = engine.visible_from_target(target, radius_m=radius, resolution_m=resolution, use_cache=False, offline=radius > 10000)
                            elapsed = time.perf_counter() - before
                        row = {"phase": "warm_uncached", "resolution_m": resolution, "radius_m": radius, "run": run, "target_id": target_id, "target_lon": target.lon, "target_lat": target.lat, "target_height_agl_m": target_height_m, "elapsed_s": elapsed, "cache_status": result.metadata["cache_status"], "rows": result.states.shape[0], "cols": result.states.shape[1], "state_bytes": result.states.nbytes, **rss}
                        row.update({name: result.timings.get(name, 0.0) for name in ("read_s", "dense_s", "mask_s", "total_s", "cache_lookup_s")})
                        writer.writerow(row)
                        csv_file.flush()
                        for name in timings:
                            timings[name].append(float(row[name]))
                        group_peak = max(group_peak, rss["peak_rss_bytes"])
                        last_result = result
                    # Populate exactly one result, then time identical lookups.
                    target_id = available[0]
                    target = targets[target_id]
                    cached = engine.visible_from_target(target, radius_m=radius, resolution_m=resolution, use_cache=True, offline=radius > 10000)
                    for run in range(runs):
                        before = time.perf_counter()
                        cached = engine.visible_from_target(target, radius_m=radius, resolution_m=resolution, use_cache=True, offline=radius > 10000)
                        elapsed = time.perf_counter() - before
                        if cached.metadata["cache_status"] not in ("memory_hit", "disk_hit"):
                            raise RuntimeError(f"Expected an exact cache hit, got {cached.metadata['cache_status']}")
                        cache_timings.append(elapsed)
                        writer.writerow({"phase": "exact_cache_hit", "resolution_m": resolution, "radius_m": radius, "run": run, "target_id": target_id, "target_lon": target.lon, "target_lat": target.lat, "target_height_agl_m": target_height_m, "elapsed_s": elapsed, "cache_status": cached.metadata["cache_status"], "rows": cached.states.shape[0], "cols": cached.states.shape[1], "state_bytes": cached.states.nbytes, **{k: cached.timings.get(k, 0.0) for k in ("read_s", "dense_s", "mask_s", "total_s", "cache_lookup_s")}})
                    csv_file.flush()
                    group = {"resolution_m": resolution, "radius_m": radius, "backend": cached.metadata["backend"], "targets_used": len(available), "prewarm_queries": len(prewarm_s), "prewarm_total_s": sum(prewarm_s), "warm_uncached": {name: _statistics(values) for name, values in timings.items()}, "exact_cache_hit": _statistics(cache_timings), "peak_sampled_rss_bytes": group_peak, "output_shape": list(cached.states.shape), "output_array_bytes": cached.states.nbytes, "useful_sample_count": runs >= 30}
                    if radius == 5000 and resolution == 5:
                        stats = group["warm_uncached"]["elapsed_s"]
                        group["design_goal"] = {"median_target_s": 1.0, "p95_target_s": 3.0, "median_met": stats["median_s"] <= 1.0, "p95_met": stats["p95_s"] <= 3.0}
                    groups.append(group)
                    # A small order-preserving sparse batch, never the dense core.
                    angles = np.arange(16) * (2 * np.pi / 16)
                    x, y = positions[target_id]
                    observer_x = x + np.cos(angles) * radius * 0.75
                    observer_y = y + np.sin(angles) * radius * 0.75
                    observer_lon, observer_lat = transform.transform(observer_x, observer_y)
                    coordinates = np.column_stack([observer_lon, observer_lat])
                    before = time.perf_counter()
                    sparse = engine.check_observers(target, coordinates, resolution_m=resolution)
                    sparse_records.append({"resolution_m": resolution, "radius_m": radius, "observers": len(coordinates), "elapsed_s": time.perf_counter() - before, "state_counts": np.bincount(sparse.states, minlength=4).tolist(), "metadata": sparse.metadata, "jit_startup_s": 0.0, "implementation": "Independent Python column-intersection LOS; no JIT compilation"})
                    # Exports belong to the manifest's budgeted data directory.
                    # UUID names protect other runs and any pre-existing files.
                    export_path = manifest_path.parent / f".visibility-benchmark-{uuid.uuid4().hex}-{key}m-{int(radius)}m.tif"
                    before = time.perf_counter()
                    try:
                        cached.export_geotiff(export_path)
                        export_records.append({"resolution_m": resolution, "radius_m": radius, "elapsed_s": time.perf_counter() - before, "disk_bytes": export_path.stat().st_size, "retained": keep_export, "path": str(export_path) if keep_export else None})
                    finally:
                        if not keep_export:
                            export_path.unlink(missing_ok=True)
                    del cached, last_result, result
        finally:
            engine.close()
    maximum_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform != "darwin":
        maximum_rss *= 1024
    report = {
        "report_schema_version": 1,
        "source_kind": manifest.get("source_kind", "unspecified"),
        "source_data_version": manifest["data_version"],
        "manifest": str(manifest_path),
        "source_region": {"crs": manifest["crs"], "products": {key: {name: p[name] for name in ("width", "height", "resolution_m", "bounds")} for key, p in manifest["products"].items()}},
        "machine": machine,
        "seed": seed,
        "geometry": {"eye_height_m": 1.7, "curvature_coefficient": 6 / 7, "target_height_agl_m": target_height_m},
        "gdal_block_cache_bytes": gdal.GetCacheMax(),
        "runs_requested_per_radius": runs,
        "startup": startup,
        "initial_data_open_s": opening_s,
        "first_query_existing_filesystem_cache_s": first_query_s,
        "one_time": {**inspection, "preprocessing_recorded_s": manifest.get("processing", {}).get("preparation_s"), "preprocessing_provenance": "Recorded during actual product creation, if provided by its manifest."},
        "targets": target_records,
        "groups": groups,
        "sparse": sparse_records,
        "exports": export_records,
        "skipped": skipped,
        "process_peak_rss_bytes": maximum_rss,
        "prepared_data_disk_bytes": _directory_bytes(manifest_path.parent),
        "result_cache_disk_bytes": 0,
        "result_cache_policy": "Engine's bounded RAM result cache; no disk result cache is implemented.",
        "notes": [
            "Synthetic source_kind means entirely fictional terrain/buildings; these measurements establish no Seoul accuracy.",
            "Warm uncached runs disable result caching. All target locations are prewarmed; no cold-disk performance is claimed.",
            "Large windows may exceed the engine's bounded data block cache; filesystem caching remains uncontrolled.",
            "One native target-centred viewshed per uncached query. Radius and eligibility masks are timed separately.",
            "Export and sparse checks are outside dense query timing. RSS sampling interval is 10 ms; process high-water RSS is also recorded.",
            "Runs cycle through spatially distributed requested targets with an explicit 120 m default AGL height; target coordinates and seed are recorded.",
            "At least 30 timed runs per radius are required for an initial useful median/p95 estimate; shorter smoke tests are marked insufficient.",
        ],
    }
    _write_json_exclusive(report_path, report)
    return {"report": str(report_path), "runs_csv": str(csv_path), "source_kind": report["source_kind"], "groups": groups, "skipped_count": len(skipped)}


def profile_block_cache(
    manifest_path: str | Path,
    output_path: str | Path,
    *,
    runs: int = 30,
    cache_sizes_mib: Sequence[int] = (64, 128, 256),
    seed: int = 1729,
) -> dict[str, Any]:
    """Measure the observed read bottleneck at 5 km, without changing geometry.

    The source datasets are reopened between bounded GDAL cache sizes and five
    target locations are prewarmed. Clearing GDAL's own cache is unprivileged;
    the filesystem cache is neither cleared nor called cold. Every output is
    hashed outside the timer to ensure this optimization preserves its states.
    """
    if runs <= 0 or any(size <= 0 for size in cache_sizes_mib):
        raise ValueError("runs and cache sizes must be positive")
    manifest_path = Path(manifest_path).resolve()
    output_path = Path(output_path).resolve()
    if output_path.exists():
        raise FileExistsError(f"{output_path} exists; preserve prior profile and use another path")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(manifest_path.read_text())
    product = manifest["products"]["5"]
    xy = _target_positions(product, [5000, 10000], seed)
    projection = Transformer.from_crs(manifest["crs"], 4326, always_xy=True)
    targets = [TargetPoint(*projection.transform(x, y), 120, "agl") for x, y in xy]
    report: dict[str, Any] = {
        "source_kind": manifest.get("source_kind", "unspecified"),
        "source_data_version": manifest["data_version"],
        "machine": machine_info(output_path.parent),
        "radius_m": 5000,
        "resolution_m": 5,
        "seed": seed,
        "runs_per_cache_size": runs,
        "targets_xy": xy,
        "result_cache": "disabled",
        "filesystem_cache": "warm/uncontrolled; no OS cache flushing",
        "read_s_includes": "GDAL window reads plus strict coverage and surface-contract validation",
        "profiles": [],
    }
    expected: dict[int, str] = {}
    original_cache = gdal.GetCacheMax()
    try:
        for cache_mib in cache_sizes_mib:
            memory_preflight(2003**2 * 48 + int(cache_mib * 1024**2))
            gdal.SetCacheMax(0)
            gdal.SetCacheMax(int(cache_mib * 1024**2))
            records: list[dict[str, Any]] = []
            with VisibilityEngine.from_manifest(manifest_path) as engine:
                for target in targets:
                    engine.visible_from_target(target, radius_m=5000, use_cache=False)
                for run in range(runs):
                    target_id = run % len(targets)
                    with _rss_peak() as rss:
                        before = time.perf_counter()
                        result = engine.visible_from_target(targets[target_id], radius_m=5000, use_cache=False)
                        elapsed = time.perf_counter() - before
                    digest = hashlib.sha256(result.states.tobytes()).hexdigest()
                    if target_id in expected and expected[target_id] != digest:
                        raise AssertionError("Changing GDAL block cache changed visibility output")
                    expected[target_id] = digest
                    records.append({"run": run, "target_id": target_id, "elapsed_s": elapsed, "read_s": result.timings["read_s"], "dense_s": result.timings["dense_s"], "mask_s": result.timings["mask_s"], **rss})
            report["profiles"].append({"gdal_block_cache_mib": cache_mib, "summary": {key: _statistics([row[key] for row in records]) for key in ("elapsed_s", "read_s", "dense_s", "mask_s")}, "peak_sampled_rss_bytes": max(row["peak_rss_bytes"] for row in records), "runs": records})
        report["identical_output_hashes_per_target"] = expected
    finally:
        gdal.SetCacheMax(original_cache)
    _write_json_exclusive(output_path, report)
    return report
