"""Explicit configuration, resource planning and atomic one-time preparation."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Mapping

import numpy as np
from osgeo import gdal, ogr, osr
from shapely import box, covers, from_wkb, make_valid, normalize, prepare as prepare_geometry, to_wkb, union_all
from shapely.geometry import MultiPolygon, Polygon

from .errors import ConfigurationError, ResourceBudgetError
from .inspect import fingerprint, source_files
from .resources import StoragePolicy, preflight, tree_bytes
from .terrain import (NODATA, create_raster, iter_tiles, open_vector, prepare_terrain,
                      read_valid, spatial_reference)

PROCESSING_VERSION = "seoul-prepare-v2"
QUALITY_BITS = {"terrain_valid": 1, "building_coverage_valid": 2,
                "height_estimated": 4, "height_unresolved": 8, "roof_conflict": 16}


def _load_config(config_path: str | Path | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(config_path, Mapping):
        config = json.loads(json.dumps(config_path))
        base = Path.cwd()
    else:
        path = Path(config_path).resolve()
        try:
            config = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise ConfigurationError(f"Cannot read JSON config {path}: {exc}") from exc
        base = path.parent
    def resolve(value: dict[str, Any]) -> None:
        for key, item in value.items():
            if key in {"path", "output_dir", "data_root"} and isinstance(item, str):
                value[key] = str((base / item).resolve())
            elif isinstance(item, dict):
                resolve(item)
            elif isinstance(item, list):
                for nested in item:
                    if isinstance(nested, dict):
                        resolve(nested)
    resolve(config)
    for name in ("output_dir", "data_root", "vertical_reference", "terrain", "buildings", "bounds"):
        if name not in config:
            raise ConfigurationError(f"Configuration requires {name!r}; see examples/config.template.json")
    if not isinstance(config["vertical_reference"], str) or not config["vertical_reference"].strip() or config["vertical_reference"].strip().lower() in {"unknown", "unspecified"}:
        raise ConfigurationError("Provide the documented DTM vertical reference; unknown datum is unsupported")
    config.setdefault("crs", "EPSG:5186")
    srs = spatial_reference(config["crs"])
    if not srs.IsProjected() or not math.isclose(srs.GetLinearUnits(), 1.0, abs_tol=1e-9):
        raise ConfigurationError("Internal CRS must be projected with metre units; EPSG:5186 is proposed for Seoul")
    resolution = config.setdefault("resolution_m", 5)
    if isinstance(resolution, bool) or resolution not in (2, 5):
        raise ConfigurationError("Prepare resolution_m must be 5 (screening) or separately sourced 2 (refinement)")
    for name in ("terrain", "buildings"):
        part = config[name]
        if name == "buildings" and part.get("assume_empty"):
            if not part.get("justification"):
                raise ConfigurationError("An explicitly empty building inventory requires a provenance justification")
            continue
        if part.get("units") != "m":
            raise ConfigurationError(f"{name}.units must explicitly be 'm'; convert other or unknown height units first")
        if part.get("vertical_reference") != config["vertical_reference"]:
            raise ConfigurationError(f"{name}.vertical_reference must match the documented DTM reference; horizontal reprojection does not convert heights")
    terrain = config["terrain"]
    if terrain.get("kind") not in ("raster", "samples"):
        raise ConfigurationError("terrain.kind must be 'raster' or 'samples'")
    if terrain["kind"] == "raster" and not terrain.get("path"):
        raise ConfigurationError("Raster terrain requires path")
    if terrain["kind"] == "samples" and not terrain.get("sources"):
        raise ConfigurationError("Sampled terrain requires explicitly mapped sources")
    buildings = config["buildings"]
    if not buildings.get("assume_empty") and not buildings.get("path"):
        raise ConfigurationError("Buildings require path; a missing inventory is not empty coverage")
    if "coverage_bounds" not in buildings:
        raise ConfigurationError("buildings.coverage_bounds is required in internal CRS; footprint bounds alone do not certify survey coverage")
    for name, bounds in (("bounds", config["bounds"]), ("buildings.coverage_bounds", buildings["coverage_bounds"])):
        if len(bounds) != 4 or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in bounds) or bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
            raise ConfigurationError(f"{name} must be finite [xmin,ymin,xmax,ymax] with positive area")
    if buildings.get("floors_field"):
        value = buildings.get("floor_height_m")
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0 or buildings.get("floors_are_above_ground") is not True:
            raise ConfigurationError("Floor estimates require positive floor_height_m and floors_are_above_ground:true")
    if buildings.get("height_field") and buildings.get("height_is_agl") is not True:
        raise ConfigurationError("Measured footprint heights require height_is_agl:true; absolute roof elevation is not an AGL height field")
    if buildings.get("height_is_estimated") and not buildings.get("height_estimation_method"):
        raise ConfigurationError("Source-estimated heights require height_estimation_method provenance")
    if buildings.get('base_estimation_method', 'median') not in ('median', 'maximum'):
        raise ConfigurationError('base_estimation_method must be median or maximum')
    if buildings.get('base_estimation_method') == 'maximum' and not buildings.get('base_estimation_justification'):
        raise ConfigurationError('Maximum-terrain base approximation requires base_estimation_justification')
    if buildings.get("missing_height_m") is not None:
        value = buildings["missing_height_m"]
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0 or not buildings.get("missing_height_justification"):
            raise ConfigurationError("Approved missing-height imputation needs positive missing_height_m and missing_height_justification")
    output, data_root = Path(config["output_dir"]), Path(config["data_root"])
    if not output.is_relative_to(data_root):
        raise ConfigurationError("output_dir must lie within data_root so derived data are included in the storage budget")
    return config


def _grid(config: dict[str, Any]) -> dict[str, Any]:
    resolution = config["resolution_m"]
    xmin, ymin, xmax, ymax = config["bounds"]
    # Fixed origin (0,0): outward rounding covers the requested rectangle.
    xmin, ymin = math.floor(xmin / resolution) * resolution, math.floor(ymin / resolution) * resolution
    xmax, ymax = math.ceil(xmax / resolution) * resolution, math.ceil(ymax / resolution) * resolution
    width, height = round((xmax - xmin) / resolution), round((ymax - ymin) / resolution)
    if width <= 0 or height <= 0:
        raise ConfigurationError("Prepared grid must contain pixels")
    return {"crs": config["crs"], "resolution_m": resolution, "width": width, "height": height,
            "bounds": [xmin, ymin, xmax, ymax], "transform": [xmin, resolution, 0, ymax, 0, -resolution]}


def _source_configs(config: dict[str, Any]) -> list[dict[str, Any]]:
    terrain = config["terrain"]
    result = [terrain] if terrain["kind"] == "raster" else list(terrain["sources"])
    if config["buildings"].get("path"):
        result.append(config["buildings"])
    if config.get("output_boundary"):
        result.append(config["output_boundary"])
    for part in (config['terrain'], config['buildings']):
        if part.get('coverage_boundary'):
            result.append(part['coverage_boundary'])
    return result


def _estimates(config: dict[str, Any], grid: dict[str, Any]) -> dict[str, int]:
    seen: set[Path] = set()
    source_bytes = 0
    external_bytes = 0
    vector_bytes = 0
    root = Path(config["data_root"])
    for item in _source_configs(config):
        for file in source_files(item["path"]):
            if file not in seen:
                seen.add(file)
                size = file.stat().st_size
                source_bytes += size
                external_bytes += size if not file.is_relative_to(root) else 0
                vector_bytes += size if file.suffix.lower() not in {".tif", ".tiff", ".vrt"} else 0
    # Lossless compression can expand noisy inputs slightly. Include TIFF
    # indexes, GPKG indexes, SQLite samples and resumable staged products.
    pixels = grid["width"] * grid["height"]
    product_bytes = math.ceil(pixels * (11 if config.get("output_boundary") else 10) * 1.1) + 8 * 1024**2
    normalized_bytes = vector_bytes * 3 + 4 * 1024**2
    sample_bytes = 0
    if config["terrain"]["kind"] == "samples":
        sample_bytes = int(config["terrain"].get("max_sample_points", 10_000_000)) * 160
    derived = product_bytes + normalized_bytes + sample_bytes
    temporary = derived + pixels + min(vector_bytes, 512 * 1024**2)  # staging + validity mask + journals
    return {"source_bytes": source_bytes, "external_source_bytes": external_bytes,
            "estimated_derived_bytes": derived, "estimated_temporary_bytes": temporary,
            "environment_installation_bytes": int(config.get("environment_installation_bytes", 0))}


def _validate_source_metadata(config: dict[str, Any], grid: dict[str, Any]) -> None:
    """Cheap metadata/field checks happen before terrain creation or hashing."""
    terrain = config["terrain"]
    if terrain["kind"] == "raster":
        if terrain.get("bare_earth_verified") is not True:
            raise ConfigurationError("Raster terrain requires bare_earth_verified:true after provenance inspection")
        source = gdal.Open(str(terrain["path"]))
        if source is None:
            raise ConfigurationError("Cannot open configured terrain raster")
        srs = source.GetSpatialRef()
        if srs is None:
            if not terrain.get("crs"):
                raise ConfigurationError("Terrain raster has unknown CRS; configure the actual source CRS")
            srs = spatial_reference(terrain["crs"])
        else:
            srs = srs.Clone()
            srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            if terrain.get("crs") and not srs.IsSame(spatial_reference(terrain["crs"])):
                raise ConfigurationError("Configured terrain CRS disagrees with raster CRS")
        band_number = terrain.get("band", 1)
        if not isinstance(band_number, int) or not 1 <= band_number <= source.RasterCount:
            raise ConfigurationError("Configured terrain band does not exist")
        band = source.GetRasterBand(band_number)
        if band.GetUnitType().strip().lower() not in {"", "m", "metre", "meter", "metres", "meters"}:
            raise ConfigurationError(f"Terrain raster declares unit {band.GetUnitType()!r}; convert to metres first")
        if band.GetScale() not in (None, 1.0) or band.GetOffset() not in (None, 0.0):
            raise ConfigurationError("Terrain has nontrivial scale/offset: convert explicitly before preparation")
        srs.StripVertical()
        transformation = osr.CoordinateTransformation(srs, spatial_reference(grid["crs"]))
        gt = source.GetGeoTransform()
        x, y = source.RasterXSize / 2, source.RasterYSize / 2
        points = [transformation.TransformPoint(*gdal.ApplyGeoTransform(gt, xx, yy))[:2]
                  for xx, yy in ((x, y), (x + 1, y), (x, y + 1))]
        native_resolution = max(math.dist(points[0], points[1]), math.dist(points[0], points[2]))
        if native_resolution > grid["resolution_m"] * 1.01:
            raise ConfigurationError(f"Requested grid would upscale a {native_resolution:.3f} m DTM; supply separately suitable finer data")
        source = None
    else:
        for source_config in terrain["sources"]:
            if source_config.get("units", "m") != "m" or source_config.get("vertical_reference", config["vertical_reference"]) != config["vertical_reference"]:
                raise ConfigurationError("Every sampled terrain source must share the configured metre units and vertical reference")
            source, layer, _ = open_vector(source_config, grid["crs"])
            field, from_z = source_config.get("elevation_field"), source_config.get("elevation_from_z") is True
            if bool(field) == from_z:
                raise ConfigurationError("Each terrain source needs exactly one elevation_field or elevation_from_z:true")
            if field and layer.GetLayerDefn().GetFieldIndex(field) < 0:
                raise ConfigurationError(f"Terrain elevation field {field!r} does not exist")
            source = None
    buildings = config["buildings"]
    if not buildings.get("assume_empty"):
        source, layer, _ = open_vector(buildings, grid["crs"])
        for mapping in ("height_field", "floors_field", "base_elevation_field"):
            field = buildings.get(mapping)
            if field and layer.GetLayerDefn().GetFieldIndex(field) < 0:
                raise ConfigurationError(f"Configured building {mapping}={field!r} does not exist")
        source = None
    if config.get("output_boundary"):
        source, _, _ = open_vector(config["output_boundary"], grid["crs"])
        source = None
    for part in (terrain, buildings):
        if part.get('coverage_boundary'):
            source, _, _ = open_vector(part['coverage_boundary'], grid['crs'])
            source = None


def plan(config_path: str | Path | Mapping[str, Any]) -> dict[str, Any]:
    """Validate explicit configuration and storage before doing large work."""
    config = _load_config(config_path)
    grid = _grid(config)
    _validate_source_metadata(config, grid)
    estimates = _estimates(config, grid)
    policy = StoragePolicy(**config.get("storage_policy", {}))
    resources = preflight(config["data_root"],
                          additional_bytes=estimates["estimated_derived_bytes"] + estimates["external_source_bytes"] + estimates["environment_installation_bytes"],
                          temporary_bytes=estimates["estimated_temporary_bytes"], policy=policy)
    return {"grid": grid, "estimates": estimates, "resources": resources,
            "steps": ["Fingerprint inputs and verify heights/CRS", "Prepare bounded terrain raster",
                      "Normalize and index footprints", "Burn maximum absolute roofs and quality masks",
                      "Publish ready manifest atomically"],
            "notes": ["Estimates reserve both final products and staging; compression savings are not assumed.",
                      "Coverage is rectangular and includes surrounding occluders. Output boundary is a separate final mask.",
                      "No vertical-datum conversion is performed. 2 m requires independently suitable source data."]}


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".writing")
    with temporary.open("w") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


@contextmanager
def _prepare_lock(path: Path):
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ConfigurationError("Another preparation job already owns this output; reuse it after completion") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _mask_for_geometry(geometry: Any, grid: dict[str, Any], window: tuple[int, int, int, int]) -> np.ndarray:
    x, y, width, height = window
    ds = gdal.GetDriverByName("MEM").Create("", width, height, 1, gdal.GDT_Byte)
    gt = grid["transform"]
    ds.SetGeoTransform([gt[0] + x * gt[1], gt[1], 0, gt[3] + y * gt[5], 0, gt[5]])
    ds.SetProjection(spatial_reference(grid["crs"]).ExportToWkt())
    memory = ogr.GetDriverByName("Memory").CreateDataSource("")
    layer = memory.CreateLayer("shape", spatial_reference(grid["crs"]), ogr.wkbUnknown)
    feature = ogr.Feature(layer.GetLayerDefn())
    feature.SetGeometry(geometry)
    layer.CreateFeature(feature)
    gdal.RasterizeLayer(ds, [1], layer, burn_values=[1], options=["ALL_TOUCHED=TRUE"])
    return ds.ReadAsArray().astype(bool)


def _geometry_window(geometry: Any, grid: dict[str, Any]) -> tuple[int, int, int, int] | None:
    xmin, xmax, ymin, ymax = geometry.GetEnvelope()
    gt, resolution = grid["transform"], grid["resolution_m"]
    x = max(0, math.floor((xmin - gt[0]) / resolution))
    y = max(0, math.floor((gt[3] - ymax) / resolution))
    right = min(grid["width"], math.ceil((xmax - gt[0]) / resolution))
    bottom = min(grid["height"], math.ceil((gt[3] - ymin) / resolution))
    return (x, y, right - x, bottom - y) if right > x and bottom > y else None


def _number(feature: Any, field: str | None) -> float | None:
    if not field:
        return None
    try:
        result = float(feature.GetField(field))
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _polygonal(geom: Any) -> Any:
    if isinstance(geom, (Polygon, MultiPolygon)):
        return geom
    parts = []
    if hasattr(geom, "geoms"):
        for part in geom.geoms:
            cleaned = _polygonal(part)
            if isinstance(cleaned, Polygon):
                parts.append(cleaned)
            elif isinstance(cleaned, MultiPolygon):
                parts.extend(cleaned.geoms)
    return MultiPolygon(parts)


def _normalise_buildings(config: dict[str, Any], grid: dict[str, Any], dtm_path: Path,
                         path: Path, progress_check: Any) -> dict[str, Any]:
    stats = {"features_read": 0, "features_retained": 0, "invalid_repaired": 0,
             "duplicates_removed": 0, "estimated_heights": 0, "unresolved_heights": 0,
             "roof_conflicts": 0, "outside_grid": 0, "partly_outside_grid": 0,
             'maximum_base_estimates': 0, 'large_terrain_relief_footprints': 0}
    temporary = path.with_name("buildings.writing.gpkg")
    # Only these exact tool-owned partial files are removed on resume.
    for ending in ("", "-wal", "-shm", "-journal"):
        Path(str(temporary) + ending).unlink(missing_ok=True)
    result = ogr.GetDriverByName("GPKG").CreateDataSource(str(temporary))
    target = result.CreateLayer("buildings", spatial_reference(grid["crs"]), ogr.wkbMultiPolygon,
                                options=["SPATIAL_INDEX=YES"])
    for name, dtype in (("source_fid", ogr.OFTString), ("base_m", ogr.OFTReal), ("height_m", ogr.OFTReal),
                        ("roof_m", ogr.OFTReal), ("estimated", ogr.OFTInteger), ("unresolved", ogr.OFTInteger),
                        ("conflict", ogr.OFTInteger), ("height_source", ogr.OFTString)):
        target.CreateField(ogr.FieldDefn(name, dtype))
    dtm = gdal.Open(str(dtm_path))
    seen_path = path.with_name(".building-dedup.sqlite")
    seen_path.unlink(missing_ok=True)
    seen = sqlite3.connect(seen_path)
    seen.execute("CREATE TABLE seen(hash TEXT PRIMARY KEY)")
    source = None
    target.StartTransaction()
    try:
        if not config.get("assume_empty"):
            source, layer, transform = open_vector(config, grid["crs"])
            for field in ("height_field", "floors_field", "base_elevation_field"):
                if config.get(field) and layer.GetLayerDefn().GetFieldIndex(config[field]) < 0:
                    raise ConfigurationError(f"Configured building {field}={config[field]!r} does not exist")
            for feature in layer:
                stats["features_read"] += 1
                if stats["features_read"] % 500 == 1:
                    progress_check()
                geometry = feature.GetGeometryRef()
                if geometry is None or geometry.IsEmpty():
                    raise ConfigurationError(f"Building {feature.GetFID()} has empty geometry; repair the source instead of silently dropping an occluder")
                geometry = geometry.Clone()
                geometry.FlattenTo2D()
                geometry.Transform(transform)
                shape = from_wkb(bytes(geometry.ExportToWkb()))
                if not shape.is_valid:
                    if config.get("invalid_geometry", "repair") != "repair":
                        raise ConfigurationError(f"Invalid building geometry at feature {feature.GetFID()}")
                    shape = _polygonal(make_valid(shape))
                    stats["invalid_repaired"] += 1
                if not isinstance(shape, (Polygon, MultiPolygon)) or shape.is_empty or shape.area <= 0:
                    raise ConfigurationError(f"Building {feature.GetFID()} cannot be repaired to polygonal area")
                shape = normalize(shape)
                if isinstance(shape, Polygon):
                    shape = MultiPolygon([shape])
                geometry = ogr.CreateGeometryFromWkb(to_wkb(shape))
                window = _geometry_window(geometry, grid)
                if window is None:
                    stats["outside_grid"] += 1
                    continue
                xmin, xmax, ymin, ymax = geometry.GetEnvelope()
                bounds = grid["bounds"]
                clipped = xmin < bounds[0] or ymin < bounds[1] or xmax > bounds[2] or ymax > bounds[3]
                stats["partly_outside_grid"] += int(clipped)
                height = _number(feature, config.get("height_field"))
                estimated = int(bool(config.get('height_is_estimated', False)))
                provenance = str(config.get('height_estimation_method')) if estimated else 'measured_height'
                if height is None or height <= 0:
                    floors = _number(feature, config.get("floors_field"))
                    if floors is not None and floors > 0:
                        height = floors * config["floor_height_m"]
                        estimated, provenance = 1, "above_ground_floors"
                    elif config.get("missing_height_m") is not None:
                        height = float(config["missing_height_m"])
                        estimated, provenance = 1, "explicit_missing_height_imputation"
                    else:
                        height, provenance = None, "unresolved"
                if height is not None and height > float(config.get("maximum_height_m", 1000)):
                    raise ConfigurationError(f"Building height {height} m exceeds maximum_height_m; check field units")
                base = _number(feature, config.get("base_elevation_field"))
                signature = hashlib.sha256(to_wkb(shape) + json.dumps([height, base, estimated]).encode()).hexdigest()
                try:
                    seen.execute("INSERT INTO seen VALUES(?)", (signature,))
                except sqlite3.IntegrityError:
                    stats["duplicates_removed"] += 1
                    continue
                x, y, width, height_px = window
                if width * height_px > int(config.get("maximum_building_window_cells", 1_000_000)):
                    raise ResourceBudgetError("A footprint's terrain window exceeds maximum_building_window_cells; inspect geometry or subset it explicitly")
                ground, valid = read_valid(dtm.GetRasterBand(1), x, y, width, height_px)
                occupied = _mask_for_geometry(geometry, grid, window)
                samples = ground[occupied & valid]
                # Full footprint support is needed even when a base attribute is
                # present; a missing slope cannot be classified behind the roof.
                unresolved = int(height is None or not len(samples) or np.any(occupied & ~valid) or
                                 (clipped and base is None))
                max_base_estimated = False
                if base is None and len(samples):
                    if config.get('base_estimation_method') == 'maximum':
                        base = float(samples.max())
                        max_base_estimated = True
                        estimated = 1
                        provenance += '; maximum DTM base approximation'
                        stats['maximum_base_estimates'] += 1
                    else:
                        base = float(np.median(samples))
                roof = None if unresolved or base is None else base + height
                large_relief = bool(len(samples) and float(samples.max() - samples.min()) > float(config.get('slope_conflict_m',20)))
                stats['large_terrain_relief_footprints'] += int(large_relief)
                # A configured maximum base is an explicit roof overestimate,
                # flagged APPROXIMATE. Keep large relief counts for review. A
                # conflicting supplied absolute base is never adjusted.
                conflict = int(roof is not None and (roof < float(samples.max()) - 0.01 or
                               (large_relief and not max_base_estimated)))
                output_feature = ogr.Feature(target.GetLayerDefn())
                output_feature.SetGeometry(geometry)
                fields = {"source_fid": str(feature.GetFID()), "base_m": base, "height_m": height,
                          "roof_m": roof, "estimated": estimated, "unresolved": unresolved,
                          "conflict": conflict, "height_source": provenance}
                for name, value in fields.items():
                    if value is not None:
                        output_feature.SetField(name, value)
                target.CreateFeature(output_feature)
                stats["features_retained"] += 1
                stats["estimated_heights"] += estimated
                stats["unresolved_heights"] += unresolved
                stats["roof_conflicts"] += conflict
                if stats["features_read"] % 1000 == 0:
                    seen.commit()
                    target.CommitTransaction()
                    target.StartTransaction()
        target.CommitTransaction()
        target.SyncToDisk()
        result.FlushCache()
    finally:
        seen.close()
        seen_path.unlink(missing_ok=True)
        result = target = dtm = source = None
    os.replace(temporary, path)
    return {**stats, "base_method": 'reliable configured absolute base; otherwise '+config.get('base_estimation_method','median')+' valid all-touched DTM cells',
            'maximum_base_justification': config.get('base_estimation_justification'),
            "overlap_policy": "maximum absolute roof; unknown overlapping height remains unresolved",
            "footprint_policy": "all_touched screening; holes retained but subcell holes/gaps can close",
            "slope_conflict_m": config.get("slope_conflict_m", 20)}


def _burn_tile(layer: Any, grid: dict[str, Any], window: tuple[int, int, int, int],
               field: str | None = None, attribute_filter: str | None = None) -> np.ndarray:
    x, y, width, height = window
    dtype = gdal.GDT_Float64 if field else gdal.GDT_Byte
    ds = gdal.GetDriverByName("MEM").Create("", width, height, 1, dtype)
    gt = grid["transform"]
    ds.SetGeoTransform([gt[0] + x * gt[1], gt[1], 0, gt[3] + y * gt[5], 0, gt[5]])
    ds.SetProjection(spatial_reference(grid["crs"]).ExportToWkt())
    if field:
        ds.GetRasterBand(1).Fill(NODATA)
    layer.SetAttributeFilter(attribute_filter)
    options = ["ALL_TOUCHED=TRUE"] + ([f"ATTRIBUTE={field}"] if field else [])
    gdal.RasterizeLayer(ds, [1], layer, burn_values=[] if field else [1], options=options)
    return ds.ReadAsArray()


def _coverage_geometry(config: dict[str, Any] | None, crs: str) -> tuple[Any, dict[str, Any]]:
    """Read a survey boundary once; complete cell coverage uses exact polygons."""
    if not config:
        return None, {}
    source, layer, transform = open_vector(config, crs)
    parts = []
    repaired = 0
    for feature in layer:
        geometry = feature.GetGeometryRef()
        if geometry is None or geometry.IsEmpty():
            raise ConfigurationError('Coverage boundary contains empty geometry')
        geometry = geometry.Clone()
        geometry.FlattenTo2D()
        geometry.Transform(transform)
        shape = from_wkb(bytes(geometry.ExportToWkb()))
        if not shape.is_valid:
            shape = make_valid(shape)
            repaired += 1
        shape = _polygonal(shape)
        if shape.is_empty:
            raise ConfigurationError('Coverage boundary must have polygonal area')
        parts.append(shape)
        if len(parts) > 10000:
            raise ResourceBudgetError('Coverage boundary exceeds 10,000 features; supply a dissolved survey polygon')
    if not parts:
        raise ConfigurationError('Coverage boundary has no polygons')
    result = union_all(parts)
    prepare_geometry(result)
    return result, {'path': config['path'], 'features': len(parts), 'invalid_repaired': repaired,
                    'policy': 'entire pixel square must be covered by supplied survey polygon'}


def _covered_cells(geometry: Any, grid: dict[str, Any], window: tuple[int, int, int, int]) -> np.ndarray:
    x, y, w, h = window
    gt = grid['transform']; r = grid['resolution_m']
    left = gt[0] + (x + np.arange(w, dtype=np.float64)) * r
    top = gt[3] - (y + np.arange(h, dtype=np.float64)) * r
    cells = box(left[None, :], top[:, None]-r, left[None, :]+r, top[:, None])
    return covers(geometry, cells)


def _apply_terrain_coverage(path: Path, grid: dict[str, Any], config: dict[str, Any],
                            progress_check: Any) -> dict[str, Any]:
    geometry, details = _coverage_geometry(config, grid['crs'])
    ds = gdal.Open(str(path), gdal.GA_Update)
    invalidated = 0
    for win in iter_tiles(grid):
        progress_check()
        values, valid = read_valid(ds.GetRasterBand(1), *win)
        covered = _covered_cells(geometry, grid, win)
        invalidated += int(np.count_nonzero(valid & ~covered))
        values[~covered] = NODATA
        ds.GetRasterBand(1).WriteArray(values, win[0], win[1])
    ds.FlushCache(); ds = None
    return {**details, 'previously_finite_cells_outside_survey': invalidated}


def _prepare_surfaces(config: dict[str, Any], grid: dict[str, Any], staging: Path,
                       progress_check: Any) -> dict[str, Any]:
    dtm = gdal.Open(str(staging / "dtm.tif"))
    buildings = gdal.OpenEx(str(staging / "buildings.gpkg"), gdal.OF_VECTOR)
    layer = buildings.GetLayerByName("buildings")
    outputs = {name: create_raster(staging / f"{name}.tif", grid,
                                  gdal.GDT_Float32 if name == "surface" else gdal.GDT_Byte)
               for name in ("surface", "occupancy", "quality")}
    counts = {key: 0 for key in QUALITY_BITS}
    coverage = config["buildings"]["coverage_bounds"]
    coverage_shape, coverage_details = _coverage_geometry(config['buildings'].get('coverage_boundary'), grid['crs'])
    r = grid["resolution_m"]
    try:
        for window in iter_tiles(grid):
            progress_check()
            x, y, width, height = window
            gt = grid["transform"]
            xmin, ymax = gt[0] + x * r, gt[3] - y * r
            xmax, ymin = xmin + width * r, ymax - height * r
            layer.SetSpatialFilterRect(xmin, ymin, xmax, ymax)
            occupancy = _burn_tile(layer, grid, window).astype(np.uint8)
            estimated = _burn_tile(layer, grid, window, attribute_filter="estimated=1").astype(bool)
            unresolved = _burn_tile(layer, grid, window, attribute_filter="unresolved=1").astype(bool)
            conflict = _burn_tile(layer, grid, window, attribute_filter="conflict=1").astype(bool)
            layer.SetAttributeFilter(None)
            # A spatial SQL query preserves the index, with ascending absolute
            # roof order so last-write wins is explicitly max, independent of
            # source feature ordering. No citywide sort or copy is retained.
            query = ("SELECT * FROM buildings WHERE roof_m IS NOT NULL AND fid IN "
                     f"(SELECT id FROM rtree_buildings_geom WHERE maxx>={xmin!r} AND minx<={xmax!r} "
                     f"AND maxy>={ymin!r} AND miny<={ymax!r}) ORDER BY roof_m ASC,fid ASC")
            roofs_layer = buildings.ExecuteSQL(query, dialect="SQLITE")
            try:
                roofs = _burn_tile(roofs_layer, grid, window, field="roof_m")
            finally:
                buildings.ReleaseResultSet(roofs_layer)
            ground, valid = read_valid(dtm.GetRasterBand(1), *window)
            surface = np.maximum(ground, roofs).astype(np.float32)
            surface[~valid] = NODATA
            # Whole-cell containment, not just centre containment, certifies
            # that no unknown fringe is labelled complete.
            left = xmin + np.arange(width) * r
            top = ymax - np.arange(height) * r
            covered = ((left >= coverage[0]) & (left + r <= coverage[2]))[None, :] & \
                      ((top - r >= coverage[1]) & (top <= coverage[3]))[:, None]
            if coverage_shape is not None:
                covered &= _covered_cells(coverage_shape, grid, window)
            flags = valid.astype(np.uint8) | (covered.astype(np.uint8) << 1) | \
                    (estimated.astype(np.uint8) << 2) | (unresolved.astype(np.uint8) << 3) | (conflict.astype(np.uint8) << 4)
            arrays = {"surface": surface, "occupancy": occupancy, "quality": flags}
            for name, array in arrays.items():
                outputs[name].GetRasterBand(1).WriteArray(array, x, y)
            for name, bit in QUALITY_BITS.items():
                counts[name] += int(np.count_nonzero(flags & bit))
        for dataset in outputs.values():
            dataset.FlushCache()
    finally:
        outputs.clear()
        dtm = layer = buildings = None
    if config.get("output_boundary"):
        source, boundary, transform = open_vector(config["output_boundary"], grid["crs"])
        normalised = ogr.GetDriverByName("Memory").CreateDataSource("")
        target = normalised.CreateLayer("boundary", spatial_reference(grid["crs"]), ogr.wkbUnknown)
        for feature in boundary:
            geometry = feature.GetGeometryRef()
            if geometry is None:
                continue
            geometry = geometry.Clone()
            geometry.Transform(transform)
            out = ogr.Feature(target.GetLayerDefn())
            out.SetGeometry(geometry)
            target.CreateFeature(out)
        mask = create_raster(staging / "output_mask.tif", grid, gdal.GDT_Byte)
        gdal.RasterizeLayer(mask, [1], target, burn_values=[1])
        mask.FlushCache()
        mask = source = normalised = None
    return {"pixel_counts": counts, "quality_bits": QUALITY_BITS,
            'building_survey_polygon': coverage_details,
            "coverage_policy": "Terrain finite and full-cell building survey coverage; unknown roofs flagged, never assumed absent"}


def prepare(config_path: str | Path | Mapping[str, Any]) -> Path:
    """Prepare once; preserve raw inputs and incomplete staged work for resume."""
    started = time.perf_counter()
    config = _load_config(config_path)
    report = plan(config)
    grid = report["grid"]
    output = Path(config["output_dir"])
    # Full content digests are a one-time preprocessing cost, never query work.
    sources = [{**fingerprint(item["path"]), "source_date": item.get("source_date")}
               for item in _source_configs(config)]
    version_payload = {"config": config, "sources": sources, "processing_version": PROCESSING_VERSION,
                       "gdal_version": gdal.VersionInfo("RELEASE_NAME")}
    version = hashlib.sha256(json.dumps(version_payload, sort_keys=True).encode()).hexdigest()
    if output.exists():
        manifest_path = output / "manifest.json"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            if manifest.get("status") == "ready" and manifest.get("data_version") == version:
                product = manifest["products"][str(config["resolution_m"])]
                required_products = ["dtm", "surface", "occupancy", "quality"]
                if config.get('output_boundary'):
                    required_products.append('output_mask')
                for name in required_products:
                    if name not in product:
                        raise ConfigurationError("Ready manifest omits a required product; use a new output directory to rebuild")
                    if not (output / product[name]).is_file():
                        raise ConfigurationError("Ready manifest has missing products; use a new output directory to rebuild")
                if not (output / manifest['processing']['footprint_index']).is_file():
                    raise ConfigurationError("Ready manifest has missing footprint index; use a new output directory to rebuild")
                return manifest_path
        raise ConfigurationError(f"Output {output} already exists with different/incomplete data; use a new output_dir (nothing was overwritten)")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / f".prepare-{version[:20]}"
    policy = StoragePolicy(**config.get("storage_policy", {}))
    with _prepare_lock(output.parent / f".prepare-{version[:20]}.lock"):
        staging.mkdir(exist_ok=True)
        progress_path = staging / "progress.json"
        progress = json.loads(progress_path.read_text()) if progress_path.exists() else {"data_version": version, "status": "preparing"}
        if progress.get("data_version") != version:
            raise ConfigurationError("Staging version mismatch; select a fresh output directory")
        _atomic_json(progress_path, progress)
        last_check = 0.0
        def check() -> None:
            nonlocal last_check
            now = time.monotonic()
            if now - last_check < 1:
                return
            last_check = now
            staged_bytes = tree_bytes(staging)
            if staged_bytes > policy.temporary_budget_bytes:
                raise ResourceBudgetError("This preparation's staged artifacts exceed temporary_budget_bytes; resumable work retained")
            remaining = max(0, report["estimates"]["estimated_derived_bytes"] - staged_bytes)
            preflight(config["data_root"], additional_bytes=remaining + report["estimates"]["external_source_bytes"],
                      policy=policy)
        check()
        if not progress.get("terrain"):
            details = prepare_terrain(config["terrain"], grid, staging / "dtm.writing.tif", check)
            if config['terrain'].get('coverage_boundary'):
                details['survey_coverage'] = _apply_terrain_coverage(staging / 'dtm.writing.tif', grid,
                    config['terrain']['coverage_boundary'], check)
            os.replace(staging / "dtm.writing.tif", staging / "dtm.tif")
            progress["terrain"] = details
            _atomic_json(progress_path, progress)
        if not progress.get("buildings"):
            progress["buildings"] = _normalise_buildings(config["buildings"], grid, staging / "dtm.tif",
                                                         staging / "buildings.gpkg", check)
            _atomic_json(progress_path, progress)
        if not progress.get("surfaces"):
            progress["surfaces"] = _prepare_surfaces(config, grid, staging, check)
            _atomic_json(progress_path, progress)
        check()
        product = {key: value for key, value in grid.items() if key != "crs"}
        product.update({name: f"{name}.tif" for name in ("dtm", "surface", "occupancy", "quality")})
        if config.get("output_boundary"):
            product["output_mask"] = "output_mask.tif"
        manifest = {"schema_version": 1, "status": "ready", "data_version": version,
                    "crs": config["crs"], "vertical_reference": config["vertical_reference"],
                    "source_kind": config.get("source_kind", "local"),
                    'provenance': config.get('provenance', {}),
                    'limitations': config.get('limitations', []),
                    "storage": {"data_root": config["data_root"], "policy": asdict(policy),
                                "external_source_bytes": report["estimates"]["external_source_bytes"]},
                    "products": {str(config["resolution_m"]): product}, "sources": sources,
                    "processing": {"version": PROCESSING_VERSION, "parameters": config,
                                   "terrain": progress["terrain"], "buildings": progress["buildings"],
                                   "quality": progress["surfaces"], "footprint_index": "buildings.gpkg",
                                   "preparation_s": time.perf_counter() - started,
                                   "storage_plan": report, "pixel_convention": "fixed origin (0,0), north-up; samples at centres",
                                   "limitations": ["2.5D columns; grid spacing is not guaranteed physical accuracy",
                                                   "All-touched footprints can close narrow gaps; no trees/overhangs unless supplied",
                                                   "Absolute roofs use approximate terrain base; vertical conversions are not performed"]}}
        _atomic_json(staging / "manifest.json", manifest)
        progress_path.unlink()
        os.replace(staging, output)
    return output / "manifest.json"
