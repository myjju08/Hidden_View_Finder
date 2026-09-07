#!/usr/bin/env python3
"""Fetch the public, city-only Seoul contour/spot archive without credentials.

The fixed digest makes acquisition reproducible. Changed upstream data requires
new inspection and an explicit source version; it is never silently accepted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
from urllib.request import Request, urlopen
from zipfile import ZipFile
import zlib

CATALOG_URL = "https://data.seoul.go.kr/dataList/OA-22241/F/1/datasetView.do"
DOWNLOAD_URL = "https://datafile.seoul.go.kr/bigfile/iot/inf/nio_download.do?&useCache=false"
POST_BODY = b"infId=OA-22241&seqNo=&seq=2&infSeq=1"
ARCHIVE_NAME = "seoul_contours_2023.zip"
ARCHIVE_BYTES = 45_852_601
ARCHIVE_SHA256 = "4fbe3c7e061b5974e7403ec116855304ed8ae321eebcc0d12c31ca8fb7be30bf"
EXPANDED_BYTES = 78_559_101
VERTICAL_REFERENCE = "Incheon mean sea level (NGII national map elevation convention)"


def _sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _tree_bytes(root: Path) -> int:
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file()) if root.exists() else 0


def _preflight(data_root: Path, additional: int) -> None:
    used = _tree_bytes(data_root)
    free = shutil.disk_usage(data_root).free
    if used + additional > 20 * 1024**3:
        raise RuntimeError("Acquisition exceeds 20 GiB project-data budget")
    if free - additional < 8 * 1024**3:
        raise RuntimeError("Acquisition would breach 8 GiB free-space floor")
    if additional > 4 * 1024**3:
        raise RuntimeError("Acquisition exceeds 4 GiB temporary-artifact cap")


def acquire(destination: Path, data_root: Path) -> dict:
    destination, data_root = destination.resolve(), data_root.resolve()
    if not destination.is_relative_to(data_root):
        raise ValueError("Destination must be inside the accounted data root")
    data_root.mkdir(parents=True, exist_ok=True)
    _preflight(data_root, ARCHIVE_BYTES + EXPANDED_BYTES + 72_000_000)
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / ARCHIVE_NAME
    if not archive.exists():
        temporary = destination / (ARCHIVE_NAME + ".terrain-download.writing")
        # Exclusive creation avoids overwriting pre-existing source/unrelated files.
        owns_temporary = False
        try:
            with temporary.open("xb") as output:
                owns_temporary = True
                request = Request(DOWNLOAD_URL, data=POST_BODY, headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "SeoulVisibility/0.1 public-data acquisition",
                    "Referer": CATALOG_URL,
                })
                with urlopen(request, timeout=60) as response:
                    count = 0
                    while block := response.read(1024 * 1024):
                        count += len(block)
                        if count > ARCHIVE_BYTES:
                            raise RuntimeError("Upstream archive grew; inspect new source before accepting")
                        output.write(block)
                        _preflight(data_root, 0)
            if temporary.stat().st_size != ARCHIVE_BYTES or _sha256(temporary) != ARCHIVE_SHA256:
                raise RuntimeError("Archive does not match verified Seoul source version")
            os.rename(temporary, archive)
        except Exception:
            # Only this invocation's exclusive-created temporary file is owned.
            if owns_temporary and temporary.exists():
                temporary.unlink()
            raise
    if archive.stat().st_size != ARCHIVE_BYTES or _sha256(archive) != ARCHIVE_SHA256:
        raise RuntimeError(f"Existing {archive} differs from verified source; preserved unchanged")
    source = destination / "source"
    with ZipFile(archive) as zipped:
        if sum(entry.file_size for entry in zipped.infolist()) != EXPANDED_BYTES:
            raise RuntimeError("Unexpected archive expansion size")
        for entry in zipped.infolist():
            relative = Path(entry.filename)
            if relative.is_absolute() or ".." in relative.parts:
                raise RuntimeError("Unsafe ZIP member")
            target = source / relative
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                crc = 0
                with target.open("rb") as handle:
                    while block := handle.read(1024 * 1024):
                        crc = zlib.crc32(block, crc)
                if target.stat().st_size != entry.file_size or crc != entry.CRC:
                    raise RuntimeError(f"Existing source differs from ZIP, preserved: {target}")
                continue
            temporary = target.with_name(target.name + ".terrain-extract.writing")
            owns_temporary = False
            try:
                with zipped.open(entry) as stream, temporary.open("xb") as output:
                    owns_temporary = True
                    shutil.copyfileobj(stream, output, 1024 * 1024)
                os.rename(temporary, target)
            except Exception:
                if owns_temporary and temporary.exists():
                    temporary.unlink()
                raise
            _preflight(data_root, 0)
    return {
        "archive": str(archive), "sha256": ARCHIVE_SHA256,
        "catalog_url": CATALOG_URL, "download_url": DOWNLOAD_URL,
        "license": "KOGL Type 1: attribution, commercial use and modifications permitted",
        "provider": "Seoul Metropolitan Government; catalog identifies NGII 2023 topographic maps",
        "catalog_file_updated": "2025-03-20", "source_year_as_published": "2023",
        "xml_export_date": "2025-03-18", "map_scale_archive_label": "1:5000",
        "vertical_reference": VERTICAL_REFERENCE,
        "vertical_reference_evidence": "https://www.ngii.go.kr/child/content.do?sq=251",
        "vertical_reference_caveat": "Source-family convention linked by catalog provenance; no embedded vertical CRS or realization epoch. No GPS ellipsoidal elevation mixing or vertical conversion.",
        "sources": [
            {"path": str(source / "등고선 5000/N3L_F001.shp"), "layer": "N3L_F001",
             "crs": "EPSG:5174", "encoding": "CP949", "elevation_field": "CONT",
             "units": "m", "source_date": "2023", "feature_count": 8570},
            {"path": str(source / "표고 5000/N3P_F002.shp"), "layer": "N3P_F002",
             "crs": "EPSG:5174", "encoding": "CP949", "elevation_field": "NUME",
             "units": "m", "source_date": "2023", "feature_count": 45870},
        ],
        "coverage_caveat": "Feature bounds are not guaranteed survey coverage; unsupported interpolation and windows outside valid data must fail.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=Path("data/acquisition/terrain_research"))
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = acquire(args.destination, args.data_root)
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
