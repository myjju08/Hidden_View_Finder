"""Citywide orchestration around the existing spatial HTTP-range GBA reader.

Only bounded metadata is read by plan_buildings. Acquisition is a single writer,
with native SQLite page limits, bounded Arrow batches, row-group transactions,
and checkpoints stored in the same transaction as the associated footprints.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import uuid

from scripts.acquire_buildings import Ranges, URL, MiB
from scripts.building_acquisition_common import validate_bounds
from seoul_visibility.resources import memory_preflight
from seoul_visibility.acquisition_safety import AcquisitionError


def fingerprint(path: Path) -> str:
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _physical_group_range(row, data_end):
    """Inspect complete column chunks before scheduling a single bounded read."""
    chunks = []
    for j in range(row.num_columns):
        column = row.column(j)
        offsets = [column.data_page_offset]
        if column.dictionary_page_offset is not None:
            offsets.append(column.dictionary_page_offset)
        if any(not isinstance(value, int) or value < 4 for value in offsets):
            raise RuntimeError('Building schema_changed: unexplained column page offsets')
        start = min(offsets)
        size = column.total_compressed_size
        if not isinstance(size, int) or size <= 0 or start + size > data_end or max(offsets) >= start + size:
            raise RuntimeError('Building schema_changed: column chunk lies outside bounded Parquet data')
        chunks.append((start, start + size))
    chunks.sort()
    if not chunks or any(left[1] > right[0] for left, right in zip(chunks, chunks[1:])):
        raise RuntimeError('Building schema_changed: overlapping or absent column chunks')
    start, end = chunks[0][0], chunks[-1][1]
    gap_bytes = end - start - sum(right - left for left, right in chunks)
    if end - start > 8 * MiB or gap_bytes > 64 * 1024:
        raise RuntimeError('Building row-group span exceeds 8 MiB or bounded 64 KiB inter-column gaps')
    return {'range_start': start, 'range_bytes': end - start, 'range_gap_bytes': gap_bytes}


def _inspect(remote, bbox):
    import pyarrow.parquet as pq

    file = pq.ParquetFile(remote, pre_buffer=False)
    metadata = file.metadata
    try:
        geo = json.loads(metadata.metadata[b'geo'])
        spec = geo['columns']['geometry']
        if ('crs' in spec or spec['geometry_types'] != ['Polygon']
                or spec['encoding'] != 'WKB' or geo['version'] != '1.1.0'):
            raise ValueError('Changed or unexplained source CRS/geometry')
        west, south, east, north = map(float, spec['bbox'])
        if not (120 < west < east < 135 and 25 < south < north < 45):
            raise ValueError('Source coordinate convention is not the inspected WGS84 distribution')
        columns = set(file.schema_arrow.names)
        if not {'geometry', 'bbox', 'source', 'id', 'height', 'var'} <= columns:
            raise ValueError('Required source identity/height/uncertainty attributes missing')
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError(f'Building schema_changed: {error}') from error
    groups = []
    all_groups = []
    west, south, east, north = bbox
    for i in range(metadata.num_row_groups):
        row = metadata.row_group(i)
        stats = {row.column(j).path_in_schema: row.column(j).statistics for j in range(row.num_columns)}
        try:
            # Excluding a row group is safe only when its bounding statistics
            # account for every row. Null/missing statistics cannot imply empty
            # ground or authorize omission of a potentially intersecting group.
            for name in ['bbox.xmin', 'bbox.ymin', 'bbox.xmax', 'bbox.ymax']:
                statistic = stats[name]
                if (statistic is None or not statistic.has_min_max or statistic.null_count != 0
                        or statistic.num_values != row.num_rows):
                    raise ValueError('Incomplete bounding statistics in row group')
            bounds = [float(stats['bbox.xmin'].min), float(stats['bbox.ymin'].min),
                      float(stats['bbox.xmax'].max), float(stats['bbox.ymax'].max)]
            if not all(math.isfinite(v) for v in bounds):
                raise ValueError('Non-finite row-group bounds')
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise RuntimeError('Building schema_changed: required spatial statistics missing') from error
        record = {'index': i, 'bounds': bounds, 'rows': row.num_rows,
                  'compressed_bytes': sum(row.column(j).total_compressed_size for j in range(row.num_columns)),
                  'uncompressed_bytes': sum(row.column(j).total_uncompressed_size for j in range(row.num_columns))}
        all_groups.append(record)
        if bounds[0] <= east and bounds[1] <= north and bounds[2] >= west and bounds[3] >= south:
            data_end = remote.identity['size'] - 8 - int.from_bytes(remote.tail[:4], 'little')
            record.update(_physical_group_range(row, data_end))
            groups.append(record)
    return file, spec, groups, all_groups


def plan_buildings(bbox, cap_bytes=512 * MiB, *, url=URL):
    """Read at most 2 MiB of Parquet metadata, without feature row groups."""
    validate_bounds(bbox, 'bbox')
    if not (126.4 <= bbox[0] < bbox[2] <= 127.7 and 37.1 <= bbox[1] < bbox[3] <= 38.1):
        raise ValueError('Building acquisition is restricted to Seoul and declared surroundings')
    if not isinstance(cap_bytes, int) or cap_bytes <= 0:
        raise ValueError('Transfer cap must be a positive byte count')
    remote = Ranges(url, min(cap_bytes, 2 * MiB))
    file, spec, groups, all_groups = _inspect(remote, bbox)
    del file
    compressed = sum(g['compressed_bytes'] for g in groups)
    transfer = sum(g['range_bytes'] for g in groups)
    max_range = max((g['range_bytes'] for g in groups), default=0)
    uncompressed = sum(g['uncompressed_bytes'] for g in groups)
    rows = sum(g['rows'] for g in groups)
    # No assumed compression savings: account all selected rows, two copies of
    # their uncompressed bytes, plus 1 KiB per-row SQLite/index overhead. This
    # conservative estimate is also a hard SQLite page-count limit: if it is
    # insufficient, stop/checkpoint instead of growing beyond the reservation.
    output_bound = 2 * uncompressed + 1024 * rows + 8 * MiB
    # Journal bound allows a second complete output, with native page caps as
    # an additional enforcement mechanism. Budget adds its own 25% margin.
    peak = 2 * output_bound + 8 * MiB
    max_group = max((g['uncompressed_bytes'] for g in groups), default=0)
    plan = {'recipe_version': 1, 'identity': remote.identity, 'bbox': list(bbox),
            'source_crs': 'OGC:CRS84', 'output_crs': 'EPSG:5186',
            'geo_metadata': spec, 'selected_groups': groups,
            'all_group_bounds': [{'index': g['index'], 'bounds': g['bounds']} for g in all_groups],
            'row_groups_total': len(all_groups), 'planned_transfer_bytes': transfer,
            'selected_compressed_column_bytes': compressed,
            'planned_inter_column_gap_bytes': transfer - compressed,
            'metadata_received_bytes': remote.bytes_read, 'metadata_ranges': remote.reads,
            'max_uncompressed_row_group_bytes': max_group,
            'decode_memory_bound_bytes': max_group * 4 + 2 * max_range + 64 * MiB,
            'maximum_prefetch_bytes': max_range, 'decode_batch_rows': 1024,
            'prefetch_policy': 'One physical row group, at most 8 MiB and 64 KiB inter-column gaps; strict 206 and identity checks still apply',
            'output_bound_bytes': output_bound, 'incremental_peak_bytes': peak,
            'transfer_cap_bytes': cap_bytes,
            'estimated_selected_rows_before_spatial_filter': rows,
            'no_bulk_downloads': True,
            'licence_review': 'required_before_public_deployment_or_redistribution',
            'coverage_claim': 'Row-group envelopes identify candidate data, not full building detection.'}
    if transfer + 2 * MiB > cap_bytes:
        raise RuntimeError(f'Building planned transfer {transfer + 2 * MiB} exceeds source cap {cap_bytes}')
    if not groups:
        raise RuntimeError('Building coverage_blocked: no row groups overlap the requested extent')
    return plan


def _write_json(path, value, budget, reservation):
    encoded = (json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n').encode()
    if len(encoded) > 2 * MiB:
        raise RuntimeError('Building checkpoint exceeded its bounded metadata allocation')
    # Exclusive unique staging avoids overwriting a pre-existing user file or
    # an interrupted metadata write. Abandoned tiny files remain accounted.
    writing = budget.safe_path(path.with_name(path.name + '.' + uuid.uuid4().hex + '.writing'))
    reservation.check_write(len(encoded) + 4096, writing)
    with writing.open('xb') as output:
        output.write(encoded)
        output.flush()
        os.fsync(output.fileno())
    os.replace(writing, path)
    reservation.observe()


def _sql_scalar(ds, sql):
    result = ds.ExecuteSQL(sql)
    try:
        return result.GetNextFeature().GetField(0)
    finally:
        ds.ReleaseResultSet(result)


def _feature_digest_update(digest, feature):
    """Stable stored-row fingerprint, independent of GeoPackage file layout."""
    from osgeo import ogr

    geometry = bytes(feature.GetGeometryRef().ExportToWkb(ogr.wkbNDR))
    attributes = _canonical([feature.GetField(i) for i in range(feature.GetFieldCount())]).encode()
    digest.update(len(geometry).to_bytes(8, 'little'))
    digest.update(geometry)
    digest.update(len(attributes).to_bytes(8, 'little'))
    digest.update(attributes)


def _validate_group_checkpoint(layer, group, report):
    digest = hashlib.sha256()
    layer.SetAttributeFilter(f'source_row_group={group}')
    layer.ResetReading()
    count = 0
    for feature in layer:
        _feature_digest_update(digest, feature)
        count += 1
    layer.SetAttributeFilter(None)
    if (report.get('stored_rows_sha256') != digest.hexdigest()
            or count != report['counts'].get('retained', 0)):
        raise RuntimeError('Partial building checkpoint rows changed or are incomplete; preserved')


def validate_buildings(path: Path, *, expected_hash=None):
    """Offline structural/semantic audit; never trusts a feature count alone."""
    from osgeo import ogr

    if expected_hash and fingerprint(path) != expected_hash:
        raise RuntimeError('Corrupt/truncated building subset: checksum mismatch')
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as connection:
        if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise RuntimeError('Corrupt building GeoPackage')
        columns = {r[1] for r in connection.execute('PRAGMA table_info(buildings)')}
        required = {'source_id', 'height_m', 'height_var', 'estimated', 'unresolved',
                    'invalid_geometry', 'source', 'height_reference', 'height_units'}
        if not required <= columns:
            raise RuntimeError('Building schema missing required height/provenance attributes')
        geom = connection.execute("SELECT column_name,srs_id FROM gpkg_geometry_columns WHERE table_name='buildings'").fetchone()
        if not geom or geom[1] != 5186:
            raise RuntimeError('Building CRS is missing or not EPSG:5186')
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", ('rtree_buildings_' + geom[0],)).fetchone():
            raise RuntimeError('Building spatial index is missing')
        count, unresolved, invalid = connection.execute('SELECT count(*),coalesce(sum(unresolved),0),coalesce(sum(invalid_geometry),0) FROM buildings').fetchone()
        duplicates = connection.execute('SELECT count(*) FROM (SELECT source,source_id FROM buildings GROUP BY source,source_id HAVING count(*)>1)').fetchone()[0]
        bad_heights = connection.execute("SELECT count(*) FROM buildings WHERE (unresolved=1 AND height_m IS NOT NULL) OR (unresolved=0 AND (height_m IS NULL OR height_m<=0)) OR height_reference!='AGL' OR height_units!='m'").fetchone()[0]
        sources = dict(connection.execute('SELECT source,count(*) FROM buildings GROUP BY source'))
        variance_missing, variance_negative, below_one = connection.execute(
            'SELECT coalesce(sum(height_var IS NULL),0),coalesce(sum(height_var<0),0),'
            'coalesce(sum(height_m<1),0) FROM buildings').fetchone()
        height_min, height_max = connection.execute('SELECT min(height_m),max(height_m) FROM buildings').fetchone()
        if bad_heights:
            raise RuntimeError('Invalid building height semantics')
    ds = ogr.Open(str(path))
    if ds is None:
        raise RuntimeError('Building GeoPackage is unparseable')
    layer = ds.GetLayerByName('buildings')
    bounds = list(layer.GetExtent()) if count else None
    ds = None
    return {'features': count, 'unresolved_height': unresolved, 'invalid_geometry': invalid,
            'repair_count': 0, 'duplicate_source_id_groups': duplicates,
            'height_variance_missing': variance_missing, 'height_variance_negative': variance_negative,
            'height_below_1m': below_one, 'height_min_m': height_min, 'height_max_m': height_max,
            'sources': sources, 'bounds_epsg5186_xmin_xmax_ymin_ymax': bounds,
            'sha256': fingerprint(path), 'file_bytes': path.stat().st_size,
            'whole_remote_object_checksum_verified': False}


def acquire_buildings(bbox, output: Path, budget, plan=None, *, support_wgs84=None,
                      cap_bytes=512 * MiB, progress=None):
    """Acquire supported footprints with durable per-row-group resume checkpoints.

    ``budget`` is the invocation's shared Budget. ``support_wgs84`` is a Shapely
    polygon or GeoJSON geometry; whole footprints intersecting it are retained.
    Gaps and invalid geometries are recorded, never filled or silently repaired.
    """
    from osgeo import gdal, ogr, osr
    import pyarrow.parquet as pq
    from pyproj import Transformer
    from shapely import from_wkb
    from shapely.geometry import box, shape as make_shape
    from shapely.ops import transform
    import pyarrow
    import shapely
    import pyproj

    validate_bounds(bbox, 'bbox')
    output = budget.safe_path(Path(output))
    plan = plan or plan_buildings(bbox, cap_bytes)
    if list(bbox) != plan['bbox']:
        raise ValueError('Building plan extent differs from requested extent')
    region = box(*bbox) if support_wgs84 is None else (make_shape(support_wgs84) if isinstance(support_wgs84, dict) else support_wgs84)
    if not region.is_valid or region.is_empty or not box(*bbox).buffer(1e-9).covers(region):
        raise ValueError('Building support geometry is invalid or outside the metadata plan extent')
    recipe = {'identity': plan['identity'], 'bbox': list(bbox), 'output_crs': 'EPSG:5186',
              'support_sha256': hashlib.sha256(region.wkb).hexdigest(),
              'selected_groups': [g['index'] for g in plan['selected_groups']], 'version': 1,
              'dependencies': {'gdal': gdal.VersionInfo('RELEASE_NAME'), 'pyarrow': pyarrow.__version__,
                               'shapely': shapely.__version__, 'pyproj': pyproj.__version__}}
    recipe_hash = hashlib.sha256(_canonical(recipe).encode()).hexdigest()
    sidecar = budget.safe_path(output.with_suffix('.source.json'))
    pending_report = budget.safe_path(output.with_suffix('.source.json.part'))
    temporary = budget.safe_path(output.with_suffix('.part.gpkg'))
    owner = budget.safe_path(output.with_suffix('.owner.json'))
    if output.exists():
        report_path = sidecar if sidecar.exists() else pending_report
        if not report_path.exists():
            raise RuntimeError('Existing building output lacks a recoverable source manifest; preserved')
        report = json.loads(report_path.read_text())
        if report.get('recipe_sha256') != recipe_hash:
            raise RuntimeError('Existing building output parameters changed; preserve and select a new output version')
        audit = validate_buildings(output, expected_hash=report['sha256'])
        if report_path == pending_report:
            os.replace(pending_report, sidecar)
        return {**report, **audit, 'reused': True}
    if owner.exists():
        if json.loads(owner.read_text()).get('recipe_sha256') != recipe_hash:
            raise RuntimeError('Partial building identity/recipe mismatch; preserve the previous source version')
    elif temporary.exists():
        raise RuntimeError('Unowned existing partial GeoPackage preserved')
    memory_preflight(plan['decode_memory_bound_bytes'])
    current_partial = temporary.stat().st_size if temporary.exists() else 0
    peak = max(8 * MiB, plan['incremental_peak_bytes'] - current_partial)
    with budget.reserve(peak, temporary_bytes=peak, label='buildings ranges and indexed subset') as reservation:
        output.parent.mkdir(parents=True, exist_ok=True)
        _write_json(owner, {'recipe_sha256': recipe_hash, 'recipe': recipe,
                           'owned_disposable': [], 'partial_is_recoverable': True}, budget, reservation)
        remote = Ranges(plan['identity']['url'], plan['transfer_cap_bytes'], expected_identity=plan['identity'])
        file, spec, groups, all_groups = _inspect(remote, bbox)
        if [g['index'] for g in groups] != recipe['selected_groups']:
            raise RuntimeError('Building selected row groups changed since metadata inspection')
        # Older compatible plans lack transport-span fields. Inspecting the
        # pinned footer supplies those bounds without changing subset identity
        # or invalidating already committed feature/checkpoint hashes.
        range_transfer = sum(group['range_bytes'] for group in groups)
        if range_transfer + 2 * MiB > plan['transfer_cap_bytes']:
            raise RuntimeError('Building physical row-group spans exceed planned transfer cap')
        decode_memory = max(group['uncompressed_bytes'] * 4 + 2 * group['range_bytes'] + 64 * MiB for group in groups)
        memory_preflight(decode_memory)
        ogr.UseExceptions()
        gdal.SetConfigOption('OGR_SQLITE_CACHE', '16')
        gdal.SetConfigOption('OGR_SQLITE_JOURNAL', 'DELETE')
        gdal.SetConfigOption('OGR_SQLITE_SYNCHRONOUS', 'FULL')
        gdal.SetCacheMax(16 * MiB)
        existing = temporary.exists()
        reservation.check_write(8 * MiB, temporary)
        ds = ogr.Open(str(temporary), update=1) if existing else ogr.GetDriverByName('GPKG').CreateDataSource(str(temporary))
        if ds is None:
            raise RuntimeError('Partial building GeoPackage could not be opened')
        try:
            if _sql_scalar(ds, 'PRAGMA quick_check') != 'ok':
                raise RuntimeError('Partial building GeoPackage failed integrity validation')
            page_size = int(_sql_scalar(ds, 'PRAGMA page_size'))
            max_pages = plan['output_bound_bytes'] // page_size
            if int(_sql_scalar(ds, f'PRAGMA max_page_count={max_pages}')) != max_pages:
                raise RuntimeError('Could not enforce native SQLite artifact page limit')
            ds.ExecuteSQL('PRAGMA cache_size=-16384')
            if not existing:
                sr = osr.SpatialReference()
                sr.ImportFromEPSG(5186)
                sr.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
                layer = ds.CreateLayer('buildings', sr, ogr.wkbPolygon, options=['SPATIAL_INDEX=YES'])
                for name, kind in [('source', ogr.OFTString), ('source_id', ogr.OFTString),
                                   ('height_m', ogr.OFTReal), ('height_var', ogr.OFTReal),
                                   ('estimated', ogr.OFTInteger), ('unresolved', ogr.OFTInteger),
                                   ('invalid_geometry', ogr.OFTInteger), ('selection_uncertain', ogr.OFTInteger),
                                   ('height_reference', ogr.OFTString), ('height_units', ogr.OFTString),
                                   ('source_row_group', ogr.OFTInteger)]:
                    layer.CreateField(ogr.FieldDefn(name, kind))
                ds.ExecuteSQL('CREATE TABLE acquisition_checkpoint (row_group INTEGER PRIMARY KEY, recipe_sha256 TEXT NOT NULL, report_json TEXT NOT NULL)')
                ds.ExecuteSQL('CREATE INDEX idx_buildings_source_row_group ON buildings(source_row_group)')
            layer = ds.GetLayerByName('buildings')
            completed = {}
            # INTEGER PRIMARY KEY is surfaced as OGR's feature ID, not an
            # ordinary field. A typed expression keeps this checkpoint key in
            # the result schema across GDAL versions without relying on FID.
            records = ds.ExecuteSQL('SELECT CAST(row_group AS TEXT) AS checkpoint_row_group,recipe_sha256,report_json FROM acquisition_checkpoint')
            for feature in records:
                if feature.GetField('recipe_sha256') != recipe_hash:
                    raise RuntimeError('Partial checkpoint input hash changed')
                group_id = int(feature.GetField('checkpoint_row_group'))
                if group_id not in recipe['selected_groups']:
                    raise RuntimeError('Partial checkpoint contains an unselected row group')
                completed[group_id] = json.loads(feature.GetField('report_json'))
            ds.ReleaseResultSet(records)
            for rg, report in completed.items():
                _validate_group_checkpoint(layer, rg, report)
            project = Transformer.from_crs(4326, 5186, always_xy=True).transform
            for group in groups:
                rg = group['index']
                if rg in completed:
                    continue
                # Journal could copy every prior page; reserve that capacity
                # before asking SQLite/GDAL to create a transaction.
                reservation.check_write(temporary.stat().st_size + 8 * MiB, temporary)
                before_ranges = len(remote.reads)
                before_bytes = remote.bytes_read
                counts = Counter()
                stored_rows_digest = hashlib.sha256()
                ds.StartTransaction()
                try:
                    prefetch = remote.prefetch(group['range_start'], group['range_bytes'])
                    for batch in file.iter_batches(batch_size=1024, row_groups=[rg], use_threads=False):
                        rows = batch.to_pylist()
                        # Predict a conservative native write bound before any
                        # feature/index writes in this batch. Hard SQLite page
                        # cap and total reservation remain in force as well.
                        bound = sum(4 * len(row['geometry']) + 16384 for row in rows) + 2 * MiB
                        reservation.check_write(bound, temporary)
                        for row in rows:
                            b = row['bbox']
                            if b['xmin'] > bbox[2] or b['ymin'] > bbox[3] or b['xmax'] < bbox[0] or b['ymax'] < bbox[1]:
                                continue
                            geometry = from_wkb(row['geometry'])
                            if geometry.geom_type != 'Polygon' or geometry.has_z or geometry.is_empty:
                                raise RuntimeError('Unexpected non-2D-polygon building schema; preserve checkpoint')
                            if not (120 < geometry.bounds[0] < geometry.bounds[2] < 135 and 25 < geometry.bounds[1] < geometry.bounds[3] < 45):
                                raise RuntimeError('Unexpected building coordinate convention')
                            if not all(math.isclose(actual, float(b[key]), abs_tol=1e-9, rel_tol=0)
                                       for actual, key in zip(geometry.bounds, ['xmin', 'ymin', 'xmax', 'ymax'])):
                                raise RuntimeError('Building geometry disagrees with indexed bbox; source schema requires inspection')
                            invalid = not geometry.is_valid
                            if not invalid and not geometry.intersects(region):
                                continue
                            # Invalid geometries remain explicitly retained if
                            # bbox-selected; their exact selection is uncertain.
                            transformed = transform(project, geometry)
                            value = row['height']
                            unresolved = value is None or not math.isfinite(value) or value <= 0
                            feature = ogr.Feature(layer.GetLayerDefn())
                            feature.SetGeometry(ogr.CreateGeometryFromWkb(transformed.wkb))
                            for key, value in {'source': str(row['source']), 'source_id': str(row['id']),
                                               'estimated': 1, 'unresolved': int(unresolved),
                                               'invalid_geometry': int(invalid), 'selection_uncertain': int(invalid),
                                               'height_reference': 'AGL', 'height_units': 'm', 'source_row_group': rg}.items():
                                feature.SetField(key, value)
                            if not unresolved:
                                feature.SetField('height_m', row['height'])
                            variance = row['var']
                            if variance is not None and math.isfinite(variance):
                                feature.SetField('height_var', variance)
                            layer.CreateFeature(feature)
                            _feature_digest_update(stored_rows_digest, feature)
                            counts['retained'] += 1
                            counts['invalid'] += invalid
                            counts['unresolved'] += unresolved
                            counts['holes'] += bool(geometry.interiors)
                        reservation.observe()
                        if temporary.stat().st_size > plan['output_bound_bytes']:
                            raise RuntimeError('Building output exceeded predeclared artifact bound')
                    report = {'counts': dict(counts), 'stored_rows_sha256': stored_rows_digest.hexdigest(),
                              'ranges': remote.reads[before_ranges:],
                              'received_bytes': remote.bytes_read - before_bytes,
                              'prefetch': prefetch, 'decode_batch_rows': 1024}
                    encoded = _canonical(report).replace("'", "''")
                    reservation.check_write(2 * MiB, temporary)
                    ds.ExecuteSQL(f"INSERT INTO acquisition_checkpoint VALUES ({rg},'{recipe_hash}','{encoded}')")
                    ds.CommitTransaction()
                    completed[rg] = report
                except BaseException as error:
                    try:
                        ds.RollbackTransaction()
                    except RuntimeError:
                        # SQLite may have rolled back automatically at its hard
                        # page cap; the previous completed groups stay intact.
                        pass
                    if 'database or disk is full' in str(error).lower():
                        raise AcquisitionError('storage_blocked',
                            'Native building artifact page limit reached; committed row groups preserved') from error
                    raise
                finally:
                    remote.clearcache()
                reservation.observe()
                if progress:
                    progress({'row_group': rg, 'completed_groups': len(completed),
                              'selected_groups': len(groups), 'received_bytes': remote.bytes_read,
                              'features_retained': sum(r['counts'].get('retained', 0) for r in completed.values())})
            ds = None
            audit = validate_buildings(temporary)
            report = {**audit, 'recipe_sha256': recipe_hash, 'recipe': recipe,
                      'identity': remote.identity, 'selected_row_groups': recipe['selected_groups'],
                      'row_groups_total': len(all_groups), 'checkpoint_groups': completed,
                      'http_bytes_downloaded': remote.bytes_read,
                      'decode_memory_bound_bytes': decode_memory, 'decode_batch_rows': 1024,
                      'planned_physical_range_bytes': range_transfer,
                      'ranges_this_invocation': remote.reads,
                      'source_catalogue': 'https://source.coop/tge-labs/globalbuildingatlas-lod1',
                      'original_dataset': 'https://github.com/zhu-xlab/GlobalBuildingAtlas',
                      'accessed_utc': datetime.now(timezone.utc).isoformat(),
                      'source_date': '2025-09 conversion; mixed footprints; mainly 2019 height imagery',
                      'height_units': 'm', 'height_reference': 'AGL', 'height_estimated': True,
                      'crs': 'EPSG:5186', 'source_crs': 'OGC:CRS84',
                      'licences': ['ODbL for OSM/Microsoft footprints', 'CC BY-NC 4.0 for other footprints and heights'],
                      'deployment_licence_review': 'required',
                      'limitations': ['Distribution coverage does not establish real-world completeness.',
                                      'Invalid geometries are explicitly retained without repair.',
                                      'Missing or nonpositive heights remain unresolved.',
                                      'Not surveyed roof elevations or field-verified visibility.']}
            _write_json(pending_report, report, budget, reservation)
            os.replace(temporary, output)
            os.replace(pending_report, sidecar)
            reservation.observe()
            return report
        finally:
            ds = None
