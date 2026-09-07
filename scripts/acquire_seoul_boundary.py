#!/usr/bin/env python3
"""Fetch one small Seoul boundary, preserving original bytes and provenance.

Default: cached OSM relation 2297418 (ODbL). Optional: archived official 2014
district boundary, whose invalid geometry and restricted license are reported.
No geocoding service is used by the visibility engine.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import urllib.parse
import urllib.request
import zipfile

from osgeo import ogr

from seoul_visibility.resources import preflight


ARCHIVE = "TL_SCCO_SIG_W_SHP.zip"
EXPECTED_SHA256 = 'cdd53ee90cccf2cb676d50b9ec23507ea59c780b35826faa30825f99bdbb721b'
ENDPOINT = "https://data.seoul.go.kr/dataList/mapFileDownload.do"
CATALOG = "https://data.seoul.go.kr/dataList/OA-11677/S/1/datasetView.do"
FORM = {"infId": "", "seq": "2", "filePath": "openDATA/data/FILE_11/OA-11677",
        "fileName": ARCHIVE, "infSeq": "2", "domainId": "seoul"}
LIMIT = 50 * 1024**2
OSM_URL = ("https://nominatim.openstreetmap.org/search?city=Seoul&country=South+Korea"
           "&format=json&polygon_geojson=1&limit=1")
OSM_SHA256 = "8be9d9f7d12289fe566ef39ea15de6c9cdc20d844dabd3b3de97e114d99cc8f0"


def acquire_osm(output: Path, budget: dict) -> None:
    """One fixed city request at most, cached permanently; no generic lookup API.

    https://operations.osmfoundation.org/policies/nominatim/: identify the
    application, no more than one request/second, cache results, attribution.
    """
    from shapely.geometry import shape
    from shapely.ops import transform
    from pyproj import Transformer

    raw = output / "seoul_nominatim_response_20260907.json"
    downloaded = not raw.exists()
    if downloaded:
        request = urllib.request.Request(OSM_URL, headers={
            "User-Agent": "Seoul-point-visibility-research/0.1 (local technical validation)"})
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read(LIMIT + 1)
        if len(data) > LIMIT:
            raise ValueError("Seoul boundary exceeds the download cap")
        if hashlib.sha256(data).hexdigest() != OSM_SHA256:
            raise ValueError("OSM response changed; inspect and version the new snapshot before accepting it")
        raw.write_bytes(data)
    data = raw.read_bytes()
    if hashlib.sha256(data).hexdigest() != OSM_SHA256:
        raise ValueError("Cached OSM response differs from the inspected fingerprint")
    features = json.loads(data)
    if len(features) != 1 or features[0].get("osm_id") != 2297418:
        raise ValueError("Response does not identify Seoul relation 2297418")
    feature = features[0]
    geometry = shape(feature["geojson"])
    if geometry.geom_type not in {"Polygon", "MultiPolygon"} or not geometry.is_valid:
        raise ValueError("OSM Seoul polygon is invalid")
    destination = output / "seoul_boundary_osm_20260907.geojson"
    collection = {"type": "FeatureCollection", "name": "seoul_osm_boundary_20260907", "features": [
        {"type": "Feature", "properties": {k: v for k, v in feature.items() if k != "geojson"},
         "geometry": feature["geojson"]}]}
    encoded = json.dumps(collection, ensure_ascii=False).encode("utf8")
    if destination.exists() and destination.read_bytes() != encoded:
        raise ValueError("Existing derived boundary differs; refusing to overwrite")
    if not destination.exists():
        destination.write_bytes(encoded)
    report = {"dataset_id": "osm-relation-2297418", "title": "Seoul administrative boundary",
              "source_url": "https://www.openstreetmap.org/relation/2297418", "download_url": OSM_URL,
              "accessed_utc": "2026-09-07", "checked_utc": datetime.now(timezone.utc).isoformat(),
              "downloaded_this_run": downloaded, "source_date": "OSM snapshot accessed 2026-09-07; per-node edit dates not supplied",
              "raw_file": str(raw), "raw_bytes": len(data), "raw_sha256": OSM_SHA256,
              "output_file": str(destination), "output_sha256": hashlib.sha256(encoded).hexdigest(),
              "license": "ODbL 1.0", "attribution": "© OpenStreetMap contributors",
              "license_url": "https://www.openstreetmap.org/copyright",
              "api_policy": "https://operations.osmfoundation.org/policies/nominatim/",
              "api_use": "One fixed-city request, cached; not a generic lookup service or runtime dependency",
              "crs": "EPSG:4326", "geometry_type": geometry.geom_type, "valid": True,
              "bounds_lon_lat": geometry.bounds,
              "area_m2_epsg5186": transform(Transformer.from_crs(4326, 5186, always_xy=True).transform, geometry).area,
              "transform": "Original polygon coordinate arrays copied without simplification into FeatureCollection",
              "limitations": ["OSM administrative boundary is not a certified survey",
                              "Boundary does not certify terrain/building inventory coverage or public access"],
              "storage_preflight": budget}
    (output / "boundary_osm_provenance.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/acquisition/boundary"))
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    parser.add_argument("--source", choices=["osm", "official"], default="osm")
    args = parser.parse_args()
    budget = preflight(args.data_root, additional_bytes=LIMIT, temporary_bytes=LIMIT)
    args.output.mkdir(parents=True, exist_ok=True)
    if args.source == "osm":
        acquire_osm(args.output, budget)
        return
    archive = args.output / ARCHIVE
    downloaded = not archive.exists()
    if downloaded:
        temporary = archive.with_suffix(".zip.part")
        if temporary.exists():
            raise FileExistsError(f"Refusing to overwrite an existing temporary file: {temporary}")
        request = urllib.request.Request(ENDPOINT, data=urllib.parse.urlencode(FORM).encode(),
                                         headers={"Referer": CATALOG})
        try:
            with urllib.request.urlopen(request, timeout=60) as response, temporary.open("xb") as target:
                count = 0
                while block := response.read(1024**2):
                    count += len(block)
                    if count > LIMIT:
                        raise ValueError("Boundary download exceeded the 50 MiB cap")
                    target.write(block)
            if not zipfile.is_zipfile(temporary):
                raise ValueError("Portal response was not a ZIP archive; original endpoint may have changed")
            digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
            if digest != EXPECTED_SHA256:
                raise ValueError(f"Source archive changed: {digest}; inspect before updating the pinned fingerprint")
            temporary.replace(archive)
        finally:
            temporary.unlink(missing_ok=True)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != EXPECTED_SHA256:
        raise ValueError("Existing archive does not match the inspected fingerprint")
    output = args.output / "official_2014"
    output.mkdir(exist_ok=True)
    allowed = {"TL_SCCO_SIG_W" + extension for extension in (".shp", ".shx", ".dbf", ".prj")}
    with zipfile.ZipFile(archive) as source:
        if set(source.namelist()) != allowed or sum(x.file_size for x in source.infolist()) > LIMIT:
            raise ValueError("Unexpected boundary ZIP members or inflated size")
        for name in sorted(allowed):
            destination = output / name
            data = source.read(name)
            if destination.exists():
                if destination.read_bytes() != data:
                    raise ValueError(f"Preserved source sidecar differs: {destination}")
            else:
                destination.write_bytes(data)
    ogr.UseExceptions()
    dataset = ogr.Open(str(output / "TL_SCCO_SIG_W.shp"))
    layer = dataset.GetLayer(0)
    layer.GetSpatialRef().AutoIdentifyEPSG()
    if layer.GetFeatureCount() != 25 or layer.GetSpatialRef().GetAuthorityCode(None) != "4326":
        raise ValueError("Boundary count or declared CRS changed")
    invalid = [feature.GetFID() for feature in layer if not feature.GetGeometryRef().IsValid()]
    report = {"dataset_id": "OA-11677", "title": "서울시 행정구역 시군구 정보 (좌표계: WGS1984)",
              "catalog_url": CATALOG, "download_url": ENDPOINT, "download_method": "POST",
              "download_form": FORM, "source_date": "2014-10-15", "portal_service_ended": "2021-07-16",
              "accessed_utc": datetime.now(timezone.utc).isoformat(), "downloaded_this_run": downloaded,
              "archive": str(archive), "archive_bytes": archive.stat().st_size, "sha256": digest,
              "crs": "EPSG:4326", "features": 25, "extent_ogr_order": layer.GetExtent(),
              "invalid_geometry_fids": invalid, "status": "inspection_only" if invalid else "inspected",
              "encoding_observed": "UTF-8 Korean names readable; original archive has no CPG",
              "license_portal": "KOGL Type 3: attribution + no derivatives",
              "limitations": ["Historical 2014 district boundary, not a current 2026 legal survey",
                              "Use only as dated output/coverage mask; does not establish terrain/building survey completeness"],
              "storage_preflight": budget}
    provenance = args.output / "boundary_provenance.json"
    provenance.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
