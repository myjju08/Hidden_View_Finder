"""Deterministic, explicitly fictional fixtures; no source data are downloaded."""

from __future__ import annotations

import hashlib
from dataclasses import asdict
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
import time
from typing import Any

import numpy as np
from osgeo import gdal

from .resources import StoragePolicy, memory_preflight, preflight


def _terrain(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Smooth fictional ground in metres relative to the fixture centre."""
    return (
        80.0 + 0.0008 * x + 0.0005 * y
        + 48.0 * np.exp(-((x - 680.0) / 150.0) ** 2)
        * np.exp(-(y / 4500.0) ** 2)
        + 18.0 * np.exp(-((y + 1350.0) / 380.0) ** 2)
    )


def create_synthetic(
    output_dir: str | Path,
    *,
    size_m: float = 2400.0,
    resolution_m: float = 5.0,
    seed: int = 1729,
    project_data_root: str | Path | None = None,
    policy: StoragePolicy | None = None,
) -> Path:
    """Write a bounded-memory fixture with terrain, roofs and complete coverage.

    ``size_m`` is a minimum side length. An odd number of pixels is used, and
    grid edges are aligned to integer multiples of the resolution in EPSG:5186.
    A 24,000 m fixture supports all benchmark radii and distributed targets.
    Existing matching fixtures are reused; other existing directories are never
    overwritten. All output is staged and published with one directory rename.
    """
    started = time.perf_counter()
    if not math.isfinite(size_m) or size_m <= 0:
        raise ValueError("size_m must be finite and positive")
    if resolution_m not in (2, 5):
        raise ValueError("Synthetic resolution_m must be 2 or 5 metres")
    if not isinstance(seed, int):
        raise ValueError("seed must be an integer")
    output = Path(output_dir).resolve()
    width = int(math.ceil(size_m / resolution_m))
    width += 1 - width % 2
    params = {
        "generator": "fictional-seoul-grid-v1",
        "width": width,
        "resolution_m": resolution_m,
        "seed": seed,
    }
    version = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()
    manifest_path = output / "manifest.json"
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text())
        if existing.get("status") == "ready" and existing.get("data_version") == version:
            product = existing["products"][str(int(resolution_m))]
            if all((output / product[key]).is_file() for key in ("dtm", "surface", "occupancy", "quality")):
                return manifest_path
        raise FileExistsError(f"{output} exists with a different or incomplete product; use a new directory")
    if output.exists():
        raise FileExistsError(f"{output} already exists; use a new output directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    budget_root = Path(project_data_root).resolve() if project_data_root else output.parent
    estimated = width * width * 10 + 16 * 1024**2
    preflight(budget_root, additional_bytes=estimated, temporary_bytes=estimated, policy=policy)
    memory_preflight(width * min(width, 256) * 96 + 64 * 1024**2)
    staging = Path(tempfile.mkdtemp(prefix=".synthetic-", dir=output.parent))
    datasets: dict[str, Any] = {}
    try:
        from osgeo import osr

        srs = osr.SpatialReference()
        srs.ImportFromEPSG(5186)
        center_x, center_y = 200_000.0, 550_000.0
        x0 = center_x - (width // 2) * resolution_m
        y0 = center_y + (width // 2 + 1) * resolution_m
        transform = [x0, resolution_m, 0.0, y0, 0.0, -resolution_m]
        driver = gdal.GetDriverByName("GTiff")
        for name in ("dtm", "surface", "occupancy", "quality"):
            dtype = gdal.GDT_Float32 if name in ("dtm", "surface") else gdal.GDT_Byte
            ds = driver.Create(
                str(staging / f"{name}.tif"), width, width, 1, dtype,
                options=["TILED=YES", "BLOCKXSIZE=256", "BLOCKYSIZE=256", "COMPRESS=DEFLATE", "PREDICTOR=3" if dtype == gdal.GDT_Float32 else "PREDICTOR=2", "NUM_THREADS=1"],
            )
            if ds is None:
                raise RuntimeError(f"GDAL failed to create synthetic {name}")
            ds.SetGeoTransform(transform)
            ds.SetProjection(srs.ExportToWkt())
            if dtype == gdal.GDT_Float32:
                ds.GetRasterBand(1).SetNoDataValue(-9999.0)
                ds.GetRasterBand(1).SetUnitType("m")
            datasets[name] = ds
        # One-dimensional coordinate vectors and 256-row strips bound working RAM.
        x = x0 + (np.arange(width, dtype=np.float64) + 0.5) * resolution_m - center_x
        for row in range(0, width, 256):
            count = min(256, width - row)
            y = y0 - (np.arange(row, row + count, dtype=np.float64) + 0.5) * resolution_m - center_y
            xx, yy = x[None, :], y[:, None]
            ground = _terrain(xx, yy).astype(np.float32)
            bx = np.floor((xx + 4000) / 250).astype(np.int64)
            by = np.floor((yy + 4000) / 180).astype(np.int64)
            local_x = (xx + 4000) % 250
            local_y = (yy + 4000) % 180
            occupied = (local_x >= 35) & (local_x <= 175) & (local_y >= 30) & (local_y <= 110)
            occupied &= (np.abs(xx) < 3200) & (np.abs(yy) < 3200)
            # Selected buildings contain an explicit courtyard hole.
            hole = ((bx + by) % 4 == 0) & (local_x > 75) & (local_x < 130) & (local_y > 55) & (local_y < 85)
            occupied &= ~hole
            base = _terrain(bx * 250 - 4000 + 105, by * 180 - 4000 + 70)
            height = 18 + ((bx * 19 + by * 31 + seed) % 63)
            roofs = np.maximum(ground, base + height)
            surface = np.where(occupied, roofs, ground).astype(np.float32)
            arrays = {"dtm": ground, "surface": surface, "occupancy": occupied.astype(np.uint8), "quality": np.full(ground.shape, 3, dtype=np.uint8)}
            for name, array in arrays.items():
                datasets[name].GetRasterBand(1).WriteArray(array, 0, row)
            if row % 1024 == 0:
                # Staged bytes already count in current usage: reserve only the
                # remaining estimated bytes. No source or unrelated file is removed.
                written = sum(p.stat().st_size for p in staging.glob("*.tif"))
                preflight(budget_root, additional_bytes=max(0, estimated - written), temporary_bytes=estimated, policy=policy)
        for ds in datasets.values():
            ds.FlushCache()
        ds = None
        datasets.clear()
        product = {
            "resolution_m": resolution_m,
            "transform": transform,
            "width": width,
            "height": width,
            "bounds": [x0, y0 - width * resolution_m, x0 + width * resolution_m, y0],
            **{name: f"{name}.tif" for name in ("dtm", "surface", "occupancy", "quality")},
        }
        manifest = {
            "schema_version": 1,
            "status": "ready",
            "data_version": version,
            "crs": "EPSG:5186",
            "vertical_reference": "synthetic local orthometric datum (not real Seoul)",
            "source_kind": "synthetic",
            "products": {str(int(resolution_m)): product},
            "sources": [],
            "storage": {"data_root": str(budget_root), "policy": asdict(policy or StoragePolicy()), "external_source_bytes": 0},
            "processing": {
                **params,
                "preparation_s": time.perf_counter() - started,
                "surface_model": "Fictional smooth terrain plus constant synthetic roof columns; no Seoul source data.",
                "coverage": "Complete fictional terrain and buildings throughout the rectangular raster.",
                "output_mask": "No Seoul administrative mask: fixture is not real Seoul geography.",
                "quality_bits": {"terrain_valid": 1, "building_coverage_valid": 2, "height_estimated": 4, "height_unresolved": 8, "roof_conflict": 16},
                "seed": seed,
            },
        }
        manifest_file = staging / "manifest.json"
        with manifest_file.open("w") as stream:
            json.dump(manifest, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staging, output)
        return manifest_path
    except BaseException:
        datasets.clear()
        shutil.rmtree(staging, ignore_errors=True)
        raise
