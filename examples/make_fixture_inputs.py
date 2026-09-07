#!/usr/bin/env python3
"""Create small FICTIONAL raw inputs for inspect -> plan -> prepare -> query.

Run ``python examples/make_fixture_inputs.py`` from an installed checkout.
The default data root is ``data`` in this repository.  Existing raw inputs or
configuration are never overwritten; select another --data-root to repeat.
Values and geometry are deterministic.  File creation metadata may differ.
These contours, elevations, footprints and boundary are not Seoul measurements.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import tempfile
import time

from osgeo import gdal, ogr, osr

from seoul_visibility.resources import StoragePolicy, preflight

BOUNDS = [199250, 549250, 200750, 550750]
SOURCE_BOUNDS = [199150, 549150, 200850, 550850]
DATUM = "fictional fixture local orthometric datum; not measured Seoul elevations"


def elevation(y: float) -> float:
    return 100 + (y - BOUNDS[1]) / 100


def rectangle(x1, y1, x2, y2):
    return f"POLYGON(({x1} {y1},{x2} {y1},{x2} {y2},{x1} {y2},{x1} {y1}))"


def add_feature(layer, geometry, fields):
    feature = ogr.Feature(layer.GetLayerDefn())
    feature.SetGeometry(geometry)
    for name, value in fields.items():
        feature.SetField(name, value)
    if layer.CreateFeature(feature) != ogr.OGRERR_NONE:
        raise RuntimeError("Unable to create fictional feature")


def generate(data_root: Path) -> dict:
    started = time.perf_counter()
    data_root = data_root.resolve()
    raw = data_root / 'raw-example'
    config_path = data_root / 'fixture-config.json'
    for path in (raw, config_path):
        if path.exists() or path.is_symlink():
            raise FileExistsError(f"Refusing to overwrite {path}; choose another --data-root")
    data_root.mkdir(parents=True, exist_ok=True)
    policy = StoragePolicy()
    preflight(data_root, additional_bytes=2 * 1024**2, temporary_bytes=2 * 1024**2, policy=policy)
    staging = Path(tempfile.mkdtemp(prefix='.fixture-inputs-', dir=data_root))
    published = False
    try:
        gdal.UseExceptions()
        ogr.UseExceptions()
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(5186)
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        terrain = ogr.GetDriverByName('GPKG').CreateDataSource(str(staging / 'fictional-terrain.gpkg'))
        contours = terrain.CreateLayer('fictional_contours', srs, ogr.wkbLineString)
        contours.CreateField(ogr.FieldDefn('SYN_Z_M', ogr.OFTReal))
        for y in range(SOURCE_BOUNDS[1], SOURCE_BOUNDS[3] + 1, 100):
            line = ogr.Geometry(ogr.wkbLineString)
            line.AddPoint_2D(SOURCE_BOUNDS[0], y)
            line.AddPoint_2D(SOURCE_BOUNDS[2], y)
            add_feature(contours, line, {'SYN_Z_M': elevation(y)})
        spots = terrain.CreateLayer('fictional_spots', srs, ogr.wkbPoint25D)
        spots.CreateField(ogr.FieldDefn('SYN_Z_M', ogr.OFTReal))
        for row in range(15):
            for col in range(15):
                x, y = BOUNDS[0] + 37.5 + col * 100, BOUNDS[1] + 62.5 + row * 100
                point = ogr.Geometry(ogr.wkbPoint25D)
                point.AddPoint(x, y, elevation(y))
                add_feature(spots, point, {'SYN_Z_M': elevation(y)})
        boundary = terrain.CreateLayer('fictional_output_boundary', srs, ogr.wkbPolygon)
        add_feature(boundary, ogr.CreateGeometryFromWkt(rectangle(199300, 549300, 200700, 550700)), {})
        terrain.ExecuteSQL("UPDATE gpkg_contents SET last_change='2026-09-07T00:00:00.000Z'")
        terrain = None

        buildings = ogr.GetDriverByName('ESRI Shapefile').CreateDataSource(str(staging / 'fictional-buildings.shp'))
        footprints = buildings.CreateLayer('fictional-buildings', srs, ogr.wkbPolygon, options=['ENCODING=CP949'])
        footprints.CreateField(ogr.FieldDefn('SYN_H_M', ogr.OFTReal))
        name_field = ogr.FieldDefn('SYN_NAME', ogr.OFTString)
        name_field.SetWidth(64)
        footprints.CreateField(name_field)
        courtyard = ('POLYGON((199600 549700,199750 549700,199750 549850,199600 549850,199600 549700),'
                     '(199645 549745,199645 549805,199705 549805,199705 549745,199645 549745))')
        bowtie = 'POLYGON((199600 550200,199650 550250,199600 550250,199650 550200,199600 550200))'
        rows = [(rectangle(200130, 549550, 200150, 550450), 40),
                (rectangle(200135, 549950, 200170, 550050), 65),
                (courtyard, 15), (bowtie, 12),
                (rectangle(199650, 550450, 199750, 550500), 20),
                (rectangle(199650, 550450, 199750, 550500), 20)]
        for index, (wkt, height) in enumerate(rows):
            add_feature(footprints, ogr.CreateGeometryFromWkt(wkt),
                        {'SYN_H_M': height, 'SYN_NAME': f'가상건물 {index + 1}'})
        buildings = None
        provenance = {
            'source_kind': 'synthetic', 'warning': 'All inputs are fictional; no real Seoul measurements or accuracy claim.',
            'horizontal_crs': 'EPSG:5186', 'vertical_reference': DATUM,
            'terrain_formula': 'z_m = 100 + (y_m - 549250) / 100',
            'contour_features': 18, 'spot_features': 225, 'building_features': len(rows),
            'building_height_semantics': 'SYN_H_M is a known fictional height above bare earth in metres.',
            'building_encoding': 'CP949; SYN_NAME contains Korean synthetic labels.',
            'geometry_cases': ['continuous wall', 'overlapping taller roof', 'courtyard hole', 'invalid bowtie to repair', 'exact duplicate'],
            'source_bounds': SOURCE_BOUNDS, 'requested_prepared_bounds': BOUNDS,
            'source_publication_scale': None, 'source_date': None,
            'boundary': 'Fictional output rectangle, not the Seoul administrative boundary.',
        }
        (staging / 'PROVENANCE.json').write_text(json.dumps(provenance, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        config = {
            'output_dir': './prepared-example', 'data_root': '.', 'crs': 'EPSG:5186',
            'source_kind': 'synthetic', 'vertical_reference': DATUM, 'resolution_m': 5, 'bounds': BOUNDS,
            'terrain': {
                'kind': 'samples', 'units': 'm', 'vertical_reference': DATUM,
                'sources': [
                    {'path': './raw-example/fictional-terrain.gpkg', 'layer': 'fictional_contours',
                     'elevation_field': 'SYN_Z_M', 'units': 'm', 'vertical_reference': DATUM},
                    {'path': './raw-example/fictional-terrain.gpkg', 'layer': 'fictional_spots',
                     'elevation_from_z': True, 'units': 'm', 'vertical_reference': DATUM},
                ],
                'sample_spacing_m': 25, 'max_sample_points': 10000, 'max_triangle_edge_m': 180,
                'halo_m': 250, 'tile_size': 128, 'max_points_per_tile': 10000,
                'duplicate_elevation_tolerance_m': 0.001,
            },
            'buildings': {
                'path': './raw-example/fictional-buildings.shp', 'encoding': 'CP949',
                'height_field': 'SYN_H_M', 'height_is_agl': True, 'units': 'm',
                'vertical_reference': DATUM, 'coverage_bounds': SOURCE_BOUNDS, 'invalid_geometry': 'repair',
            },
            'output_boundary': {'path': './raw-example/fictional-terrain.gpkg', 'layer': 'fictional_output_boundary'},
            'storage_policy': asdict(policy),
        }
        # Publish without replacing any existing directory or file, including
        # paths created by a concurrent writer.  Config is the final marker.
        lock_path = data_root / '.fixture-inputs.lock'
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        published_files = []
        created_raw = False
        try:
            if raw.exists() or raw.is_symlink() or config_path.exists() or config_path.is_symlink():
                raise FileExistsError('Fixture destinations appeared during generation; no source overwritten')
            raw.mkdir(exist_ok=False)
            created_raw = True
            for source_file in sorted(staging.iterdir()):
                destination = raw / source_file.name
                os.link(source_file, destination)
                published_files.append(destination)
            config_temp = staging / 'fixture-config.json'
            config_temp.write_text(json.dumps(config, indent=2) + '\n', encoding='utf-8')
            os.link(config_temp, config_path)
            published_files.append(config_path)
            published = True
        finally:
            if not published:
                for own_file in published_files:
                    own_file.unlink(missing_ok=True)
                if created_raw:
                    try:
                        raw.rmdir()
                    except OSError:
                        pass  # a concurrent writer's files are never removed
            lock_path.unlink()
        return {'source_kind': 'synthetic', 'raw_directory': str(raw), 'config': str(config_path),
                'generated_s': time.perf_counter() - started, 'provenance': provenance}
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path, default=Path(__file__).resolve().parents[1] / 'data')
    args = parser.parse_args()
    print(json.dumps(generate(args.data_root), indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
