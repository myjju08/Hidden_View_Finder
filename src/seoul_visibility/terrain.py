"""Bounded-memory terrain preparation from a verified DTM or sampled contours.

Sampled-contour triangulation is unconstrained. It approximates the supplied
terrain and does not preserve contour lines as enforced TIN edges. Every output
tile uses a spatial-indexed halo; unsupported triangles remain NoData.
"""
from __future__ import annotations

import json
import hashlib
import math
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Iterator

import numpy as np
from osgeo import gdal, ogr, osr

from .errors import ConfigurationError, ResourceBudgetError
from .resources import memory_preflight

NODATA = -3.4028234663852886e38
GTIFF_OPTIONS = ["TILED=YES", "COMPRESS=DEFLATE", "PREDICTOR=3", "BIGTIFF=IF_SAFER",
                 "BLOCKXSIZE=256", "BLOCKYSIZE=256", "NUM_THREADS=1"]


def spatial_reference(value: str | int) -> osr.SpatialReference:
    result = osr.SpatialReference()
    if result.SetFromUserInput(str(value)) != 0:
        raise ConfigurationError(f"Invalid CRS: {value!r}")
    result.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return result


def open_vector(config: dict[str, Any], destination_crs: str) -> tuple[Any, Any, Any]:
    path = Path(config["path"])
    options = [f"ENCODING={config['encoding']}"] if config.get("encoding") else []
    dataset = gdal.OpenEx(str(path), gdal.OF_VECTOR | gdal.OF_READONLY, open_options=options)
    if dataset is None:
        raise ConfigurationError(f"Cannot open vector: {path}")
    if "layer" not in config and dataset.GetLayerCount() != 1:
        raise ConfigurationError(f"{path}: multiple layers; supply an explicit layer name")
    layer = dataset.GetLayerByName(config["layer"]) if config.get("layer") else dataset.GetLayerByIndex(0)
    if layer is None:
        raise ConfigurationError(f"Layer {config.get('layer')!r} absent from {path}")
    source_srs = layer.GetSpatialRef()
    explicit = spatial_reference(config["crs"]) if config.get("crs") else None
    if source_srs is None or (source_srs.GetName() or "").lower().startswith("undefined"):
        if explicit is None:
            raise ConfigurationError(f"{path}: unknown CRS; provide its actual source CRS in config")
        source_srs = explicit
    else:
        source_srs = source_srs.Clone()
        source_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        if explicit and not source_srs.IsSame(explicit):
            raise ConfigurationError(f"{path}: configured source CRS disagrees with declared CRS")
    source_srs.StripVertical()
    destination = spatial_reference(destination_crs)
    if path.suffix.lower() == ".dxf":
        layers = config.get("cad_layers")
        if not layers:
            raise ConfigurationError(f"{path}: specify cad_layers from inspect; DXF entities are not all terrain")
        quote = lambda text: "'" + str(text).replace("'", "''") + "'"
        layer.SetAttributeFilter('"Layer" IN (' + ",".join(quote(x) for x in layers) + ")")
    transform = osr.CoordinateTransformation(source_srs, destination)
    return dataset, layer, transform


def create_raster(path: Path, grid: dict[str, Any], dtype: int = gdal.GDT_Float32) -> Any:
    options = GTIFF_OPTIONS if dtype == gdal.GDT_Float32 else [x for x in GTIFF_OPTIONS if not x.startswith("PREDICTOR=")]
    dataset = gdal.GetDriverByName("GTiff").Create(str(path), grid["width"], grid["height"], 1,
                                                 dtype, options=options)
    dataset.SetGeoTransform(grid["transform"])
    dataset.SetProjection(spatial_reference(grid["crs"]).ExportToWkt())
    if dtype == gdal.GDT_Float32:
        dataset.GetRasterBand(1).SetNoDataValue(NODATA)
    return dataset


def iter_tiles(grid: dict[str, Any], tile_size: int = 256) -> Iterator[tuple[int, int, int, int]]:
    for y in range(0, grid["height"], tile_size):
        for x in range(0, grid["width"], tile_size):
            yield x, y, min(tile_size, grid["width"] - x), min(tile_size, grid["height"] - y)


def read_valid(band: Any, x: int, y: int, width: int, height: int) -> tuple[np.ndarray, np.ndarray]:
    array = band.ReadAsArray(x, y, width, height).astype(np.float32, copy=False)
    valid = np.isfinite(array)
    nodata = band.GetNoDataValue()
    if nodata is not None:
        valid &= array != nodata
    return array, valid


def _raster_terrain(config: dict[str, Any], grid: dict[str, Any], output: Path) -> dict[str, Any]:
    if config.get("bare_earth_verified") is not True:
        raise ConfigurationError("Raster terrain requires bare_earth_verified:true after inspecting its provenance")
    source = gdal.Open(str(config["path"]), gdal.GA_ReadOnly)
    srs = source.GetSpatialRef()
    if srs is None:
        if not config.get("crs"):
            raise ConfigurationError("Terrain raster has unknown CRS; configure the actual source CRS")
        srs = spatial_reference(config["crs"])
    else:
        srs = srs.Clone()
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        if config.get("crs") and not srs.IsSame(spatial_reference(config["crs"])):
            raise ConfigurationError("Configured terrain CRS disagrees with raster CRS")
    srs.StripVertical()
    band_number = int(config.get("band", 1))
    if not 1 <= band_number <= source.RasterCount:
        raise ConfigurationError("Configured terrain band does not exist")
    band = source.GetRasterBand(band_number)
    if band.GetUnitType().strip().lower() not in {"", "m", "metre", "meter", "metres", "meters"}:
        raise ConfigurationError(f"Terrain raster declares unit {band.GetUnitType()!r}; convert it to metres first")
    if band.GetScale() not in (None, 1.0) or band.GetOffset() not in (None, 0.0):
        raise ConfigurationError("Terrain has nontrivial scale/offset: explicitly convert to metre elevations first")
    transform = osr.CoordinateTransformation(srs, spatial_reference(grid["crs"]))
    gt = source.GetGeoTransform()
    x, y = source.RasterXSize / 2, source.RasterYSize / 2
    pts = [transform.TransformPoint(*gdal.ApplyGeoTransform(gt, xx, yy))[:2]
           for xx, yy in ((x, y), (x + 1, y), (x, y + 1))]
    source_resolution = max(math.dist(pts[0], pts[1]), math.dist(pts[0], pts[2]))
    if source_resolution > grid["resolution_m"] * 1.01:
        raise ConfigurationError(f"Requested {grid['resolution_m']} m grid would upscale a {source_resolution:.3f} m DTM; "
                                 "supply independently prepared finer terrain")
    # A single-band VRT avoids materialising another copy. Explicit horizontal
    # SRSs prevent a DEM warp from silently introducing a vertical-datum shift.
    selected = gdal.Translate("", source, format="VRT", bandList=[band_number])
    result = gdal.Warp(str(output), selected, format="GTiff", srcSRS=srs.ExportToWkt(),
                       dstSRS=spatial_reference(grid["crs"]).ExportToWkt(),
                       outputBounds=grid["bounds"], width=grid["width"], height=grid["height"],
                       outputType=gdal.GDT_Float32, dstNodata=NODATA, resampleAlg="bilinear",
                       multithread=False, warpMemoryLimit=64, creationOptions=GTIFF_OPTIONS)
    if result is None:
        raise ConfigurationError("GDAL terrain reprojection failed")
    # Warp an explicit source-validity band with minimum aggregation, retaining
    # zero (unknown) as a value. GDAL's normal elevation warp may renormalise
    # bilinear weights around NoData. Expand unknown support by one output cell
    # so it cannot quietly interpolate across such a hole.
    validity_vrt = gdal.Translate("", source, format="VRT", bandList=[f"mask,{band_number}"])
    validity_path = output.with_name("terrain-validity.writing.tif")
    validity = gdal.Warp(str(validity_path), validity_vrt, format="GTiff", srcSRS=srs.ExportToWkt(),
                         dstSRS=spatial_reference(grid["crs"]).ExportToWkt(),
                         outputBounds=grid["bounds"], width=grid["width"], height=grid["height"],
                         outputType=gdal.GDT_Byte, srcNodata=None, dstNodata=0, resampleAlg="min",
                         multithread=False, warpMemoryLimit=32,
                         creationOptions=[v for v in GTIFF_OPTIONS if not v.startswith("PREDICTOR=")])
    from scipy.ndimage import minimum_filter
    valid_count = 0
    for tx, ty, w, h in iter_tiles(grid):
        values, valid = read_valid(result.GetRasterBand(1), tx, ty, w, h)
        left, top = max(0, tx - 1), max(0, ty - 1)
        right, bottom = min(grid["width"], tx + w + 1), min(grid["height"], ty + h + 1)
        support = validity.ReadAsArray(left, top, right - left, bottom - top)
        support = minimum_filter(support, size=3, mode="nearest")
        valid &= support[ty - top:ty - top + h, tx - left:tx - left + w] == 255
        values[~valid] = NODATA
        result.GetRasterBand(1).WriteArray(values, tx, ty)
        valid_count += int(valid.sum())
    result.FlushCache()
    result = selected = source = validity = validity_vrt = None
    validity_path.unlink(missing_ok=True)
    return {"method": "verified bare-earth raster, bilinear horizontal warp", "source_resolution_m": source_resolution,
            "valid_pixels": valid_count, "vertical_conversion": "none",
            "missing_support": "minimum-warped source validity, unknowns conservatively expanded by one output cell"}


def _vertices(geometry: ogr.Geometry, max_vertices: int = 1_000_000) -> Iterator[list[tuple[float, float, float]]]:
    if geometry.GetGeometryCount():
        for index in range(geometry.GetGeometryCount()):
            yield from _vertices(geometry.GetGeometryRef(index), max_vertices)
    else:
        if geometry.GetPointCount() > max_vertices:
            raise ResourceBudgetError(f"Terrain feature exceeds max_feature_vertices={max_vertices}; split it before preparation")
        yield [geometry.GetPoint(i)[:3] for i in range(geometry.GetPointCount())]


def _contour_samples(vertices: list[tuple[float, float, float]], spacing: float,
                     mode: str) -> Iterator[tuple[float, float, float]]:
    """Sample a polyline in projected XY while retaining its supplied elevation.

    Regular arclength follows the original polyline, not endpoint chords. It
    retains both endpoints but intentionally does not index every dense CAD
    vertex. It is supported only for constant-height contours; varying geometry
    Z must use preserve_vertices so extrema cannot disappear silently.
    """
    if mode not in {"preserve_vertices", "regular_arclength"}:
        raise ConfigurationError("contour_sampling must be preserve_vertices or regular_arclength")
    if not vertices:
        return
    if not math.isfinite(spacing) or spacing <= 0:
        raise ConfigurationError("sample_spacing_m must be finite and positive")
    if mode == "preserve_vertices":
        yield vertices[0]
        for a, b in zip(vertices, vertices[1:]):
            steps = max(1, math.ceil(math.dist(a[:2], b[:2]) / spacing))
            for step in range(1, steps + 1):
                u = step / steps
                yield tuple(a[j] + u * (b[j] - a[j]) for j in range(3))
        return
    points = np.asarray(vertices, dtype=np.float64)
    if not np.all(np.isfinite(points)):
        raise ConfigurationError("Nonfinite transformed terrain coordinates/elevation")
    if np.any(points[:, 2] != points[0, 2]):
        raise ConfigurationError("regular_arclength requires constant contour elevation; "
                                 "use preserve_vertices for varying geometry Z to retain extrema")
    yield vertices[0]
    if len(points) == 1:
        return
    lengths = np.linalg.norm(np.diff(points[:, :2], axis=0), axis=1)
    cumulative = np.concatenate(([0.0], np.cumsum(lengths)))
    total = cumulative[-1]
    if total > 0:
        distances = np.arange(spacing, total, spacing, dtype=np.float64)
        segments = np.searchsorted(cumulative, distances, side="right") - 1
        fractions = (distances - cumulative[segments]) / lengths[segments]
        locations = points[segments, :2] + fractions[:, None] * (
            points[segments + 1, :2] - points[segments, :2])
        for x, y in locations:
            yield float(x), float(y), float(points[0, 2])
    yield vertices[-1]


def _build_sample_index(config: dict[str, Any], grid: dict[str, Any], path: Path,
                        progress_check: Any = lambda: None) -> dict[str, Any]:
    spacing = float(config.get("sample_spacing_m", max(grid["resolution_m"], 10)))
    if not math.isfinite(spacing) or spacing <= 0:
        raise ConfigurationError("sample_spacing_m must be finite and positive")
    sampling_mode = config.get("contour_sampling", "preserve_vertices")
    if sampling_mode not in {"preserve_vertices", "regular_arclength"}:
        raise ConfigurationError("contour_sampling must be preserve_vertices or regular_arclength")
    max_points = int(config.get("max_sample_points", 10_000_000))
    tolerance = float(config.get("duplicate_elevation_tolerance_m", 0.01))
    connection = sqlite3.connect(path)
    connection.executescript("""
      CREATE TABLE points(id INTEGER PRIMARY KEY,x REAL,y REAL,z REAL,holdout INTEGER,UNIQUE(x,y));
      CREATE VIRTUAL TABLE spatial USING rtree(id,minx,maxx,miny,maxy);
    """)
    count, duplicates, spots = 0, 0, 0
    z_min, z_max = math.inf, -math.inf
    try:
        for source_config in config["sources"]:
            dataset, layer, transform = open_vector(source_config, grid["crs"])
            field = source_config.get("elevation_field")
            from_z = source_config.get("elevation_from_z") is True
            if bool(field) == bool(from_z):
                raise ConfigurationError("Each terrain source needs exactly one of elevation_field or elevation_from_z:true")
            if field and layer.GetLayerDefn().GetFieldIndex(field) < 0:
                raise ConfigurationError(f"Terrain elevation field {field!r} does not exist")
            for feature in layer:
                geometry = feature.GetGeometryRef()
                if geometry is None or geometry.IsEmpty():
                    continue
                kind = ogr.GT_Flatten(geometry.GetGeometryType())
                if kind not in (ogr.wkbPoint, ogr.wkbMultiPoint, ogr.wkbLineString, ogr.wkbMultiLineString):
                    raise ConfigurationError(f"Unsupported terrain geometry {geometry.GetGeometryName()}; select contour/spot layers only")
                if from_z and not geometry.Is3D():
                    raise ConfigurationError("Geometry Z requested on a 2D terrain feature")
                try:
                    attr_z = float(feature.GetField(field)) if field else None
                except (TypeError, ValueError):
                    raise ConfigurationError(f"Missing/non-numeric terrain elevation in feature {feature.GetFID()}") from None
                if attr_z is not None and not math.isfinite(attr_z):
                    raise ConfigurationError("Nonfinite terrain elevation")
                is_spot = kind in (ogr.wkbPoint, ogr.wkbMultiPoint)
                for vertices in _vertices(geometry, int(config.get("max_feature_vertices", 1_000_000))):
                    transformed = []
                    for px, py, pz in vertices:
                        xx, yy, _ = transform.TransformPoint(px, py, 0)
                        transformed.append((xx, yy, pz if from_z else attr_z))
                    for xx, yy, zz in _contour_samples(transformed, spacing, sampling_mode):
                        if not all(map(math.isfinite, (xx, yy, zz))):
                            raise ConfigurationError("Nonfinite transformed terrain sample")
                        # Micrometre coordinate quantisation merges float-transform
                        # noise explicitly; no elevation rounding is performed.
                        xx, yy = round(xx, 6), round(yy, 6)
                        existing = connection.execute("SELECT id,z FROM points WHERE x=? AND y=?", (xx, yy)).fetchone()
                        if existing:
                            if abs(existing[1] - zz) > tolerance:
                                raise ConfigurationError(f"Conflicting duplicate terrain XY ({xx}, {yy}): {existing[1]} versus {zz} m")
                            duplicates += 1
                            continue
                        count += 1
                        if count > max_points:
                            raise ResourceBudgetError(f"Terrain sampling exceeds max_sample_points={max_points}; increase spacing or subset inputs")
                        spots += int(is_spot)
                        holdout = int(is_spot and spots % 5 == 0)
                        connection.execute("INSERT INTO points VALUES(?,?,?,?,?)", (count, xx, yy, zz, holdout))
                        connection.execute("INSERT INTO spatial VALUES(?,?,?,?,?)", (count, xx, xx, yy, yy))
                        z_min, z_max = min(z_min, zz), max(z_max, zz)
                        if count % 10_000 == 0:
                            connection.commit()
                            progress_check()
            dataset = None
        connection.commit()
    finally:
        connection.close()
    if count < 3:
        raise ConfigurationError("At least three non-collinear terrain samples are required")
    return {"sample_count": count, "duplicate_samples_retained_first_within_tolerance": duplicates,
            "spot_count": spots, "sample_spacing_m": spacing, "coordinate_quantization_m": 0.000001,
            "contour_sampling": sampling_mode,
            "sampling_limitations": ("Regular arclength approximates constant-height polylines between samples; "
                                     "original source vertices/elevations remain unchanged" if sampling_mode == "regular_arclength"
                                     else "Every original vertex is retained; dense CAD vertices can exceed nominal sampling density"),
            "elevation_min_m": z_min, "elevation_max_m": z_max}


def _local_points(connection: sqlite3.Connection, bounds: tuple[float, float, float, float],
                  maximum: int, *, exclude_holdout: bool = False) -> np.ndarray:
    xmin, ymin, xmax, ymax = bounds
    sql = ("SELECT p.x,p.y,p.z FROM points p JOIN spatial s ON p.id=s.id "
           "WHERE s.maxx>=? AND s.minx<=? AND s.maxy>=? AND s.miny<=? ")
    if exclude_holdout:
        sql += "AND p.holdout=0 "
    sql += "ORDER BY p.id LIMIT ?"
    rows = connection.execute(sql, (xmin, xmax, ymin, ymax, maximum + 1)).fetchall()
    if len(rows) > maximum:
        raise ResourceBudgetError(f"Terrain tile has more than {maximum} samples; reduce tile size/halo or input sampling density")
    return np.asarray(rows, dtype=np.float64).reshape((-1, 3))


def _linear_supported(points: np.ndarray, queries: np.ndarray, support: float) -> np.ndarray:
    from scipy.spatial import Delaunay, QhullError
    output = np.full(len(queries), np.nan, dtype=np.float64)
    if len(points) < 3:
        return output
    try:
        tri = Delaunay(points[:, :2])
    except QhullError:
        return output
    simplex = tri.find_simplex(queries)
    in_hull = simplex >= 0
    if not in_hull.any():
        return output
    ids = np.flatnonzero(in_hull)
    simplices = simplex[in_hull]
    vertices = points[tri.simplices[simplices]]
    max_edge = np.maximum.reduce([np.linalg.norm(vertices[:, a, :2] - vertices[:, b, :2], axis=1)
                                  for a, b in ((0, 1), (1, 2), (2, 0))])
    accepted = max_edge <= support
    if accepted.any():
        ids, simplices, vertices = ids[accepted], simplices[accepted], vertices[accepted]
        bary = np.einsum("ijk,ik->ij", tri.transform[simplices, :2, :],
                         queries[ids] - tri.transform[simplices, 2, :])
        weights = np.column_stack((bary, 1 - bary.sum(axis=1)))
        output[ids] = np.sum(weights * vertices[:, :, 2], axis=1)
    return output


def _validation_spots(connection: sqlite3.Connection, maximum: int, seed: int
                      ) -> tuple[list[tuple[float, float, float]], dict[str, Any]]:
    """Bounded deterministic spatial strata plus reservoir fill over holdouts.

    Retains at most twice `maximum` rows while streaming the index. Strata cover
    the holdout bounds, so dense urban clusters do not consume every validation
    slot. This is a diagnostic subset, not a probability-weighted error estimate.
    """
    if isinstance(maximum, bool) or not isinstance(maximum, int) or not 0 <= maximum <= 100_000:
        raise ConfigurationError("max_validation_spots must be an integer from 0 to 100000")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ConfigurationError("validation_seed must be a nonnegative integer")
    available, xmin, ymin, xmax, ymax = connection.execute(
        "SELECT count(*),min(x),min(y),max(x),max(y) FROM points WHERE holdout=1").fetchone()
    report = {"available": available, "maximum": maximum, "seed": seed,
              "selection": "spatial strata representatives plus deterministic reservoir fill",
              "interpretation": "diagnostic subset, not a probability-weighted population error estimate"}
    if available == 0 or maximum == 0:
        return [], {**report, "selected": 0}
    nx = max(1, math.isqrt(maximum))
    ny = max(1, maximum // nx)
    rng = np.random.default_rng(seed)
    representatives: dict[tuple[int, int], tuple[int, tuple]] = {}
    reservoir: list[tuple] = []
    rows = connection.execute("SELECT id,x,y,z FROM points WHERE holdout=1 ORDER BY id")
    for number, row in enumerate(rows, 1):
        _, x, y, _ = row
        ix = min(nx - 1, int((x - xmin) / (xmax - xmin) * nx)) if xmax > xmin else 0
        iy = min(ny - 1, int((y - ymin) / (ymax - ymin) * ny)) if ymax > ymin else 0
        count, chosen = representatives.get((ix, iy), (0, row))
        count += 1
        if int(rng.integers(count)) == 0:
            chosen = row
        representatives[(ix, iy)] = count, chosen
        if len(reservoir) < maximum:
            reservoir.append(row)
        else:
            replace = int(rng.integers(number))
            if replace < maximum:
                reservoir[replace] = row
    selected = {chosen[0]: chosen for _, chosen in representatives.values()}
    for row in reservoir:
        if len(selected) >= maximum:
            break
        selected.setdefault(row[0], row)
    result = [tuple(row[1:]) for _, row in sorted(selected.items())]
    return result, {**report, "selected": len(result), "occupied_spatial_strata": len(representatives)}


def _interpolate_tiles(connection: sqlite3.Connection, grid: dict[str, Any], output: Path,
                       tile_size: int, halo: float, support: float, maximum: int,
                       progress_check: Any) -> dict[str, Any]:
    """One TIN per tile, with future-neighbor seam probes in the same batch.

    The retained south probes occupy one raster row plus a single east column.
    Their coordinates and input halos equal the former neighbor-TIN checks.
    """
    result = create_raster(output, grid)
    gt, resolution = grid["transform"], grid["resolution_m"]
    valid_count = 0
    vertical_seams: list[float] = []
    horizontal_seams: list[float] = []
    south_probes: dict[int, np.ndarray] = {}
    east_probe: np.ndarray | None = None
    tiles_completed = 0
    vertical_support_mismatches = horizontal_support_mismatches = 0
    total_tiles = math.ceil(grid["width"] / tile_size) * math.ceil(grid["height"] / tile_size)
    try:
        for x, y, w, h in iter_tiles(grid, tile_size):
            progress_check()
            xmin, ymax = gt[0] + x * resolution, gt[3] - y * resolution
            xmax, ymin = xmin + w * resolution, ymax - h * resolution
            points = _local_points(connection, (xmin - halo, ymin - halo, xmax + halo, ymax + halo), maximum)
            xx = xmin + (np.arange(w) + .5) * resolution
            yy = ymax - (np.arange(h) + .5) * resolution
            xgrid, ygrid = np.meshgrid(xx, yy)
            queries = [np.column_stack((xgrid.ravel(), ygrid.ravel()))]
            east_count = h if x + w < grid["width"] else 0
            south_count = w if y + h < grid["height"] else 0
            if east_count:
                queries.append(np.column_stack((np.full(h, xmax + .5 * resolution), yy)))
            if south_count:
                queries.append(np.column_stack((xx, np.full(w, ymin - .5 * resolution))))
            predicted = _linear_supported(points, np.concatenate(queries), support)
            values = predicted[:w * h].reshape(h, w)
            valid = np.isfinite(values)
            valid_count += int(valid.sum())
            if x > 0 and east_probe is not None:
                vertical_support_mismatches += int(np.count_nonzero(np.isfinite(east_probe) ^ valid[:, 0]))
                both = np.isfinite(east_probe) & valid[:, 0]
                if both.any():
                    vertical_seams.append(float(np.max(np.abs(east_probe[both] - values[both, 0]))))
            if y > 0 and x in south_probes:
                previous = south_probes[x]
                horizontal_support_mismatches += int(np.count_nonzero(np.isfinite(previous) ^ valid[0]))
                both = np.isfinite(previous) & valid[0]
                if both.any():
                    horizontal_seams.append(float(np.max(np.abs(previous[both] - values[0, both]))))
            east_probe = predicted[w * h:w * h + east_count].copy() if east_count else None
            if south_count:
                south_probes[x] = predicted[w * h + east_count:].copy()
            else:
                south_probes.pop(x, None)
            values[~valid] = NODATA
            result.GetRasterBand(1).WriteArray(values.astype(np.float32), x, y)
            tiles_completed += 1
            if tiles_completed % 8 == 0 or tiles_completed == total_tiles:
                _checkpoint(output.with_name("terrain_raster_progress.json"), {
                    "status": "interpolation_not_engine_ready", "tiles_completed": tiles_completed,
                    "total_tiles": total_tiles,
                })
        result.FlushCache()
    finally:
        # Close the TIFF before any expensive heldout validation. FlushCache
        # alone can leave the directory/block offsets invisible to readers.
        result = None
    return {"valid_pixels": valid_count, "tiles_completed": tiles_completed,
            "tin_builds": tiles_completed,
            "seam_check": {"vertical_edges_compared": len(vertical_seams),
                           "horizontal_edges_compared": len(horizontal_seams),
                           "vertical_support_mismatched_points": vertical_support_mismatches,
                           "horizontal_support_mismatched_points": horizontal_support_mismatches,
                           "max_same_point_difference_m": max(vertical_seams + horizontal_seams, default=None),
                           "method": "east/south neighbor probes evaluated in each tile's single TIN"}}


def _checkpoint(path: Path, data: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".writing")
    temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _raster_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _sampled_terrain(config: dict[str, Any], grid: dict[str, Any], output: Path,
                     progress_check: Any) -> dict[str, Any]:
    support = float(config.get("max_triangle_edge_m", 250))
    halo = float(config.get("halo_m", max(500, support * 2)))
    if not (math.isfinite(support) and support > 0 and math.isfinite(halo) and halo >= support):
        raise ConfigurationError("Positive max_triangle_edge_m and halo_m >= max_triangle_edge_m are required")
    tile_size = int(config.get("tile_size", 256))
    maximum = int(config.get("max_points_per_tile", 100_000))
    if not 16 <= tile_size <= 2048 or not 3 <= maximum <= 1_000_000:
        raise ConfigurationError("Terrain tile_size must be 16..2048, max_points_per_tile 3..1000000")
    memory_preflight(maximum * 400 + tile_size * tile_size * 300)
    index_path = output.with_name("terrain_samples.sqlite")
    info_path = output.with_name("terrain_samples.json")
    index_started = time.perf_counter()
    index_reused = index_path.exists() and info_path.exists()
    if index_path.exists() and info_path.exists():
        info = json.loads(info_path.read_text())
        if (info.get("contour_sampling", "preserve_vertices") != config.get("contour_sampling", "preserve_vertices")
                or info["sample_spacing_m"] != float(config.get("sample_spacing_m", max(grid["resolution_m"], 10)))):
            raise ConfigurationError("Existing terrain sample index uses different sampling settings; use a new preparation output directory")
    else:
        index_path.unlink(missing_ok=True)
        info = _build_sample_index(config, grid, index_path, progress_check)
        info_path.write_text(json.dumps(info, indent=2))
    index_seconds = time.perf_counter() - index_started
    raster_checkpoint = output.with_name("terrain_raster_checkpoint.json")
    validation_checkpoint = output.with_name("terrain_validation_checkpoint.json")
    surface_parameters = {key: value for key, value in config.items()
                          if key not in {"max_validation_spots", "validation_seed"}}
    index_stat = index_path.stat()
    identity = {"grid": grid, "parameters": surface_parameters,
                "index_size": index_stat.st_size, "index_mtime_ns": index_stat.st_mtime_ns,
                "interpolation_version": "single-tin-neighbor-probes-v1"}
    raster_key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    connection = sqlite3.connect(index_path)
    raster_reused = False
    try:
        if raster_checkpoint.exists():
            checkpoint = json.loads(raster_checkpoint.read_text())
            if (checkpoint["configuration_key"] != raster_key or not output.exists()
                    or checkpoint["raster_sha256"] != _raster_digest(output)):
                raise ConfigurationError("Terrain raster checkpoint differs from parameters/index/output; use a new output directory")
            raster_statistics = checkpoint["raster_statistics"]
            interpolation_seconds = checkpoint["interpolation_seconds"]
            raster_reused = True
        else:
            interpolation_started = time.perf_counter()
            raster_statistics = _interpolate_tiles(connection, grid, output, tile_size, halo,
                                                    support, maximum, progress_check)
            interpolation_seconds = time.perf_counter() - interpolation_started
            _checkpoint(raster_checkpoint, {
                "status": "closed_raster_complete_validation_pending_not_engine_ready",
                "configuration_key": raster_key, "raster_sha256": _raster_digest(output),
                "raster_statistics": raster_statistics, "interpolation_seconds": interpolation_seconds,
            })
        # A completed, closed TIFF is now durable even if validation is interrupted.
        holdouts, validation_selection = _validation_spots(
            connection, config.get("max_validation_spots", 500), config.get("validation_seed", 0))
        validation_key = hashlib.sha256(json.dumps({"raster_key": raster_key,
            "selection": validation_selection, "points": holdouts}, sort_keys=True).encode()).hexdigest()
        residual_sum, residual_squares, residual_count, unsupported, processed = 0.0, 0.0, 0, 0, 0
        previous_seconds = 0.0
        if validation_checkpoint.exists():
            saved = json.loads(validation_checkpoint.read_text())
            if saved["configuration_key"] == validation_key:
                residual_sum, residual_squares = saved["residual_sum"], saved["residual_squares"]
                residual_count, unsupported, processed = saved["evaluated"], saved["unsupported"], saved["processed"]
                previous_seconds = saved["elapsed_seconds"]
        validation_resumed_at = processed
        validation_started = time.perf_counter()
        for number, (xx, yy, zz) in enumerate(holdouts[processed:], processed + 1):
            progress_check()
            points = _local_points(connection, (xx - halo, yy - halo, xx + halo, yy + halo),
                                   maximum, exclude_holdout=True)
            predicted = _linear_supported(points, np.array([[xx, yy]]), support)[0]
            if math.isfinite(predicted):
                residual = predicted - zz
                residual_sum += residual
                residual_squares += residual * residual
                residual_count += 1
            else:
                unsupported += 1
            processed = number
            if processed % 20 == 0 or processed == len(holdouts):
                _checkpoint(validation_checkpoint, {
                    "status": "validation_complete" if processed == len(holdouts) else "validation_in_progress",
                    "configuration_key": validation_key, "processed": processed,
                    "evaluated": residual_count, "unsupported": unsupported,
                    "residual_sum": residual_sum, "residual_squares": residual_squares,
                    "elapsed_seconds": previous_seconds + time.perf_counter() - validation_started,
                })
        validation_seconds = previous_seconds + time.perf_counter() - validation_started
    finally:
        connection.close()
    return {**info, **raster_statistics, "method": "unconstrained sampled-contour local linear TIN", "halo_m": halo,
            "timings_s": {"sample_index": index_seconds, "interpolation_and_seams": interpolation_seconds,
                          "heldout_validation": validation_seconds}, "sample_index_reused": index_reused,
            "raster_checkpoint_reused": raster_reused, "validation_resumed_at": validation_resumed_at,
            "max_triangle_edge_m": support, "max_points_per_tile": maximum,
            "edge_policy": "NoData outside local convex hull or triangles with any edge exceeding support",
            "heldout_spots": {"strategy": "every fifth unique spot excluded from validation TIN only; bounded spatial subset evaluated",
                              **validation_selection,
                              "evaluated": residual_count, "unsupported": unsupported,
                              "rmse_m": math.sqrt(residual_squares / residual_count) if residual_count else None,
                              "bias_m": residual_sum / residual_count if residual_count else None}}


def prepare_terrain(config: dict[str, Any], grid: dict[str, Any], output: Path,
                    progress_check: Any = lambda: None) -> dict[str, Any]:
    if config["kind"] == "raster":
        return _raster_terrain(config, grid, output)
    if config["kind"] == "samples":
        return _sampled_terrain(config, grid, output, progress_check)
    raise ConfigurationError("terrain.kind must be 'raster' or 'samples'")
