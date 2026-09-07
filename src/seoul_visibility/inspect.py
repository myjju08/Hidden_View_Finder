"""Streaming inventories of local vector/raster inputs; never infer a schema."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import math
from pathlib import Path
from typing import Any, Iterable

from osgeo import gdal, ogr

from .errors import ConfigurationError

gdal.UseExceptions()
ogr.UseExceptions()

_SIDECARS = {".shp", ".shx", ".dbf", ".prj", ".cpg", ".qix", ".sbn", ".sbx"}


def source_files(path: str | Path) -> list[Path]:
    """Include case-insensitive SHP sidecars in inventory and fingerprints."""
    path = Path(path).resolve()
    if not path.is_file():
        raise ConfigurationError(f"Source is not a file: {path}")
    if path.suffix.lower() != ".shp":
        files = [path]
        if path.suffix.lower() in {".tif", ".tiff", ".vrt"}:
            dataset = gdal.OpenEx(str(path), gdal.OF_RASTER | gdal.OF_READONLY)
            for dependency in dataset.GetFileList() or []:
                child = Path(dependency).resolve()
                if not child.is_file():
                    raise ConfigurationError(f"Raster dependency must be an existing local file: {dependency}")
                files.append(child)
        for suffix in (".aux.xml", ".ovr", ".msk", "-wal"):
            child = Path(str(path) + suffix)
            if child.is_file():
                files.append(child)
        return sorted(set(files))
    return sorted(p for p in path.parent.iterdir()
                  if p.stem.lower() == path.stem.lower() and p.suffix.lower() in _SIDECARS)


def fingerprint(path: str | Path, *, hash_content: bool = True) -> dict[str, Any]:
    records = []
    for item in source_files(path):
        stat = item.stat()
        row: dict[str, Any] = {"path": str(item), "size_bytes": stat.st_size,
                               "modified_ns": stat.st_mtime_ns}
        if hash_content:
            digest = hashlib.sha256()
            with item.open("rb") as stream:
                for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                    digest.update(chunk)
            row["sha256"] = digest.hexdigest()
        records.append(row)
    return {"path": str(Path(path).resolve()), "files": records}


def _geometry_z(geom: ogr.Geometry) -> Iterable[float]:
    if geom.GetGeometryCount():
        for i in range(geom.GetGeometryCount()):
            yield from _geometry_z(geom.GetGeometryRef(i))
    elif geom.Is3D():
        for i in range(geom.GetPointCount()):
            yield float(geom.GetZ(i))


def _crs(srs: Any) -> dict[str, Any] | None:
    if srs is None:
        return None
    return {"wkt": srs.ExportToWkt(), "authority": srs.GetAuthorityName(None),
            "code": srs.GetAuthorityCode(None), "linear_units": srs.GetLinearUnitsName(),
            "vertical_reference": "not verified; supply documented reference in configuration"}


def _suggest(name: str) -> list[str]:
    low = name.casefold()
    suggestions = []
    if any(t in low for t in ("elev", "contour", "표고", "등고", "해발")):
        suggestions.append("terrain elevation candidate")
    if any(t in low for t in ("height", "높이", "hght")):
        suggestions.append("height candidate (check AGL versus absolute)")
    if any(t in low for t in ("floor", "층수", "grnd_flr", "지상층")):
        suggestions.append("floor-count candidate (verify above-ground only)")
    return suggestions


def _inspect_vector(path: Path, encoding: str | None = None) -> dict[str, Any]:
    options = [f"ENCODING={encoding}"] if encoding and path.suffix.lower() == ".shp" else []
    dataset = gdal.OpenEx(str(path), gdal.OF_VECTOR | gdal.OF_READONLY, open_options=options)
    if dataset is None:
        raise ConfigurationError(f"Cannot open vector source: {path}")
    layers = []
    for li in range(dataset.GetLayerCount()):
        layer = dataset.GetLayerByIndex(li)
        definition = layer.GetLayerDefn()
        fields: dict[str, dict[str, Any]] = {}
        for i in range(definition.GetFieldCount()):
            field = definition.GetFieldDefn(i)
            fields[field.GetName()] = {"type": field.GetTypeName(), "missing": 0,
                                      "nonfinite": 0, "zero": 0, "min": None, "max": None,
                                      "suggestions": _suggest(field.GetName())}
        count, empty = 0, 0
        geometry_types: dict[str, int] = {}
        z_count, z_nonzero, z_min, z_max = 0, 0, math.inf, -math.inf
        entity_layers: dict[str, dict[str, Any]] = {}
        layer.ResetReading()
        for feature in layer:
            count += 1
            geom = feature.GetGeometryRef()
            if geom is None or geom.IsEmpty():
                empty += 1
                kind, zz = "EMPTY", iter(())
            else:
                kind = geom.GetGeometryName()
                zz = _geometry_z(geom)
            geometry_types[kind] = geometry_types.get(kind, 0) + 1
            feature_nonzero_z = 0
            for value in zz:
                if math.isfinite(value):
                    z_count += 1
                    z_nonzero += int(value != 0)
                    feature_nonzero_z += int(value != 0)
                    z_min, z_max = min(z_min, value), max(z_max, value)
            for name, summary in fields.items():
                value = feature.GetField(name)
                if value is None or value == "":
                    summary["missing"] += 1
                elif isinstance(value, (float, int)):
                    if not math.isfinite(value):
                        summary["nonfinite"] += 1
                    else:
                        summary["zero"] += int(value == 0)
                        summary["min"] = value if summary["min"] is None else min(summary["min"], value)
                        summary["max"] = value if summary["max"] is None else max(summary["max"], value)
            # DXF's OGR layer is commonly "entities"; the CAD Layer attribute is
            # the actual thematic layer and must be inspected separately.
            if path.suffix.lower() == ".dxf":
                cad_layer = str(feature.GetField("Layer")) if "Layer" in fields else "unknown"
                entry = entity_layers.setdefault(cad_layer, {"features": 0, "geometry_types": {},
                                                             "z_nonzero_vertices": 0})
                entry["features"] += 1
                entry["geometry_types"][kind] = entry["geometry_types"].get(kind, 0) + 1
                entry["z_nonzero_vertices"] += feature_nonzero_z
        extent = layer.GetExtent() if count else None
        layers.append({"name": layer.GetName(), "feature_count": count,
                       "bounds": [extent[0], extent[2], extent[1], extent[3]] if extent else None,
                       "crs": _crs(layer.GetSpatialRef()), "geometry_types": geometry_types,
                       "empty_geometries": empty, "fields": fields,
                       "geometry_z": {"finite_vertices": z_count, "nonzero_vertices": z_nonzero,
                                      "min": z_min if z_count else None, "max": z_max if z_count else None},
                       "dxf_entity_layers": entity_layers or None,
                       "encoding_metadata": layer.GetMetadata("SHAPEFILE") or {}})
    return {"kind": "vector", "driver": dataset.GetDriver().ShortName, "layers": layers}


def _inspect_raster(path: Path) -> dict[str, Any]:
    dataset = gdal.OpenEx(str(path), gdal.OF_RASTER | gdal.OF_READONLY)
    if dataset is None:
        raise ConfigurationError(f"Cannot open raster source: {path}")
    gt = dataset.GetGeoTransform()
    corners = [gdal.ApplyGeoTransform(gt, x, y) for x, y in
               ((0, 0), (dataset.RasterXSize, 0), (0, dataset.RasterYSize),
                (dataset.RasterXSize, dataset.RasterYSize))]
    bands = []
    import numpy as np
    for i in range(1, dataset.RasterCount + 1):
        band = dataset.GetRasterBand(i)
        nodata = band.GetNoDataValue()
        valid_count, missing_count, low, high = 0, 0, math.inf, -math.inf
        for y in range(0, dataset.RasterYSize, 512):
            for x in range(0, dataset.RasterXSize, 512):
                a = band.ReadAsArray(x, y, min(512, dataset.RasterXSize - x),
                                     min(512, dataset.RasterYSize - y))
                valid = np.isfinite(a)
                if nodata is not None:
                    valid &= a != nodata
                if valid.any():
                    low, high = min(low, float(a[valid].min())), max(high, float(a[valid].max()))
                valid_count += int(valid.sum())
                missing_count += int(a.size - valid.sum())
        bands.append({"band": i, "dtype": gdal.GetDataTypeName(band.DataType), "nodata": nodata,
                      "unit": band.GetUnitType() or None, "scale": band.GetScale(),
                      "offset": band.GetOffset(), "valid_pixels": valid_count,
                      "missing_pixels": missing_count,
                      "min": low if valid_count else None, "max": high if valid_count else None})
    return {"kind": "raster", "driver": dataset.GetDriver().ShortName,
            "width": dataset.RasterXSize, "height": dataset.RasterYSize,
            "transform": list(gt), "bounds": [min(p[0] for p in corners), min(p[1] for p in corners),
                                                max(p[0] for p in corners), max(p[1] for p in corners)],
            "crs": _crs(dataset.GetSpatialRef()), "bands": bands,
            "metadata": dataset.GetMetadata()}


def inspect_paths(paths: Iterable[str | Path], encoding: str | None = None) -> dict[str, Any]:
    """Fully scan attributes/validity in bounded blocks; dates are never invented."""
    result = []
    for raw in paths:
        path = Path(raw).resolve()
        if path.is_dir():
            items = sorted(p for p in path.rglob("*") if p.suffix.lower() in
                           {".shp", ".dxf", ".gpkg", ".geojson", ".tif", ".tiff", ".vrt"})
        else:
            items = [path]
        for item in items:
            stat = fingerprint(item, hash_content=False)
            raster = item.suffix.lower() in {".tif", ".tiff", ".vrt"}
            details = _inspect_raster(item) if raster else _inspect_vector(item, encoding)
            cpg = next((p for p in source_files(item) if p.suffix.lower() == ".cpg"), None)
            result.append({**stat, **details,
                           "encoding_cpg": cpg.read_text(errors="replace").strip() if cpg else None,
                           "encoding_override": encoding,
                           "filesystem_modified_utc": datetime.fromtimestamp(item.stat().st_mtime,
                                                                               timezone.utc).isoformat(),
                           "source_date": None,
                           "notes": ["Filesystem date is not survey/publication date.",
                                     "Verify vertical datum, height units, field semantics and coverage before prepare."]})
    return {"sources": result, "schema_version": 1,
            "mapping_policy": "Suggestions only; configure every ambiguous field explicitly. "
                              "For Korean SHP without a correct CPG, specify encoding (e.g. CP949) in source config."}
