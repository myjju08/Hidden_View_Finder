"""Reuse the pinned official terrain contract and existing bounded TIN builder."""
from __future__ import annotations
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
from zipfile import ZipFile

from scripts import acquire_terrain as source_contract
from seoul_visibility.acquisition_safety import atomic_json, guarded_download, safe_extract_zip, sha256


TERRAIN_LAYERS = (
    ('등고선 5000/N3L_F001.shp', 'contours', 'CONT', 8570, 'MultiLineString'),
    ('표고 5000/N3P_F002.shp', 'spots', 'NUME', 45870, 'Point'),
)


def _verified_terrain_sources(raw: Path, budget) -> dict:
    """Validate pinned archive and every extracted member without expanding again."""
    archive = budget.safe_path(raw / source_contract.ARCHIVE_NAME)
    if (not archive.is_file() or archive.stat().st_size != source_contract.ARCHIVE_BYTES
            or sha256(archive) != source_contract.ARCHIVE_SHA256):
        raise ValueError('Official terrain archive fails its unchanged pinned size/SHA256 contract; preserved')
    members = {}
    with ZipFile(archive) as zipped:
        infos = zipped.infolist()
        if len(infos) > 1000 or sum(info.file_size for info in infos) != source_contract.EXPANDED_BYTES:
            raise ValueError('Pinned terrain archive expansion metadata changed')
        for info in infos:
            if info.is_dir():
                continue
            relative = Path(info.filename)
            if relative.is_absolute() or '..' in relative.parts or '\\' in info.filename:
                raise ValueError('Unsafe terrain archive member')
            source = budget.safe_path(raw / 'source' / relative)
            if not source.is_file() or source.stat().st_size != info.file_size:
                raise ValueError(f'Extracted terrain source missing or truncated: {relative}')
            digest = hashlib.sha256()
            expanded = 0
            with zipped.open(info) as stream:
                while block := stream.read(1024**2):
                    expanded += len(block)
                    if expanded > info.file_size:
                        raise ValueError('Terrain member expanded beyond its pinned bound')
                    digest.update(block)
            if expanded != info.file_size or sha256(source) != digest.hexdigest():
                raise ValueError(f'Extracted terrain source differs from retained pinned archive: {relative}')
            members[str(relative)] = digest.hexdigest()
    for relative, *_ in TERRAIN_LAYERS:
        stem = Path(relative).with_suffix('')
        for suffix in ('.shp', '.shx', '.dbf', '.prj'):
            if str(stem.with_suffix(suffix)) not in members:
                raise ValueError(f'Terrain shapefile sidecar missing: {stem}{suffix}')
    return {'archive': archive, 'archive_sha256': source_contract.ARCHIVE_SHA256,
            'members_sha256': members}


def validate_terrain(path: Path) -> dict:
    """Offline terrain schema/CRS/elevation/index check, one geometry at a time."""
    from osgeo import ogr
    from shapely import from_wkb
    ogr.UseExceptions()
    with sqlite3.connect(f'file:{Path(path).resolve()}?mode=ro', uri=True) as db:
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('Normalized terrain failed SQLite integrity validation')
        for _, name, _, _, _ in TERRAIN_LAYERS:
            columns = {row[1] for row in db.execute(f'PRAGMA table_info({name})')}
            if not {'source_id', 'elevation_m', 'invalid'} <= columns:
                raise ValueError('Normalized terrain schema lacks identity/elevation/quality attributes')
            row = db.execute('SELECT column_name,srs_id FROM gpkg_geometry_columns WHERE table_name=?', (name,)).fetchone()
            if not row or row[1] != 5186:
                raise ValueError('Normalized terrain CRS is missing or incorrect')
            if not db.execute('SELECT 1 FROM sqlite_master WHERE type=? AND name=?', ('table', f'rtree_{name}_{row[0]}')).fetchone():
                raise ValueError('Normalized terrain spatial index missing')
    dataset = ogr.Open(str(path))
    if dataset is None or dataset.GetLayerCount() != len(TERRAIN_LAYERS):
        raise ValueError('Normalized terrain is not parseable as two expected layers')
    counts = {}
    for _, name, _, expected, _ in TERRAIN_LAYERS:
        layer = dataset.GetLayerByName(name)
        if layer is None or layer.GetFeatureCount() != expected:
            raise ValueError('Normalized terrain feature count differs from pinned source')
        invalid = duplicates = 0
        identifiers = set()
        for feature in layer:
            geometry = feature.GetGeometryRef()
            elevation = feature['elevation_m']
            if geometry is None or geometry.IsEmpty() or geometry.WkbSize() > 16_000_000:
                raise ValueError('Terrain geometry is missing, empty, or beyond the bounded decode size')
            if elevation is None or not math.isfinite(float(elevation)):
                raise ValueError('Terrain elevation missing/nonfinite; no implicit zero is accepted')
            actual_invalid = not from_wkb(bytes(geometry.ExportToWkb())).is_valid
            if bool(feature['invalid']) != actual_invalid:
                raise ValueError('Terrain invalid-geometry quality evidence disagrees with geometry')
            identifier = feature['source_id']
            if identifier is None:
                raise ValueError('Terrain source identity missing')
            duplicates += identifier in identifiers
            identifiers.add(identifier)
            invalid += actual_invalid
        counts[name] = {'features': expected, 'invalid_geometry': invalid, 'duplicate_source_ids': duplicates}
    dataset = None
    return {'valid': True, 'layers': counts, 'crs': 'EPSG:5186', 'elevation_units': 'm'}


def acquire_terrain(destination: Path, budget):
    archive = destination / source_contract.ARCHIVE_NAME
    transfer = guarded_download(source_contract.DOWNLOAD_URL, archive, budget,
        max_bytes=source_contract.ARCHIVE_BYTES, expected_size=source_contract.ARCHIVE_BYTES,
        expected_sha256=source_contract.ARCHIVE_SHA256, method='POST', data=source_contract.POST_BODY,
        headers={'Content-Type': 'application/x-www-form-urlencoded', 'Referer': source_contract.CATALOG_URL},
        allowed_hosts={'datafile.seoul.go.kr'}, magic=b'PK')
    expanded = safe_extract_zip(archive, destination / 'source', budget,
        max_expanded_bytes=source_contract.EXPANDED_BYTES,
        expected_expanded_bytes=source_contract.EXPANDED_BYTES)
    return dict(status='acquired', archive=str(archive), transfer=transfer, extraction=expanded,
        source_year='2023', source_file_updated='2025-03-20', catalogue=source_contract.CATALOG_URL,
        licence='KOGL Type 1', vertical_reference=source_contract.VERTICAL_REFERENCE,
        checksum_kind='Repository-pinned inspected SHA256, not independently publisher-published SHA256',
        coverage='Official Seoul contours and spot heights; surrounding support area is not supplied')


def normalize_terrain(raw: Path, output: Path, support_wgs84, budget):
    """Reproducible, safely restartable official terrain normalization."""
    from scripts.data.osm_pipeline import _resume_product, validate_contract_geometry
    validate_contract_geometry(support_wgs84, crs='EPSG:4326')
    raw, output = budget.safe_path(raw), budget.safe_path(output)
    sources = _verified_terrain_sources(raw, budget)
    key = {'archive_sha256': source_contract.ARCHIVE_SHA256, 'schema': 2, 'crs': 'EPSG:5186',
           'support_geometry_sha256': hashlib.sha256(support_wgs84.wkb).hexdigest(),
           'source_members_sha256': sources['members_sha256']}
    partial = output.with_suffix('.part.gpkg')
    with budget.reserve(384_000_000, temporary_bytes=384_000_000, label='terrain normalization') as reservation:
        return _resume_product(output, key, {'archive': (sources['archive'], sources['archive_sha256'])},
            lambda n: reservation.check_write(n, partial),
            lambda callback: _normalize_terrain_once(raw, output, partial, key, reservation, callback),
            budget=budget, validator=validate_terrain, partial_path=partial, rebuild_peak_bytes=384_000_000)


def _normalize_terrain_once(raw, output, partial, key, reservation, publication_callback):
    from osgeo import ogr, osr, gdal
    from shapely import from_wkb
    ogr.UseExceptions()
    dst_srs = osr.SpatialReference(); dst_srs.ImportFromEPSG(5186)
    dst_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    report = {'recipe': key, 'layers': [], 'vertical_reference': source_contract.VERTICAL_REFERENCE,
              'vertical_conversion': 'none', 'coverage_supported_beyond_seoul': False}
    reservation.check_write(1024**2, partial)
    ds = ogr.GetDriverByName('GPKG').CreateDataSource(str(partial))
    result = ds.ExecuteSQL('PRAGMA page_size')
    page_size = result.GetNextFeature().GetField(0); ds.ReleaseResultSet(result)
    result = ds.ExecuteSQL(f'PRAGMA max_page_count={(256 * 1024**2) // page_size}'); ds.ReleaseResultSet(result)
    result = ds.ExecuteSQL('PRAGMA journal_mode=MEMORY'); ds.ReleaseResultSet(result)
    result = ds.ExecuteSQL('PRAGMA cache_size=-16384'); ds.ReleaseResultSet(result)
    credit=0
    try:
        for relative, name, field, expected_count, geometry_name in TERRAIN_LAYERS:
            geom_type = getattr(ogr, 'wkb' + geometry_name)
            path = raw / 'source' / relative
            original = gdal.OpenEx(str(path), gdal.OF_VECTOR, open_options=['ENCODING=CP949'])
            layer = original.GetLayer(0)
            srs = layer.GetSpatialRef()
            if srs is not None:
                srs.AutoIdentifyEPSG()
            if srs is None or srs.GetAuthorityCode(None) != '5174':
                raise ValueError('Terrain source CRS is missing or changed, expected documented EPSG:5174')
            if layer.GetLayerDefn().GetFieldIndex(field) < 0 or layer.GetLayerDefn().GetFieldIndex('UFID') < 0:
                raise ValueError('Terrain elevation schema changed')
            if layer.GetFeatureCount() != expected_count:
                raise ValueError('Pinned source feature count changed')
            target = ds.CreateLayer(name, dst_srs, geom_type, options=['SPATIAL_INDEX=YES'])
            for column, dtype in [('source_id',ogr.OFTString),('elevation_m',ogr.OFTReal),('invalid',ogr.OFTInteger)]:
                target.CreateField(ogr.FieldDefn(column,dtype))
            sr = srs.Clone(); sr.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            tx = osr.CoordinateTransformation(sr,dst_srs)
            counts = Counter(); types = Counter(); elevation_min = math.inf; elevation_max = -math.inf
            ids = set(); minx=miny=math.inf; maxx=maxy=-math.inf
            ds.StartTransaction()
            for feature in layer:
                geometry = feature.GetGeometryRef()
                height = feature[field]
                if geometry is None or height is None or not math.isfinite(float(height)):
                    raise ValueError('Missing terrain geometry or elevation; no imputation allowed')
                if geometry.WkbSize() > 16_000_000:
                    raise ValueError('Terrain geometry exceeds bounded per-feature decoding budget')
                counts['features'] += 1
                elevation_min=min(elevation_min,float(height));elevation_max=max(elevation_max,float(height))
                if feature['UFID'] is None:
                    raise ValueError('Terrain source identity missing; no fabricated ID is accepted')
                source_id=str(feature['UFID'])
                counts['duplicate_source_ids'] += source_id in ids;ids.add(source_id)
                geom=geometry.Clone(); geom.Transform(tx)
                if name=='contours': geom=ogr.ForceToMultiLineString(geom)
                shp=from_wkb(bytes(geom.ExportToWkb()))
                counts['invalid_geometry'] += not shp.is_valid
                counts['geometry_z'] += shp.has_z
                types[shp.geom_type] += 1
                a,b,c,d=shp.bounds;minx=min(minx,a);miny=min(miny,b);maxx=max(maxx,c);maxy=max(maxy,d)
                upcoming=geom.WkbSize()*3+65536
                if credit<upcoming:
                    credit=max(4*1024**2,upcoming)
                    reservation.check_write(credit,partial)
                credit-=upcoming
                out=ogr.Feature(target.GetLayerDefn());out.SetGeometry(geom)
                out.SetField('source_id',source_id);out.SetField('elevation_m',float(height));out.SetField('invalid',int(not shp.is_valid))
                target.CreateFeature(out);out=None
                if counts['features']%500==0:
                    ds.CommitTransaction();ds.StartTransaction();reservation.observe()
            ds.CommitTransaction()
            report['layers'].append(dict(layer=name,source_field=field,elevation_field='elevation_m',
                source_crs='EPSG:5174',crs='EPSG:5186',units='m',encoding='CP949',
                elevation_range_m=[elevation_min,elevation_max],bounds=[minx,miny,maxx,maxy],
                geometry_types=dict(types),repairs=0,missing_elevations=0,**counts))
            original=None;target=None
    finally:
        ds=None
    audit = validate_terrain(partial)
    with partial.open('rb') as stream:
        os.fsync(stream.fileno())
    report.update(path=output.name, sha256=sha256(partial), bytes=partial.stat().st_size, validation=audit,
        coverage_caveat='Source bounds are not a support mask. TIN 500m edge and halo checks remain necessary; unsupported cells remain NoData.')
    publication_callback(report)
    reservation.check_write(8192, partial)
    if output.exists():
        raise FileExistsError('Terrain final appeared before atomic publication; both representations preserved')
    partial.replace(output)
    return report
