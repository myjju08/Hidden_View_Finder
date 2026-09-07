#!/usr/bin/env python3
"""Reproduce the inspected central-Seoul screening product from acquired files.

Run acquisition/subset scripts first. No network I/O occurs in this script or
in visibility queries. The target is a hypothetical explicit point, not a claim
about a surveyed landmark's height. Existing inputs and products are preserved.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import resource
import sys
import time

from osgeo import gdal
from pyproj import Transformer

from seoul_visibility.prepare import plan, prepare
from seoul_visibility.resources import preflight
from acquire_terrain import VERTICAL_REFERENCE

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data'
FULL = [192500, 547500, 204000, 559000]
PILOT = [195500, 550500, 200500, 555500]


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # These deterministic generated records may be reused only identically.
    body = json.dumps(value, ensure_ascii=False, indent=2) + '\n'
    if path.exists():
        if path.read_text() != body:
            raise FileExistsError(f'Existing differing record preserved: {path}')
        return
    with path.open('x') as stream:
        stream.write(body)


def subset_buildings() -> Path:
    source = DATA / 'acquisition/buildings/gba_seoul_2025.gpkg'
    target = DATA / 'seoul/inputs/buildings_local.gpkg'
    record = target.with_suffix('.provenance.json')
    source_hash = digest(source)
    if target.exists():
        metadata = json.loads(record.read_text())
        if metadata['source_sha256'] != source_hash or metadata['output_sha256'] != digest(target):
            raise ValueError('Building subset fingerprint mismatch; existing files preserved')
        return target
    # A rectangular selection is deliberately wider than the work grid. Select
    # full footprints, without clipping geometry or losing terrain-base extent.
    selection = [FULL[0]-1000, FULL[1]-1000, FULL[2]+1000, FULL[3]+1000]
    lonlat = Transformer.from_crs(5186, 4326, always_xy=True).transform_bounds(*selection, densify_pts=21)
    preflight(DATA, additional_bytes=180_000_000, temporary_bytes=90_000_000)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name('buildings_local.writing.gpkg')
    if temporary.exists():
        raise FileExistsError(f'Existing partial artifact preserved: {temporary}')
    start = time.perf_counter()
    gdal.UseExceptions()
    dataset = gdal.VectorTranslate(str(temporary), str(source), options=gdal.VectorTranslateOptions(
        format='GPKG', dstSRS='EPSG:5186', spatFilter=lonlat,
        layerName='buildings', geometryType='PROMOTE_TO_MULTI', dim='XY',
        layerCreationOptions=['SPATIAL_INDEX=YES'], transactionSize=1000))
    count = dataset.GetLayer(0).GetFeatureCount()
    dataset = None
    os.replace(temporary, target)
    write_json(record, {'source': str(source), 'source_sha256': source_hash,
        'output_sha256': digest(target), 'features': count,
        'selection_bounds_epsg5186': selection, 'actual_selection_bbox_wgs84': lonlat,
        'geometry_policy': 'full intersecting footprints retained; no polygon clipping',
        'crs': 'EPSG:5186', 'height_policy': 'original estimated AGL height_m unchanged',
        'elapsed_s': time.perf_counter()-start})
    return target


def make_config(area: str, base_method: str) -> dict:
    bounds = FULL if area == 'central' else PILOT
    terrain_directory = 'full-terrain-500m' if area == 'central' else 'pilot-terrain-regular'
    dtm = DATA / 'seoul' / terrain_directory / 'dtm.tif'
    validation_path = dtm.parent / 'terrain_validation.json'
    validation = json.loads(validation_path.read_text())
    boundary = DATA / 'acquisition/boundary/seoul_boundary_osm_20260907.geojson'
    output = DATA / 'seoul/processed' / f'{area}-gba-{base_method}'
    buildings = {
        'path': str(subset_buildings()), 'layer': 'buildings', 'height_field': 'height_m',
        'height_is_agl': True, 'height_is_estimated': True,
        'height_estimation_method': 'Published GBA ML-derived AGL heights, primarily 2019 imagery (2018 fallback); release 2025',
        'units': 'm', 'vertical_reference': VERTICAL_REFERENCE,
        # Actual source selection surrounds this rectangle by 1 km. It is a
        # dataset domain declaration, not a completeness or present-day claim.
        'coverage_bounds': FULL, 'base_estimation_method': base_method,
        'invalid_geometry': 'repair', 'source_date': '2018/2019 heights; 2025 release',
    }
    limitations = [
        'REAL INPUTS / APPROXIMATE: 2023 contours plus estimated building heights; not a current measured-height survey.',
        'GBA heights mostly use 2019 imagery with 2018 fallback; footprints have mixed vintages. Omitted buildings and height errors can cause false visibility.',
        'Sub-metre positive GBA heights are retained as supplied and flagged estimated; no claim that they are plausible measured heights.',
        'NGII/Incheon mean-sea-level height convention is documented via the source agency; SHP has no embedded vertical CRS. No vertical-datum conversion performed.',
        'Sampled contour TIN is unconstrained and approximate; interpolation support and held-out checks are recorded separately. 5 m spacing is not 5 m physical accuracy.',
        'Only the prepared central Seoul domain is supported. Complete computational windows are required; no surrounding missing terrain is assumed absent.',
        'GBA height estimates and some footprints have CC BY-NC 4.0 terms; OSM/Microsoft footprints have ODbL terms. Retain source-specific attribution and licences.',
    ]
    if base_method == 'maximum':
        buildings['base_estimation_justification'] = (
            'Explicit screening approximation after median-ground roof conflicts were measured: '
            'use highest DTM under the full all-touched footprint plus the unchanged supplied AGL estimate. '
            'This overestimates roof elevation relative to a median base and can falsely block rays; no guaranteed visibility bound.')
        limitations.append(buildings['base_estimation_justification'])
    return {
        'data_root': str(DATA), 'output_dir': str(output), 'crs': 'EPSG:5186',
        'vertical_reference': VERTICAL_REFERENCE, 'resolution_m': 5, 'bounds': bounds,
        'source_kind': 'local',
        'terrain': {'kind': 'raster', 'path': str(dtm), 'bare_earth_verified': True,
            'units': 'm', 'vertical_reference': VERTICAL_REFERENCE, 'source_date': '2023',
            'coverage_boundary': {'path': str(boundary)}},
        'buildings': buildings, 'output_boundary': {'path': str(boundary)},
        'provenance': {
            'terrain_catalog': 'https://data.seoul.go.kr/dataList/OA-22241/F/1/datasetView.do',
            'terrain_credit': 'Seoul Open Data / NGII 2023 contours and spot heights; KOGL Type 1',
            'terrain_validation_path': str(validation_path), 'terrain_validation': validation,
            'buildings_catalog': 'https://github.com/zhu-xlab/GlobalBuildingAtlas',
            'building_source_record': str(DATA / 'acquisition/buildings/gba_seoul_2025.source.json'),
            'boundary_credit': '© OpenStreetMap contributors, ODbL 1.0, relation 2297418; retrieved 2026-09-07',
            'hypothetical_example_target': {'lon':126.9777,'lat':37.578,'height_m':100,'height_reference':'agl'},
        }, 'limitations': limitations,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--area', choices=['pilot','central'], default='pilot')
    parser.add_argument('--base-method', choices=['median','maximum'], default='median')
    parser.add_argument('--prepare', action='store_true', help='Execute after writing the concrete config and plan')
    args = parser.parse_args()
    config = make_config(args.area, args.base_method)
    config_path = ROOT / 'examples' / f'seoul-{args.area}-{args.base_method}.json'
    write_json(config_path, config)
    report = plan(config_path)
    report_path = ROOT / 'reports' / f'seoul-{args.area}-{args.base_method}-plan.json'
    # Resource measurements naturally change on each preflight; print each new
    # plan but preserve the original record alongside the product manifest.
    if not report_path.exists():
        write_json(report_path, report)
    print(json.dumps({'config':str(config_path),'plan':report}, ensure_ascii=False, indent=2), flush=True)
    if args.prepare:
        start = time.perf_counter()
        manifest = prepare(config_path)
        peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform != 'darwin':
            peak_rss *= 1024
        execution = {'manifest':str(manifest), 'prepare_or_verified_resume_s':time.perf_counter()-start,
            'process_peak_rss_bytes':peak_rss,
            'memory_scope':'whole preparation script including input selection and source verification',
            'preflight_after':preflight(DATA, additional_bytes=0)}
        run_record = ROOT / 'reports' / f'seoul-{args.area}-{args.base_method}-preparation.json'
        if not run_record.exists():
            write_json(run_record, execution)
        print(json.dumps(execution), flush=True)


if __name__ == '__main__':
    main()
