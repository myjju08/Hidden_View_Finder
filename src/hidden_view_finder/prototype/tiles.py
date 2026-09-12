"""Read-only local exploration of validated citywide raster *collections*.

This is a bounded adapter around the existing closed-column reference LOS, not
an override of the dense engine's full-computational-region validation. A local
ray can be supported while the source package's global readiness remains false.
All elevations use the declared terrain vertical reference; buildings remain
estimated maximum-ground-base plus AGL columns. No surface is removed for a
roof target, no missing column is transparent, and no datum conversion occurs.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Sequence

import numpy as np
from osgeo import gdal
from pyproj import CRS, Transformer

from seoul_visibility.reference import EARTH_DIAMETER_M, traversed_cells

METHOD_VERSION = "citywide-closed-column-local-v1"
QUALITY_BITS = {"terrain_valid": 1, "building_coverage_valid": 2,
                "height_estimated": 4, "height_unresolved": 8, "roof_conflict": 16}
PRODUCTS = ("dtm", "surface", "occupancy", "quality", "terrain_quality", "contract_mask")
VERTICAL_REFERENCE = "Incheon mean sea level (NGII national map elevation convention)"
TOLERANCE_M = 1e-6
MAX_METADATA_BYTES = 8 * 1024**2


class TileContractError(ValueError):
    """Input identity, grid, or declared semantics cannot safely be used."""


def _json(path: Path) -> dict:
    if path.stat().st_size > MAX_METADATA_BYTES:
        raise TileContractError("Raster metadata exceeds the bounded read limit")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise TileContractError("Raster metadata must be an object")
    return value


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _signature(path: Path) -> tuple:
    st = path.stat()
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def _relative(root: Path, name: str) -> Path:
    if not isinstance(name, str) or Path(name).is_absolute() or ".." in Path(name).parts:
        raise TileContractError("Raster paths must be safe relative paths")
    path = (root / name).resolve(strict=True)
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise TileContractError("Raster path escaped the published collection")
    return path


def _bounds_intersect(a, b):
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def inspect_uncertainty(buildings_path: str | Path, overlap_report_path: str | Path | None = None,
                        *, coverage_gpkg: str | Path | None = None,
                        buffer_m: float = 5.0, maximum_regions: int = 5000) -> dict:
    """Locate flagged buildings and terrain-overlap dependency areas read-only.

    A flagged original footprint is never removed merely because a reprojected
    geometry happens to pass GEOS. RTree envelopes are conservative Float32
    bounds. Every possible building affected by a terrain-overlap anomaly gets
    its *whole* envelope masked, because maximum-footprint base estimation can
    carry a ground discrepancy to a different roof cell. Envelope false
    positives are intentional; this performs no repairs or raster rebuilding.
    """
    if not math.isfinite(buffer_m) or buffer_m < 5 or not 1 <= maximum_regions <= 10000:
        raise ValueError("Uncertainty buffer must be >=5 m with a bounded region count")
    path = Path(buildings_path).resolve(strict=True)
    connection = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA cache_size=-4096")
    regions: list[dict] = []
    flagged: list[dict] = []
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(buildings)")}
        if not {"fid", "invalid_geometry", "geom", "source", "source_id"} <= columns:
            raise TileContractError("Building source lacks recorded geometry validity or identity")
        try:
            declared_crs=connection.execute("SELECT srs_id FROM gpkg_geometry_columns WHERE table_name='buildings' AND column_name='geom'").fetchone()
        except sqlite3.Error as exc:
            raise TileContractError("Building uncertainty extents have no declared CRS") from exc
        if declared_crs != (5186,):
            raise TileContractError("Building uncertainty extents require inspected EPSG:5186")
        invalid = connection.execute("SELECT b.fid,b.source,b.source_id,r.minx,r.miny,r.maxx,r.maxy "
            "FROM buildings b LEFT JOIN rtree_buildings_geom r ON r.id=b.fid "
            "WHERE b.invalid_geometry<>0").fetchall()
        for fid, source, source_id, *bounds in invalid:
            if any(v is None or not math.isfinite(v) for v in bounds):
                raise TileContractError("An invalid building has no reliable extent; local obstruction scope unavailable")
            row = {"fid": fid, "source": source, "source_id": source_id, "bounds": bounds,
                   "reason": "source_flagged_invalid_building"}
            flagged.append(row)
            # Inspection only: a source-invalid flag is never cleared because
            # floating-point reprojection changes a later validity predicate.
            blob=connection.execute("SELECT geom FROM buildings WHERE fid=?",(fid,)).fetchone()[0]
            try:
                from shapely import from_wkb,is_valid,is_valid_reason
                if bytes(blob[:2]) != b"GP": raise ValueError("Not a GeoPackage geometry")
                envelope=(blob[3]>>1)&7
                offset=8+{0:0,1:32,2:48,3:48,4:64}[envelope]
                geometry=from_wkb(blob[offset:])
                row["stored_geometry_recheck"]={"valid":bool(is_valid(geometry)),
                    "reason":is_valid_reason(geometry),"type":geometry.geom_type,
                    "does_not_clear_source_flag":True}
            except (ValueError,KeyError,TypeError):
                row["stored_geometry_recheck"]={"valid":None,"reason":"unparseable flagged source geometry"}
            regions.append({**row, "bounds": [bounds[0]-buffer_m, bounds[1]-buffer_m,
                                               bounds[2]+buffer_m, bounds[3]+buffer_m]})
        anomaly_cells = set()
        dependent_buildings = {}
        anomaly_identity = None
        if overlap_report_path:
            report_path = Path(overlap_report_path).resolve(strict=True)
            report = _json(report_path)
            if report.get("coordinate_crs") != "EPSG:5186" or report.get("schema_version") != 1:
                raise TileContractError("Unsupported overlap-anomaly report schema")
            anomaly_identity = {"sha256": _sha(report_path), "recipe_sha256": report["recipe_sha256"]}
            for row in report["anomalies"]:
                x, y = (float(v) for v in row["coordinate_epsg5186"])
                if not math.isfinite(x+y):
                    raise TileContractError("Overlap-anomaly coordinate is not finite")
                # Retain even floating-point-scale disagreements; no chosen
                # materiality threshold can silently certify these cells.
                anomaly_cells.add((x, y))
            for x, y in sorted(anomaly_cells):
                bounds = [x-2.5, y-2.5, x+2.5, y+2.5]
                regions.append({"bounds": bounds, "reason": "terrain_overlap_disagreement"})
                for fid, minx, miny, maxx, maxy in connection.execute(
                    "SELECT id,minx,miny,maxx,maxy FROM rtree_buildings_geom "
                    "WHERE minx<=? AND maxx>=? AND miny<=? AND maxy>=?",
                    (bounds[2], bounds[0], bounds[3], bounds[1])):
                    dependent_buildings[fid] = {"fid": fid, "reason": "building_base_overlap_dependency",
                        "bounds": [minx-buffer_m, miny-buffer_m, maxx+buffer_m, maxy+buffer_m]}
            regions.extend(dependent_buildings.values())
        if len(regions) > maximum_regions:
            raise TileContractError("Uncertainty regions exceed the bounded local index limit")
        coverage_report=None
        coverage_path=Path(coverage_gpkg) if coverage_gpkg else path.parent/'coverage.gpkg'
        if coverage_gpkg or coverage_path.exists():
            from shapely import from_wkb
            def parse_geometry(blob):
                if bytes(blob[:2]) != b'GP': raise TileContractError('Coverage layer geometry is not GeoPackage')
                return from_wkb(blob[8+{0:0,1:32,2:48,3:48,4:64}[(blob[3]>>1)&7]:])
            coverage=sqlite3.connect(coverage_path.resolve(strict=True).as_uri()+'?mode=ro&immutable=1',uri=True)
            try:
                for layer in ('requested_extents','building_source_domain'):
                    if coverage.execute('SELECT srs_id FROM gpkg_geometry_columns WHERE table_name=?',(layer,)).fetchone() != (5186,):
                        raise TileContractError('Building coverage geometry has incompatible CRS')
                city=parse_geometry(coverage.execute("SELECT geom FROM requested_extents WHERE source_id='recommendation'").fetchone()[0])
                gap=parse_geometry(coverage.execute("SELECT geom FROM building_source_domain WHERE source_id='outside-selected-row-group-envelopes'").fetchone()[0])
                affected=gap.intersection(city)
                coverage_report={'source_sha256':_sha(coverage_path),'selection_envelope_gap_area_m2':gap.area,
                    'gap_intersection_seoul_area_m2':affected.area,'gap_distance_to_seoul_m':gap.distance(city),
                    'interpretation':'Selection envelopes are distribution evidence, not completeness of real-world buildings',
                    'source_terrain_domain':'Published supported terrain is restricted to Seoul; outside remains NoData'}
                if not affected.is_empty:
                    parts=list(affected.geoms) if hasattr(affected,'geoms') else [affected]
                    for part in parts:
                        if part.is_empty:continue
                        minx,miny,maxx,maxy=part.bounds
                        regions.append({'bounds':[minx-buffer_m,miny-buffer_m,maxx+buffer_m,maxy+buffer_m],
                            'reason':'outside_selected_building_distribution_envelopes'})
            finally:
                coverage.close()
        if len(regions)>maximum_regions:
            raise TileContractError('Coverage uncertainty exceeds the bounded region count')
        return {"regions": regions, "report": {"source": "buildings.gpkg; published input remains unchanged",
            "source_sha256": _sha(path), "crs":"EPSG:5186",
            "source_flagged_invalid_buildings": flagged, "flagged_building_count": len(flagged),
            "buffer_m": buffer_m, "overlap_anomaly_unique_cells": len(anomaly_cells),
            "overlap_dependent_building_count": len(dependent_buildings),
            "overlap_report": anomaly_identity, "region_count": len(regions),
            "building_distribution_coverage":coverage_report,
            "repairs_written": 0, "rasters_modified": False,
            "policy": "Flagged original bounds and all possible footprint-base dependencies remain unknown"}}
    finally:
        connection.close()


@dataclass
class WorkBudget:
    """Request-owned counters; deadline bounds actual cell traversal work."""
    max_rays: int = 10000
    max_cells: int = 2_000_000
    deadline: float | None = None
    rays: int = 0
    cells: int = 0
    stopped_reason: str | None = None

    def __post_init__(self):
        if not 1 <= self.max_rays <= 10000 or not 1 <= self.max_cells <= 20_000_000:
            raise ValueError("Sparse work caps exceed the documented bounds")
        if self.deadline is not None and not math.isfinite(self.deadline):
            raise ValueError("Work deadline must be finite")

    def check(self):
        if self.stopped_reason:
            return False
        if self.deadline is not None and time.monotonic() >= self.deadline:
            self.stopped_reason = "geometry_deadline"
        elif self.cells >= self.max_cells:
            self.stopped_reason = "geometry_cell_limit"
        return self.stopped_reason is None

    def start_ray(self):
        if not self.check():
            return False
        if self.rays >= self.max_rays:
            self.stopped_reason = "geometry_ray_limit"
            return False
        self.rays += 1
        return True

    def as_dict(self):
        return {"rays": self.rays, "cells": self.cells, "max_rays": self.max_rays,
                "max_cells": self.max_cells, "stopped_reason": self.stopped_reason}


class Tiles:
    """Process/thread-owned lazy reader of disjoint aligned prepared tiles.

    Construct this inside the single geometry worker. Metadata and file hashes
    are validated on opening; a bounded LRU holds arrays and dataset handles.
    Read-only handles are never passed across worker boundaries. Missing tiles
    in the rectangular tile index remain unknown, including seam/corner cells.
    """
    def __init__(self, manifest_path: str | Path, invalid_regions=(), *,
                 overlap_anomalies_path: str | Path | None = None,
                 max_handles: int = 12, cache_bytes: int = 32 * 1024**2,
                 verify_hashes: bool = True):
        started = time.perf_counter()
        if not 1 <= max_handles <= 64 or not 1024**2 <= cache_bytes <= 128*1024**2:
            raise ValueError("Tile cache/handle bounds are invalid")
        if verify_hashes is not True:
            raise TileContractError("Local exploration requires validated input hashes; hash verification cannot be bypassed")
        self.path = Path(manifest_path).resolve(strict=True)
        self.root = self.path.parent
        self.manifest = _json(self.path)
        m = self.manifest
        recipe = m.get("recipe", {})
        if recipe.get("processing_version") != "citywide-existing-tin-and-surfaces-v4-window-policy":
            raise TileContractError("Unsupported collection preparation version")
        encoded = json.dumps(recipe, sort_keys=True, separators=(",", ":")).encode()
        recipe_hash = hashlib.sha256(encoded).hexdigest()
        if m.get("recipe_sha256") != recipe_hash:
            raise TileContractError("Collection recipe identity does not match its recipe")
        if m.get("status") not in ("terrain_and_surface_tiles_ready_support_incomplete", "terrain_and_surface_tiles_ready"):
            raise TileContractError("Prepared collection is not a completed publication")
        self.recipe_sha256 = recipe_hash
        self.resolution = float(m["resolution_m"])
        if self.resolution != 5 or recipe.get("resolution_m") != self.resolution:
            raise TileContractError("Local prototype requires the existing 5 m product convention")
        self.tile_metres = float(recipe["tile_metres"])
        self.tile_pixels = int(round(self.tile_metres / self.resolution))
        if self.tile_pixels <= 0 or self.tile_pixels*self.resolution != self.tile_metres:
            raise TileContractError("Tile length is not a whole number of cells")
        self.crs = CRS.from_epsg(5186)
        self.to_xy = Transformer.from_crs(4326, self.crs, always_xy=True)
        self.to_lonlat = Transformer.from_crs(self.crs, 4326, always_xy=True)
        self.vertical_reference = VERTICAL_REFERENCE
        self._thread = threading.get_ident()
        self._pid = os.getpid()
        self._closed = False
        self.max_handles = max_handles
        self.cache_limit = cache_bytes
        self._handles = OrderedDict()
        self._blocks = OrderedDict()
        self._cache_bytes = 0
        self._tiles = {}
        self._signatures = {self.path: _signature(self.path)}
        self._records = []
        self._invalid_regions = []
        self._invalid_by_tile = {}
        self._uncertainty_reasons = [None]
        self._unknown_cells = set()
        self._operation = 0
        self._checked_tile_versions = {}
        self.stats = {"block_reads": 0, "decoded_bytes": 0, "rays": 0, "cells": 0,
                      "maximum_cached_bytes": 0, "maximum_open_handles": 0}
        gdal.UseExceptions()
        gdal.SetConfigOption("GDAL_PAM_ENABLED", "NO")
        gdal.SetCacheMax(32 * 1024**2)
        entries = m.get("tiles", [])
        if not entries or len(entries) > 512 or m.get("completed_tiles") != len(entries):
            raise TileContractError("Tile collection count is missing, inconsistent, or unbounded")
        for entry in entries:
            path = _relative(self.root, entry["path"])
            tile = _json(path)
            self._signatures[path] = _signature(path)
            if tile.get("recipe_sha256") != recipe_hash or tile.get("quality_bits") != QUALITY_BITS:
                raise TileContractError("Tile recipe or quality-bit semantics disagree")
            if tile.get("vertical_reference") != self.vertical_reference or tile.get("vertical_conversion") != "none":
                raise TileContractError("Unsupported/mixed terrain vertical reference")
            if tile.get("surface_processing", {}).get("source_buildings_sha256") != recipe.get("buildings_sha256"):
                raise TileContractError("Obstruction source identity disagrees with preparation")
            grid = tile["grid"]
            if grid.get("id") != entry["id"] or grid.get("crs") != "EPSG:5186":
                raise TileContractError("Tile identity or CRS disagrees")
            if grid.get("width") != self.tile_pixels or grid.get("height") != self.tile_pixels:
                raise TileContractError("Collection tiles must have uniform declared shape")
            bounds = tuple(float(v) for v in grid["bounds"])
            transform = tuple(float(v) for v in grid["transform"])
            expected = (bounds[0], self.resolution, 0., bounds[3], 0., -self.resolution)
            if (transform != expected or bounds[2]-bounds[0] != self.tile_metres or
                bounds[3]-bounds[1] != self.tile_metres or
                any(abs(v/self.tile_metres-round(v/self.tile_metres)) > 1e-9 for v in bounds)):
                raise TileContractError("Tile grid is shifted, rotated, or misaligned")
            key = (round(bounds[0]/self.tile_metres), round(bounds[1]/self.tile_metres))
            if key in self._tiles:
                raise TileContractError("Final raster tiles overlap")
            record = {"id": entry["id"], "metadata_path": path, "bounds": bounds, "transform": transform,
                      "files": {}, "nodata": {}, "key": key}
            for name in PRODUCTS:
                metadata = tile.get("products", {}).get(name)
                if not isinstance(metadata, dict):
                    raise TileContractError("Required tile product is missing")
                raster = _relative(path.parent, metadata["path"])
                before = _signature(raster)
                if before[2] != metadata.get("bytes"):
                    raise TileContractError("Published raster size changed")
                if verify_hashes and _sha(raster) != metadata.get("sha256"):
                    raise TileContractError("Published raster checksum changed")
                self._validate_raster(raster, transform, name)
                if _signature(raster) != before:
                    raise TileContractError("Raster changed during verification")
                record["files"][name] = raster
                self._signatures[raster] = before
                dataset = gdal.OpenEx(str(raster), gdal.OF_RASTER | gdal.OF_READONLY)
                record["nodata"][name] = dataset.GetRasterBand(1).GetNoDataValue()
                dataset = None
            self._tiles[key] = record
            self._records.append(record)
        self.bounds = (min(t["bounds"][0] for t in self._records), min(t["bounds"][1] for t in self._records),
                       max(t["bounds"][2] for t in self._records), max(t["bounds"][3] for t in self._records))
        self.transform = (self.bounds[0], self.resolution, 0., self.bounds[3], 0., -self.resolution)
        self.shape = (round((self.bounds[3]-self.bounds[1])/self.resolution),
                      round((self.bounds[2]-self.bounds[0])/self.resolution))
        for region in invalid_regions:
            bounds = tuple(region["bounds"] if isinstance(region, dict) else region)
            reason = region.get("reason", "unresolved_source_geometry") if isinstance(region, dict) else "unresolved_source_geometry"
            if len(bounds) != 4 or not all(math.isfinite(v) for v in bounds) or bounds[0]>bounds[2] or bounds[1]>bounds[3]:
                raise TileContractError("Unlocalized invalid feature cannot safely be ignored")
            self._invalid_regions.append((bounds, reason))
            if reason not in self._uncertainty_reasons:
                self._uncertainty_reasons.append(reason)
                if len(self._uncertainty_reasons)>256:
                    raise TileContractError("Uncertainty reason index is unbounded")
            for key, record in self._tiles.items():
                if _bounds_intersect(bounds, record["bounds"]):
                    self._invalid_by_tile.setdefault(key, []).append((bounds, reason))
        overlap_hash = None
        if overlap_anomalies_path:
            anomaly_path = Path(overlap_anomalies_path).resolve(strict=True)
            report = _json(anomaly_path)
            if report.get("recipe_sha256") != self.recipe_sha256 or report.get("coordinate_crs") != "EPSG:5186":
                raise TileContractError("Overlap inspection belongs to a different geometry version")
            for anomaly in report["anomalies"]:
                self._unknown_cells.add(self._cell(*anomaly["coordinate_epsg5186"]))
            overlap_hash = _sha(anomaly_path)
        self.metadata = {"method": METHOD_VERSION, "manifest_sha256": _sha(self.path),
            "geometry_version": self.recipe_sha256, "terrain_source_sha256": recipe["source_sha256"],
            "buildings_source_sha256": recipe["buildings_sha256"], "crs": "EPSG:5186",
            "resolution_m": self.resolution, "vertical_reference": self.vertical_reference,
            "quality_bits": QUALITY_BITS, "tile_count": len(entries),
            "source_visibility_ready": m.get("visibility_ready", False),
            "local_exploration_available": True, "whole_panorama_supported": False,
            "curvature_coefficient": 6/7, "earth_diameter_m": EARTH_DIAMETER_M,
            "obstacle_contact_tolerance_m": TOLERANCE_M,
            "maximum_modeled_distance_m": 10000,
            "overlap_anomaly_sha256": overlap_hash, "uncertainty_region_count": len(self._invalid_regions),
            "overlap_unknown_cells": len(self._unknown_cells), "hashes_verified": verify_hashes,
            "open_seconds": time.perf_counter()-started,
            "limitations": ["5 m spacing is not 5 m accuracy", "estimated buildings; vegetation canopy unmodeled",
                "unknown cells dominate a ray, including after a known blocker",
                "partial sparse components do not certify panorama or atmospheric visibility"]}
        from seoul_visibility import reference
        self.metadata["implementation_sha256"]={"adapter":_sha(Path(__file__)),
            "reference":_sha(Path(reference.__file__))}
        self.metadata["evidence_version"] = hashlib.sha256(json.dumps({
            "geometry": self.recipe_sha256, "method": METHOD_VERSION, "overlap": overlap_hash,
            "regions": self._invalid_regions,"implementation":self.metadata["implementation_sha256"]}, sort_keys=True).encode()).hexdigest()

    def _validate_raster(self, path, transform, name):
        dataset = gdal.OpenEx(str(path), gdal.OF_RASTER | gdal.OF_READONLY)
        if dataset is None:
            raise TileContractError("Published raster could not be parsed")
        expected = gdal.GDT_Float32 if name in ("dtm", "surface") else gdal.GDT_Byte
        try:
            if (dataset.RasterCount != 1 or dataset.RasterXSize != self.tile_pixels or
                dataset.RasterYSize != self.tile_pixels or tuple(dataset.GetGeoTransform()) != transform or
                dataset.GetRasterBand(1).DataType != expected or
                not self.crs.equals(CRS.from_wkt(dataset.GetProjection()), ignore_axis_order=True)):
                raise TileContractError("Raster shape, alignment, type, or CRS does not match its tile")
        finally:
            dataset = None

    def _check(self):
        if self._closed or os.getpid() != self._pid or threading.get_ident() != self._thread:
            raise TileContractError("Raster reader is closed or used outside its owning worker")
        if _signature(self.path) != self._signatures[self.path]:
            raise TileContractError("Raster collection manifest changed; reopen and invalidate evidence")
        self._operation += 1

    def _cell(self, x, y):
        return (math.floor((self.bounds[3]-y)/self.resolution),
                math.floor((x-self.bounds[0])/self.resolution))

    def _xy(self, row, col):
        return self.bounds[0]+(col+.5)*self.resolution, self.bounds[3]-(row+.5)*self.resolution

    def _tile_for_cell(self, row, col):
        x, y = self._xy(row, col)
        return self._tiles.get((math.floor(x/self.tile_metres), math.floor(y/self.tile_metres)))

    def _dataset(self, path):
        if _signature(path) != self._signatures[path]:
            raise TileContractError("Published raster changed; reopen and invalidate evidence")
        if path in self._handles:
            self._handles.move_to_end(path)
            return self._handles[path]
        dataset = gdal.OpenEx(str(path), gdal.OF_RASTER | gdal.OF_READONLY)
        self._handles[path] = dataset
        while len(self._handles) > self.max_handles:
            _, old = self._handles.popitem(last=False)
            old = None
        self.stats["maximum_open_handles"] = max(self.stats["maximum_open_handles"], len(self._handles))
        return dataset

    def _block(self, tile, local_row, local_col):
        # 128x128 bounds decoding to 192 KiB for six planes, even for a single
        # near-seam sample. GDAL's separate block cache is capped at 32 MiB.
        yoff, xoff = (local_row//128)*128, (local_col//128)*128
        if self._checked_tile_versions.get(tile["id"]) != self._operation:
            for path in (tile["metadata_path"], *tile["files"].values()):
                if _signature(path) != self._signatures[path]:
                    raise TileContractError("Published tile changed; reopen and invalidate evidence")
            self._checked_tile_versions[tile["id"]] = self._operation
        key = (tile["id"], yoff, xoff)
        if key in self._blocks:
            self._blocks.move_to_end(key)
            return self._blocks[key], local_row-yoff, local_col-xoff
        width, height = min(128, self.tile_pixels-xoff), min(128, self.tile_pixels-yoff)
        arrays = {}
        for name, path in tile["files"].items():
            data = self._dataset(path).GetRasterBand(1).ReadAsArray(xoff, yoff, width, height)
            if data is None:
                raise TileContractError("A required raster window could not be read")
            arrays[name] = data
        uncertainty = np.zeros((height,width),dtype=np.uint8)
        left = tile["bounds"][0]+xoff*self.resolution
        top = tile["bounds"][3]-yoff*self.resolution
        block_bounds = (left,top-height*self.resolution,left+width*self.resolution,top)
        for bounds,reason in self._invalid_by_tile.get(tile["key"], ()):
            if not _bounds_intersect(bounds,block_bounds):
                continue
            c0=max(0,math.ceil((bounds[0]-left)/self.resolution-1))
            c1=min(width-1,math.floor((bounds[2]-left)/self.resolution))
            r0=max(0,math.ceil((top-bounds[3])/self.resolution-1))
            r1=min(height-1,math.floor((top-bounds[1])/self.resolution))
            uncertainty[r0:r1+1,c0:c1+1]=self._uncertainty_reasons.index(reason)
        arrays["_uncertainty"] = uncertainty
        size = sum(array.nbytes for array in arrays.values())
        while self._blocks and self._cache_bytes + size > self.cache_limit:
            _, old = self._blocks.popitem(last=False)
            self._cache_bytes -= sum(a.nbytes for a in old.values())
        self._blocks[key] = arrays
        self._cache_bytes += size
        self.stats["block_reads"] += 1
        self.stats["decoded_bytes"] += size
        self.stats["maximum_cached_bytes"] = max(self.stats["maximum_cached_bytes"], self._cache_bytes)
        return arrays, local_row-yoff, local_col-xoff

    def _value(self, row, col):
        tile = self._tile_for_cell(row, col)
        if tile is None:
            return None, "missing_tile"
        x, y = self._xy(row, col)
        if (row, col) in self._unknown_cells:
            return None, "terrain_overlap_disagreement"
        local_col = int(round((x-tile["bounds"][0])/self.resolution-.5))
        local_row = int(round((tile["bounds"][3]-y)/self.resolution-.5))
        arrays, r, c = self._block(tile, local_row, local_col)
        uncertainty = int(arrays["_uncertainty"][r,c])
        if uncertainty:
            return None, self._uncertainty_reasons[uncertainty]
        dtm, surface = float(arrays["dtm"][r,c]), float(arrays["surface"][r,c])
        occupancy, quality = int(arrays["occupancy"][r,c]), int(arrays["quality"][r,c])
        tq, contract = int(arrays["terrain_quality"][r,c]), int(arrays["contract_mask"][r,c])
        values = {"dtm_m": dtm, "surface_m": surface, "occupancy": occupancy,
                  "quality": quality, "terrain_quality": tq, "contract_mask": contract,
                  "tile_id": tile["id"], "estimated_height": bool(quality & 4)}
        if not math.isfinite(dtm) or dtm == tile["nodata"]["dtm"] or tq != 1 or not (quality & 1):
            return values, "terrain_unsupported"
        if not (contract & 1):
            return values, "outside_requested_support"
        if quality & 2 != 2 or occupancy > 1:
            return values, "obstruction_coverage_unknown"
        if quality & 248 or not math.isfinite(surface) or surface == tile["nodata"]["surface"]:
            return values, "obstruction_height_unknown"
        if surface < dtm-TOLERANCE_M or occupancy == 0 and abs(surface-dtm) > TOLERANCE_M:
            return values, "surface_contract_violation"
        return values, None

    def sample(self, x: float, y: float) -> dict:
        self._check()
        if not math.isfinite(x) or not math.isfinite(y):
            raise ValueError("Sample coordinates must be finite projected metres")
        row, col = self._cell(x,y)
        cx, cy = self._xy(row,col)
        values, reason = self._value(row,col)
        return {"state": "supported" if reason is None else "unknown", "reason": reason,
                "requested_xy": [x,y], "cell_center_xy": [cx,cy],
                "effective_xy": [cx,cy], "cell_center_displacement_m": math.hypot(cx-x,cy-y),
                "row": row, "col": col, **(values or {})}

    def observer(self, x: float, y: float, eye_height_m: float = 1.7) -> dict:
        if not math.isfinite(eye_height_m) or not 0.5 <= eye_height_m <= 3:
            raise ValueError("Eye height must be between 0.5 and 3 m above supported ground")
        result = self.sample(x,y)
        if result["state"] == "supported" and result["occupancy"]:
            result.update(state="excluded", reason="building_occupied_observer")
        elif result["state"] == "supported" and not (result["contract_mask"] & 2):
            result.update(state="excluded", reason="standing_location_outside_seoul")
        if result["state"] == "supported":
            result["xyz"] = [x,y,result["dtm_m"]+eye_height_m]
        result["eye_height_m"] = eye_height_m
        result["effective_xy"] = [x,y]
        result["snapping_displacement_m"] = 0.0
        result["observer_coordinate_method"] = "exact requested XY; ground from containing closed-column cell; no horizontal movement"
        return result

    def target(self, x: float, y: float, *, absolute_elevation_m: float | None = None,
               surface_sample: bool = True) -> dict:
        """Roof/terrain top samples use the actual supported model surface.

        Default elevation is surface contact (not an artificial height lift).
        Arbitrary absolute points below that surface are excluded. The target
        is evaluated at the reported containing-cell centre, matching the
        existing engine's roof-target convention. The observer is never moved.
        """
        result = self.sample(x,y)
        if result["state"] != "supported":
            return result
        z = result["surface_m"] if surface_sample and absolute_elevation_m is None else absolute_elevation_m
        if z is None or not math.isfinite(z) or z < result["surface_m"]-TOLERANCE_M:
            return {**result, "state": "excluded", "reason": "target_below_supported_surface"}
        result["xyz"] = [*result["cell_center_xy"], float(z)]
        result["height_method"] = "supported model surface contact" if surface_sample and absolute_elevation_m is None else "explicit compatible absolute elevation"
        return result

    def roof_boundary_target(self, observer_xy: Sequence[float], roof_cell_xy: Sequence[float]) -> dict:
        """Sample the near-facing *top edge* of an occupied raster column.

        The segment toward the selected roof cell's centre is intersected with
        its closed square footprint. The first intersection is on the actual
        column's top edge at its unmodified surface elevation. There is no
        epsilon height lift, obstacle deletion, facade sampling or use of the
        far roof centre. All other columns, including the same building, remain
        in LOS and can self-occlude this sample. This is a model-column
        silhouette sample, not an actual building facade or surveyed roof.
        """
        return self._column_boundary_target(observer_xy,roof_cell_xy,require_occupied=True)

    def surface_boundary_target(self, observer_xy: Sequence[float], terrain_cell_xy: Sequence[float]) -> dict:
        """Near-facing top edge of supported *unoccupied* mapped terrain.

        This avoids falsely treating an uphill cell-centre endpoint as a
        visible slope behind its own column face. It still models the existing
        constant-height cell convention, not interpolated terrain, tree canopy
        or measured river water. All target/ray quality checks are unchanged.
        """
        return self._column_boundary_target(observer_xy,terrain_cell_xy,require_occupied=False)

    def _column_boundary_target(self, observer_xy, roof_cell_xy, *, require_occupied):
        if len(observer_xy) != 2 or len(roof_cell_xy) != 2:
            raise ValueError("Observer and roof-cell coordinates must be XY pairs")
        ox, oy = map(float, observer_xy)
        if not math.isfinite(ox+oy):
            raise ValueError("Observer coordinates must be finite")
        result = self.sample(*map(float, roof_cell_xy))
        if result["state"] != "supported":
            return result
        if bool(result["occupancy"]) != require_occupied:
            return {**result, "state": "excluded", "reason":
                    "roof_sample_cell_unoccupied" if require_occupied else "terrain_sample_cell_occupied"}
        cx, cy = result["cell_center_xy"]
        half = self.resolution / 2
        t_enter, t_exit = 0., 1.
        for origin, centre in ((ox,cx),(oy,cy)):
            delta = centre-origin
            lower, upper = centre-half, centre+half
            if delta == 0:
                if origin < lower or origin > upper:
                    return {**result,"state":"excluded","reason":"roof_boundary_intersection_unavailable"}
                continue
            a,b = (lower-origin)/delta, (upper-origin)/delta
            t_enter=max(t_enter,min(a,b));t_exit=min(t_exit,max(a,b))
        if t_enter <= 0 or t_enter > t_exit:
            return {**result,"state":"excluded","reason":"observer_inside_roof_column"}
        x,y = ox+t_enter*(cx-ox), oy+t_enter*(cy-oy)
        # Exact projected grid-line values avoid arithmetic roundoff without
        # moving the analytical intersection into or out of a column.
        for edge in (cx-half,cx+half):
            if abs(x-edge) <= 1e-9: x=edge
        for edge in (cy-half,cy+half):
            if abs(y-edge) <= 1e-9: y=edge
        return {**result,"effective_xy":[x,y],"xyz":[x,y,result["surface_m"]],
                "target_sample_cell_xy":[cx,cy],
                "snapping_displacement_m":math.hypot(x-roof_cell_xy[0],y-roof_cell_xy[1]),
                "height_method":"unchanged modeled roof-column top edge" if require_occupied else "unchanged modeled terrain-column top edge",
                "target_geometry_method":"analytic observer-facing intersection with closed raster footprint",
                "claim_scope":"sampled modeled roof silhouette; not whole-building or facade visibility" if require_occupied else
                    "sampled mapped terrain top edge; not measured canopy or water elevation"}

    def ray(self, start_xyz: Sequence[float], end_xyz: Sequence[float], *,
            work: WorkBudget | None = None, deadline: float | None = None,
            curvature_coefficient: float = 6/7) -> dict:
        self._check()
        endpoints = [tuple(float(v) for v in endpoint) for endpoint in (start_xyz,end_xyz)]
        if any(len(p)!=3 or not all(math.isfinite(v) for v in p) for p in endpoints):
            raise ValueError("Ray endpoints must be finite projected XYZ triples")
        if not math.isfinite(curvature_coefficient) or not 0<=curvature_coefficient<=1:
            raise ValueError("Curvature/refraction coefficient must be in [0,1]")
        start,end = endpoints
        distance = math.hypot(end[0]-start[0],end[1]-start[1])
        response = {"state": "unknown", "reason": None, "distance_m": distance,
                    "cells_checked": 0, "tiles_touched": [], "estimated_height_cells": 0,
                    "method": METHOD_VERSION, "geometry_version": self.metadata["evidence_version"],
                    "curvature_coefficient": curvature_coefficient, "maximum_modeled_distance_m": 10000}
        if distance>10000:
            return {**response, "reason": "maximum_modeled_distance_exceeded"}
        work = work if work is not None else WorkBudget(deadline=deadline)
        if deadline is not None and (work.deadline is None or deadline < work.deadline):
            work.deadline = deadline
        if not work.start_ray():
            return {**response, "reason": work.stopped_reason}
        for endpoint in endpoints:
            row,col=self._cell(*endpoint[:2])
            if not (0<=row<self.shape[0] and 0<=col<self.shape[1]):
                return {**response,"reason":"endpoint_outside_collection"}
            value,reason=self._value(row,col)
            if reason:
                return {**response,"reason":reason}
            if endpoint[2] < value["surface_m"]-TOLERANCE_M:
                return {**response,"state":"excluded","reason":"endpoint_below_supported_surface"}
        q=curvature_coefficient*distance*distance/EARTH_DIAMETER_M
        dz=end[2]-start[2]
        stationary=(q-dz)/(2*q) if q else None
        blocked=False
        minimum_clearance=math.inf
        touched=set()
        self.stats["rays"]+=1
        # traversed_cells itself is bounded by <=10 km / 5 m, and creates at
        # most O(crossed rows + columns) intervals. Every exact edge/corner is
        # then checked; no fixed-distance samples can jump a narrow obstacle.
        for row,col,enter,leave in traversed_cells(self.shape,self.transform,start[:2],end[:2]):
            if not work.check():
                response.update(reason=work.stopped_reason)
                break
            work.cells+=1
            response["cells_checked"]+=1
            self.stats["cells"]+=1
            value,reason=self._value(row,col)
            if value:
                touched.add(value["tile_id"])
                response["estimated_height_cells"]+=int(value["estimated_height"])
            if reason:
                response.update(reason=reason)
                break  # Unknown already dominates any known obstruction.
            ts=[enter,leave]
            if stationary is not None and enter<stationary<leave:
                ts.append(stationary)
            clearance=min(start[2]+t*dz-value["surface_m"]-q*t*(1-t) for t in ts)
            minimum_clearance=min(minimum_clearance,clearance)
            blocked |= clearance < -TOLERANCE_M
        else:
            response.update(state="blocked" if blocked else "visible",
                            reason="modeled_obstruction" if blocked else None)
        response["tiles_touched"]=sorted(touched)
        response["minimum_modeled_clearance_m"]=None if not math.isfinite(minimum_clearance) else minimum_clearance
        response["inspected_distance_m"]=distance if response["state"] in ("visible","blocked") else None
        return response

    def horizon(self, observer_xyz: Sequence[float], bearing_deg: float, max_distance_m: float = 10000,
                *, work: WorkBudget | None = None, deadline: float | None = None,
                curvature_coefficient: float = 6/7) -> dict:
        """Maximum supported closed-column angle in one azimuth, not a panorama.

        Each full cell interval is inspected. The apparent slope is
        (surface - observer_z)/distance - k*distance/earth_diameter. Its
        endpoint and interior extrema are evaluated analytically. A missing
        column makes this azimuth unknown, while retaining an explicitly
        partial maximum and distance inspected. Nothing beyond the requested
        range is certified, including the solar disc or atmospheric clarity.
        """
        self._check()
        if (len(observer_xyz)!=3 or not all(math.isfinite(v) for v in observer_xyz) or
            not math.isfinite(bearing_deg) or not math.isfinite(max_distance_m) or
            not 5 <= max_distance_m <= 10000 or not math.isfinite(curvature_coefficient) or
            not 0<=curvature_coefficient<=1):
            raise ValueError("Invalid bounded directional-horizon request")
        x,y,z=map(float,observer_xyz)
        bearing_deg %= 360
        bearing=math.radians(bearing_deg)
        ex,ey=x+math.sin(bearing)*max_distance_m,y+math.cos(bearing)*max_distance_m
        response={"state":"unknown","reason":None,"bearing_deg":bearing_deg,
            "maximum_modeled_distance_m":max_distance_m,"supported_until_m":0.,
            "maximum_angle_deg":None,"partial_maximum_angle_deg":None,"cells_checked":0,
            "method":METHOD_VERSION+"-directional-horizon",
            "geometry_version":self.metadata["evidence_version"],
            "curvature_coefficient":curvature_coefficient,"distant_horizon_verified":False}
        work=work if work is not None else WorkBudget(deadline=deadline)
        if deadline is not None and (work.deadline is None or deadline<work.deadline):
            work.deadline=deadline
        if not work.start_ray():
            return {**response,"reason":work.stopped_reason}
        observer,reason=self._value(*self._cell(x,y))
        if reason:
            return {**response,"reason":reason}
        if observer["occupancy"] or z < observer["surface_m"]-TOLERANCE_M:
            return {**response,"state":"excluded","reason":"unsupported_horizon_observer"}
        # A virtual *metadata-only* grid encloses the full requested ray even
        # when it leaves the actual tile collection. No raster is synthesized;
        # the first absent real cell stops the supported prefix.
        left=math.floor(min(x,ex)/self.resolution)*self.resolution-self.resolution
        top=math.ceil(max(y,ey)/self.resolution)*self.resolution+self.resolution
        cols=math.ceil((max(x,ex)-left)/self.resolution)+1
        rows=math.ceil((top-min(y,ey))/self.resolution)+1
        transform=(left,self.resolution,0.,top,0.,-self.resolution)
        maximum=-math.inf
        self.stats["rays"]+=1
        for row,col,enter,leave in traversed_cells((rows,cols),transform,(x,y),(ex,ey)):
            if not work.check():
                response["reason"]=work.stopped_reason
                break
            work.cells+=1;self.stats["cells"]+=1;response["cells_checked"]+=1
            cx,cy=left+(col+.5)*self.resolution,top-(row+.5)*self.resolution
            value,reason=self._value(*self._cell(cx,cy))
            if reason:
                response["reason"]=reason
                response["supported_until_m"]=min(response["supported_until_m"],enter*max_distance_m)
                break
            h=value["surface_m"]-z
            lo,hi=enter*max_distance_m,leave*max_distance_m
            distances=[d for d in (lo,hi) if d>0]
            if curvature_coefficient and h<0:
                stationary=math.sqrt(-h*EARTH_DIAMETER_M/curvature_coefficient)
                if lo<stationary<hi:distances.append(stationary)
            if distances:
                maximum=max(maximum,max(math.degrees(math.atan2(
                    h-curvature_coefficient*d*d/EARTH_DIAMETER_M,d)) for d in distances))
            response["supported_until_m"]=max(response["supported_until_m"],hi)
        else:
            response["state"]="supported"
            response["supported_until_m"]=max_distance_m
            response["maximum_angle_deg"]=None if not math.isfinite(maximum) else maximum
        response["partial_maximum_angle_deg"]=None if not math.isfinite(maximum) else maximum
        return response

    def close(self):
        if not self._closed:
            self._check()
            self._handles.clear()
            self._blocks.clear()
            self._cache_bytes=0
            self._closed=True

    def __enter__(self):
        return self

    def __exit__(self,*args):
        self.close()
