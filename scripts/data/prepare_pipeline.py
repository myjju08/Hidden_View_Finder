"""Resumable 5 m terrain/obstruction tiles using the existing bounded builders.

This prepares input surfaces, without computing visibility. Unsupported terrain
remains NoData; no visibility or building-completeness claim is introduced.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
from itertools import chain
import json
import math
import os
from pathlib import Path
import resource
import sqlite3
import sys
import uuid

from seoul_visibility.acquisition_safety import Budget, AcquisitionError, REPORT_RESERVE, atomic_json, run_bounded, sha256
from seoul_visibility.resources import memory_preflight

MiB = 1024 ** 2
TILE_OWNED_PATHS = ['terrain-work', 'dtm.tif', 'terrain_quality.tif', 'contract_mask.tif',
    'surface.tif', 'occupancy.tif', 'quality.tif', 'terrain.json', 'surface.json', 'tile.json',
    'terrain.json.writing', 'surface.json.writing', 'tile.json.writing']


def _key(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _worker_memory():
    return {'observed_peak_rss_bytes': int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) *
            (1 if sys.platform == 'darwin' else 1024),
            'measurement': 'OS process maximum resident set, including native libraries; not a sampled estimate'}


def retained_preparation_bound(grids, sample_index_cap, halo_m, margin=1.25):
    """No-compression whole-run estimate, separate from enforced stage limits."""
    if margin < 1.25:
        raise ValueError('Preparation uncertainty margin must be at least 25%')
    def padded_cells(grid):
        return math.ceil(grid['width'] / 256) * 256 * math.ceil(grid['height'] / 256) * 256
    # Final tile: Float32 DTM/surface + Byte terrain quality, contract,
    # occupancy and combined quality. Retained halo: Float32 DTM + two Bytes.
    raster_bytes = sum(padded_cells(grid) * 12 + padded_cells(_halo_grid(grid, halo_m)) * 6
                       for grid in grids)
    log_bytes = (len(grids) + 1) * 2 * MiB
    metadata_bytes = 32 * MiB
    retained = raster_bytes + sample_index_cap + log_bytes + metadata_bytes
    retained_with_margin = math.ceil(retained * margin)
    tile_writer = math.ceil((480 * MiB + 64 * MiB + 2 * MiB) * margin) + REPORT_RESERVE
    index_writer = math.ceil((2 * sample_index_cap + 32 * MiB + 64 * MiB + 2 * MiB) * margin) + REPORT_RESERVE
    return {'requested_tile_count': len(grids), 'tile_metres': 5000, 'cell_spacing_m': 5,
        'raster_uncompressed_block_padded_bytes': raster_bytes,
        'sample_index_cap_bytes': sample_index_cap, 'bounded_worker_logs_bytes': log_bytes,
        'metadata_allowance_bytes': metadata_bytes, 'retained_bytes_before_uncertainty': retained,
        'uncertainty_factor': margin, 'retained_bytes_with_uncertainty': retained_with_margin,
        'conservative_peak_incremental_growth_bytes': max(index_writer, retained_with_margin + tile_writer),
        'derivation': 'Requested full-support tiles; 256x256 storage-block padding; 12 bytes/final cell plus 6 bytes/halo cell; no compression savings; one sample index, capped logs and metadata; at least 25% uncertainty.',
        'scope': 'Successful preparation without accumulated failed attempts. Existing files and failed attempts remain in shared accounting and can stop later stages.',
        'enforcement': 'Whole-run estimate does not reserve or authorize unlimited writes. Every index/tile still obtains the unchanged locked stage reservation and live headroom checks.'}


def tile_grids(bounds, resolution=5, tile_metres=5000):
    """Fixed-origin tiles cover the full contract rectangle without coarsening."""
    if resolution != 5:
        raise ValueError('Citywide terrain preparation preserves the existing 5 m convention')
    if tile_metres <= 0 or tile_metres % resolution:
        raise ValueError('Tile metres must be a positive multiple of grid resolution')
    xmin, ymin, xmax, ymax = bounds
    for iy in range(math.floor(ymin / tile_metres), math.ceil(ymax / tile_metres)):
        for ix in range(math.floor(xmin / tile_metres), math.ceil(xmax / tile_metres)):
            left, bottom = ix * tile_metres, iy * tile_metres
            right, top = left + tile_metres, bottom + tile_metres
            yield {'id': f'x{ix}_y{iy}', 'crs': 'EPSG:5186', 'resolution_m': resolution,
                   'bounds': [left, bottom, right, top],
                   'width': tile_metres // resolution, 'height': tile_metres // resolution,
                   'transform': [left, resolution, 0, top, 0, -resolution]}


def validate_tile(directory: Path, recipe_sha256=None):
    return _validate_products(directory, 'tile.json', recipe_sha256)


def _validate_products(directory: Path, manifest, recipe_sha256=None):
    from osgeo import gdal

    record = json.loads((directory / manifest).read_text())
    if recipe_sha256 and record['recipe_sha256'] != recipe_sha256:
        raise ValueError('Terrain tile inputs/settings changed; select a new version')
    grid = record['grid']
    for name, product in record['products'].items():
        relative = Path(product['path'])
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('Tile product path must stay inside its manifest directory')
        path = directory / relative
        if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
            raise ValueError('Tile product symlink escape is forbidden')
        if sha256(path) != product['sha256']:
            raise ValueError(f'Corrupt terrain tile {name}; preserved for inspection')
        ds = gdal.Open(str(path))
        if ds is None or ds.RasterXSize != grid['width'] or ds.RasterYSize != grid['height']:
            raise ValueError('Terrain tile is unparseable or has unexpected dimensions')
        if list(ds.GetGeoTransform()) != grid['transform']:
            raise ValueError('Terrain tile grid alignment changed')
        srs = ds.GetSpatialRef()
        if srs is None or srs.GetAuthorityCode(None) != '5186':
            raise ValueError('Terrain tile CRS is missing or changed')
        ds = None
    return record


def _halo_grid(grid, halo_m):
    result = dict(grid)
    halo_cells = math.ceil(halo_m / grid['resolution_m'])
    halo = halo_cells * grid['resolution_m']
    left, bottom, right, top = grid['bounds']
    result.update(bounds=[left - halo, bottom - halo, right + halo, top + halo],
                  width=grid['width'] + 2 * halo_cells, height=grid['height'] + 2 * halo_cells,
                  transform=[left - halo, grid['resolution_m'], 0, top + halo, 0, -grid['resolution_m']])
    return result


def _crop_raster(source, output, source_grid, target_grid):
    """Exact integer window copy; no resampling or change in resolution."""
    from osgeo import gdal

    if Path(output).exists() or Path(output).is_symlink():
        raise ValueError('Existing raster crop preserved; use a fresh or recovered attempt')
    r = target_grid['resolution_m']
    offsets = [(target_grid['bounds'][0] - source_grid['bounds'][0]) / r,
               (source_grid['bounds'][3] - target_grid['bounds'][3]) / r]
    if source_grid['resolution_m'] != r or any(v != int(v) for v in offsets):
        raise ValueError('Surface crop must preserve exact grid alignment')
    dataset = gdal.Translate(str(output), str(source), format='GTiff',
        srcWin=[int(offsets[0]), int(offsets[1]), target_grid['width'], target_grid['height']],
        creationOptions=['TILED=YES', 'BLOCKXSIZE=256', 'BLOCKYSIZE=256', 'COMPRESS=DEFLATE', 'NUM_THREADS=1'])
    if dataset is None:
        raise RuntimeError('Surface window copy failed')
    dataset.FlushCache()
    dataset = None


def _product(path, relative=None):
    return {'path': relative or path.name, 'bytes': path.stat().st_size, 'sha256': sha256(path)}


def _preserve_unvalidated(directory, names, budget, label, recipe_sha256, ownership_manifest=None):
    """Move orphan task outputs aside under the writer lock; never overwrite them.

    The durable intent lists each move before it occurs. A crash during these
    renames leaves both the completed moves and remaining originals recoverable;
    a retry uses another unique directory and never reuses the interrupted one.
    Preserved bytes remain charged to the shared total and temporary budgets.
    """
    if budget._reservation is None and not budget._checkpoint_active:
        with budget.checkpoint_writer():
            return _preserve_unvalidated(directory, names, budget, label, recipe_sha256, ownership_manifest)
    directory = budget.safe_path(directory)
    paths = []
    moved_bytes = 0
    for name in names:
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('Preservation paths must stay inside the task directory')
        path = directory / relative
        if path.exists() or path.is_symlink():
            path = budget.safe_path(path)
            info = path.lstat()
            inventory = []
            for entry_count, entry in enumerate(chain((path,), path.rglob('*') if path.is_dir() else ())):
                if entry_count >= 512:
                    raise ValueError('Interrupted output inventory exceeds 512 entries; preserved in place for inspection')
                budget.safe_path(entry)
                stat = entry.lstat()
                moved_bytes += max(stat.st_size, getattr(stat, 'st_blocks', 0) * 512)
                if entry.is_file():
                    if len(inventory) >= 256:
                        raise ValueError('Interrupted output has more than 256 files; preserve it in place for inspection')
                    inventory.append({'path': str(entry.relative_to(directory)),
                                      'bytes': stat.st_size, 'sha256': sha256(entry)})
            paths.append({'path': name, 'kind': 'directory' if path.is_dir() else 'file',
                          'bytes': info.st_size if path.is_file() else None,
                          'device': info.st_dev, 'inode': info.st_ino, 'file_inventory': inventory})
    if not paths:
        return None
    owner_path = budget.safe_path(ownership_manifest or directory / 'owner.json')
    if not owner_path.is_file():
        raise ValueError('Unrecognized interrupted outputs preserved in place: missing prior ownership receipt')
    owner = json.loads(owner_path.read_text())
    if owner.get('recipe_sha256') != recipe_sha256:
        raise ValueError('Interrupted output ownership recipe changed; preserved in place')
    for entry in paths:
        authorized_directory = owner.get('owns_directory') is True and owner_path.parent == directory / entry['path']
        authorized_path = owner_path.parent == directory and entry['path'] in owner.get('owned_relative_paths', [])
        if not (authorized_directory or authorized_path):
            raise ValueError('Unrecognized interrupted output is absent from prior ownership receipt; preserved in place')
    preserved = directory / ('.' + label + '-interrupted-' + uuid.uuid4().hex + '.partial')
    # Conservatively charge every moved byte as newly temporary, even if it was
    # already staging. Retained canonical halves cannot evade the staging cap.
    budget.check(additional=65536, temporary=moved_bytes + 65536)
    preserved.mkdir()
    record = {'recipe_sha256': recipe_sha256, 'status': 'preserving_unvalidated_outputs',
        'reason': 'Interrupted stage has no validated successor checkpoint; original bytes retained',
        'ownership_receipt_sha256': sha256(owner_path),
        'entries': paths, 'completed': [], 'deletion_performed': False}
    journal = preserved / 'preservation.json'
    atomic_json(journal, record, budget, checkpoint=True)
    for entry in paths:
        source = directory / entry['path']
        destination = preserved / entry['path']
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise ValueError('Preservation destination unexpectedly exists')
        os.rename(source, destination)
        record['completed'].append(entry['path'])
        atomic_json(journal, record, budget, checkpoint=True)
    record['status'] = 'preserved'
    atomic_json(journal, record, budget, checkpoint=True)
    return preserved


def _validate_index_bundle(directory, recipe_sha256):
    index, info = directory / 'terrain_samples.sqlite', directory / 'terrain_samples.json'
    owner_path = directory / 'owner.json'
    for path in [index, info, owner_path]:
        if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
            raise ValueError('Terrain index symlink escape is forbidden')
    inspected = json.loads(info.read_text())
    owner = json.loads(owner_path.read_text())
    if owner.get('recipe_sha256') != recipe_sha256 or owner.get('owns_directory') is not True:
        raise ValueError('Terrain sample-index bundle ownership is not established')
    if inspected.get('recipe_sha256') != recipe_sha256 or inspected.get('sha256') != sha256(index):
        raise ValueError('Shared terrain sample index changed; existing source preserved')
    with sqlite3.connect(f'file:{index}?mode=ro', uri=True) as connection:
        if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('Terrain sample index integrity validation failed')
    return inspected


def _publish_index_bundle(attempt, final, recipe_sha256, budget):
    """Publish the validated SQLite/receipt pair with one same-filesystem rename."""
    with budget.checkpoint_writer():
        budget.safe_path(attempt)
        budget.safe_path(final)
        inspected = _validate_index_bundle(attempt, recipe_sha256)
        if final.exists() or final.is_symlink():
            raise ValueError('Existing terrain sample-index bundle preserved')
        budget.check(additional=16384, checkpoint=True)
        if attempt.stat().st_dev != final.parent.stat().st_dev:
            raise ValueError('Index publication requires a same-filesystem atomic rename')
        os.rename(attempt, final)
        return inspected


def _resume_index_bundle(output, recipe_sha256, budget):
    """Reuse a valid canonical pair or promote a completed interrupted attempt."""
    final = budget.safe_path(output / 'sample-index')
    if final.exists():
        if all((final / name).exists() for name in ['terrain_samples.sqlite', 'terrain_samples.json']):
            return _validate_index_bundle(final, recipe_sha256)
        _preserve_unvalidated(output, ['sample-index'], budget, 'index', recipe_sha256, final / 'owner.json')
    attempts = sorted(output.glob('sample-index.*.partial'))
    if len(attempts) > 128:
        raise AcquisitionError('storage_blocked', 'More than 128 retained index attempts require inspection; no source deleted')
    for attempt in attempts:
        budget.safe_path(attempt)
        if all((attempt / name).is_file() for name in ['terrain_samples.sqlite', 'terrain_samples.json']):
            return _publish_index_bundle(attempt, final, recipe_sha256, budget)
    return None


def _register_tile_owner(staging, recipe_sha256, budget):
    """Register fixed native output names before any tile data writer starts."""
    owner_path = staging / 'owner.json'
    expected = {'recipe_sha256': recipe_sha256, 'kind': 'citywide_prepare_tile',
                'owned_relative_paths': TILE_OWNED_PATHS}
    if owner_path.exists():
        if json.loads(owner_path.read_text()) != expected:
            raise ValueError('Existing tile ownership receipt changed; outputs preserved in place')
        return
    existing = list(staging.iterdir())
    pending = staging / 'owner.json.writing'
    if existing:
        # No native output is authorized before this first receipt completes.
        if existing != [pending] or json.loads(pending.read_text()) != expected:
            raise ValueError('Unrecognized existing tile workspace preserved in place; no ownership receipt')
        budget.check(additional=8192)
        os.rename(pending, owner_path)
        return
    atomic_json(owner_path, expected, budget, checkpoint=True)


def _cleanup_owned(root, owned, reason, budget):
    """Delete only enumerated task intermediates after callers verify successors."""
    records = []
    for value in owned:
        path = budget.safe_path(value)
        if not path.is_relative_to(root) or path.is_symlink():
            raise ValueError('Disposable intermediate escaped its task-owned attempt')
        if path.is_file():
            entry = {'path': str(path.relative_to(root)), 'bytes': path.stat().st_size,
                     'sha256': sha256(path), 'reason': reason}
            records.append(entry)
    journal = root / ('cleanup.' + uuid.uuid4().hex + '.json')
    atomic_json(journal, {'entries': records, 'completed': []}, budget, checkpoint=True)
    completed = []
    for entry in records:
        (root / entry['path']).unlink()
        completed.append(entry['path'])
        atomic_json(journal, {'entries': records, 'completed': completed}, budget, checkpoint=True)
    return records


def _native_page_cap(dataset, maximum):
    result = dataset.ExecuteSQL('PRAGMA page_size')
    page_size = int(result.GetNextFeature().GetField(0))
    dataset.ReleaseResultSet(result)
    result = dataset.ExecuteSQL(f'PRAGMA max_page_count={maximum // page_size}')
    effective = int(result.GetNextFeature().GetField(0))
    dataset.ReleaseResultSet(result)
    if effective * page_size > maximum:
        raise RuntimeError('Native GeoPackage page limit was not applied')
    for sql in ['PRAGMA cache_size=-16384', 'PRAGMA temp_store=MEMORY', 'PRAGMA journal_mode=DELETE']:
        result = dataset.ExecuteSQL(sql)
        if result is not None: dataset.ReleaseResultSet(result)


def _subset_buildings(source_path, output, grid, pressure, maximum=128 * MiB, batch_size=512):
    """Indexed rectangle read preserves complete footprints and source fields."""
    from osgeo import gdal, ogr

    source = gdal.OpenEx(str(source_path), gdal.OF_VECTOR)
    if not isinstance(batch_size, int) or not 1 <= batch_size <= 512:
        raise ValueError('Building subset batch size must remain bounded at 1..512')
    if source is None: raise ValueError('Building source cannot be opened')
    layer = source.GetLayerByName('buildings')
    srs = layer.GetSpatialRef()
    if srs is None or srs.GetAuthorityCode(None) != '5186':
        raise ValueError('Building surface input CRS must already be inspected EPSG:5186')
    definition = layer.GetLayerDefn()
    if any(definition.GetFieldIndex(name) < 0 for name in ['source_id', 'height_m', 'height_var', 'invalid_geometry']):
        raise ValueError('Building height/provenance schema is incomplete')
    if output.exists(): raise ValueError('Existing surface subset preserved; select a fresh attempt')
    layer.SetSpatialFilterRect(*grid['bounds'])
    target_ds = ogr.GetDriverByName('GPKG').CreateDataSource(str(output))
    _native_page_cap(target_ds, maximum)
    target = target_ds.CreateLayer('buildings', srs, layer.GetGeomType(), options=['SPATIAL_INDEX=YES'])
    for i in range(definition.GetFieldCount()): target.CreateField(definition.GetFieldDefn(i))
    count = invalid = unresolved = 0
    pressure()
    target_ds.StartTransaction()
    try:
        for feature in layer:
            geometry = feature.GetGeometryRef()
            if geometry is None or geometry.IsEmpty() or geometry.WkbSize() > 1024 * 1024:
                raise ValueError('Empty or oversized building footprint requires inspected bounded handling')
            out = ogr.Feature(target.GetLayerDefn())
            out.SetFrom(feature)
            out.SetFID(feature.GetFID())
            target.CreateFeature(out)
            out = None
            count += 1
            invalid += int(bool(feature.GetField('invalid_geometry')))
            unresolved += int(feature.GetField('height_m') is None)
            if count % batch_size == 0:
                target_ds.CommitTransaction()
                pressure()
                target_ds.StartTransaction()
        target_ds.CommitTransaction()
        target_ds.FlushCache()
        pressure()
    finally:
        target = target_ds = layer = source = None
    with sqlite3.connect(f'file:{output}?mode=ro', uri=True) as db:
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('Surface building subset failed SQLite integrity validation')
    return {'features': count, 'invalid_source_geometries': invalid, 'missing_height_m': unresolved,
            'bounds_epsg5186': grid['bounds'], 'whole_footprints_preserved': True,
            'source_fid_preserved': True, 'feature_batch_size': batch_size, **_product(output)}


def _source_domains(geometry_path):
    from pyproj import Transformer
    from shapely import prepare
    from shapely.geometry import shape
    from shapely.ops import transform

    geometries = {f['properties']['role']: shape(f['geometry'])
                  for f in json.loads(Path(geometry_path).read_text())['features']}
    project = Transformer.from_crs(4326, 5186, always_xy=True).transform
    recommendation = transform(project, geometries['recommendation'])
    support = transform(project, geometries['obstruction_support'])
    prepare(recommendation)
    prepare(support)
    return recommendation, support


def _source_aware_terrain(settings, grid, raw, geometry_path, pressure):
    """Skip interpolation only when every halo cell is outside the source domain."""
    from shapely.geometry import box
    from seoul_visibility.terrain import prepare_terrain, create_raster, NODATA

    if Path(raw).exists() or Path(raw).is_symlink():
        raise ValueError('Existing raw terrain preserved; use a fresh or recovered attempt')
    recommendation, _ = _source_domains(geometry_path)
    if box(*grid['bounds']).disjoint(recommendation):
        pressure()
        dataset = create_raster(raw, grid)
        dataset.GetRasterBand(1).Fill(NODATA)
        dataset.FlushCache()
        dataset = None
        pressure()
        return {'kind': 'source_domain_nodata', 'valid_pixels': 0, 'interpolation_performed': False,
            'reason': 'Entire footprint-terrain halo rectangle is disjoint from the inspected Seoul-only terrain domain',
            'grid_bounds_epsg5186': grid['bounds'], 'geometry_sha256': sha256(geometry_path),
            'coverage_preserved': 'Full requested grid retained with NoData; no synthetic elevations supplied'}
    return prepare_terrain(settings, grid, raw, pressure)


def _worker(job):
    from osgeo import gdal
    from seoul_visibility.terrain import _build_sample_index, prepare_terrain

    budget = Budget(job['root'], extra_roots=job['extra_roots'], limit=job['total_limit'],
                    min_free=job['minimum_free_bytes'], stage_root=job['stage_root'],
                    stage_limit=job['temporary_limit'],
                    additional_accounted_bytes=job['additional_accounted_bytes'], inherited_reservation=True)
    # This is a per-file native backstop in addition to the parent's complete
    # simultaneous-file bound, reservation, and monitored free-space reserve.
    resource.setrlimit(resource.RLIMIT_FSIZE, (job['per_file_limit'], job['per_file_limit']))
    address_limit = int(job.get('memory_limit_bytes', 2 * 1024 ** 3))
    previous_soft, previous_hard = resource.getrlimit(resource.RLIMIT_AS)
    if previous_hard != resource.RLIM_INFINITY:
        address_limit = min(address_limit, previous_hard)
    if previous_soft != resource.RLIM_INFINITY:
        address_limit = min(address_limit, previous_soft)
    resource.setrlimit(resource.RLIMIT_AS, (address_limit, address_limit))
    gdal.UseExceptions()
    gdal.SetCacheMax(32 * MiB)
    gdal.SetConfigOption('GDAL_NUM_THREADS', '1')
    gdal.SetConfigOption('OGR_SQLITE_CACHE', '16')
    gdal.SetConfigOption('OGR_SQLITE_JOURNAL', 'DELETE')
    def pressure():
        budget.check(additional=4 * MiB)
    if job['operation'] == 'index':
        import numpy as np
        from seoul_visibility.terrain import _validation_spots, _local_points, _linear_supported

        path = budget.safe_path(job['output'])
        if path.exists() or Path(job['report']).exists():
            raise ValueError('Existing terrain index attempt preserved; reuse its validated bundle or select a fresh attempt')
        details = _build_sample_index(job['terrain'], job['grid'], path, pressure)
        with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as connection:
            if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise RuntimeError('Terrain sample index failed SQLite integrity validation')
            holdouts, selection = _validation_spots(connection, 20, 0)
            residuals = []
            unsupported = 0
            halo = job['terrain']['halo_m']
            for x, y, z in holdouts:
                pressure()
                points = _local_points(connection, (x - halo, y - halo, x + halo, y + halo),
                                       job['terrain']['max_points_per_tile'], exclude_holdout=True)
                predicted = _linear_supported(points, np.array([[x, y]]),
                                              job['terrain']['max_triangle_edge_m'])[0]
                if math.isfinite(predicted): residuals.append(float(predicted - z))
                else: unsupported += 1
            details['shared_heldout_spots'] = {**selection, 'evaluated': len(residuals),
                'unsupported': unsupported,
                'rmse_m': math.sqrt(sum(r * r for r in residuals) / len(residuals)) if residuals else None,
                'bias_m': sum(residuals) / len(residuals) if residuals else None,
                'scope': 'One shared diagnostic across source extent, not repeated for every product tile'}
        atomic_json(job['report'], {**details, 'worker_memory': _worker_memory(), 'sha256': sha256(path),
                    'recipe_sha256': job['recipe_sha256']}, budget, checkpoint=True)
        return
    staging = budget.safe_path(job['staging'])
    staging.mkdir(parents=True, exist_ok=True)
    _register_tile_owner(staging, job['recipe_sha256'], budget)
    _preserve_unvalidated(staging, ['terrain.json.writing', 'surface.json.writing', 'tile.json.writing'],
                          budget, 'metadata', job['recipe_sha256'])
    if (staging / 'terrain.json').exists():
        record = _validate_products(staging, 'terrain.json', job['recipe_sha256'])
        # The expanded ground is retained for footprint-wide base estimation,
        # so a surface interruption does not repeat a successful terrain build.
        auxiliary = staging / record['terrain_halo']['path']
        if not auxiliary.exists() or sha256(auxiliary) != record['terrain_halo']['sha256']:
            raise ValueError('Checkpointed footprint-halo terrain is missing or corrupt')
    else:
        _preserve_unvalidated(staging, ['terrain-work', 'dtm.tif', 'terrain_quality.tif',
            'contract_mask.tif', 'surface.tif', 'occupancy.tif', 'quality.tif', 'surface.json'],
            budget, 'terrain', job['recipe_sha256'])
        work = staging / 'terrain-work'
        work.mkdir(exist_ok=True)
        raw = work / 'raw_dtm.part.tif'
        details = _source_aware_terrain(job['terrain'], job['halo_grid'], raw, job['geometry'], pressure)
        _mask_terrain(raw, work, job['halo_grid'], job['geometry'], pressure)
        # DTM and roof estimation use the same ground samples. Cropping the
        # expanded product avoids triangulation changes at a second tile grid.
        for name, source_name in [('dtm', 'dtm'), ('terrain_quality', 'quality'), ('contract_mask', 'contract_mask')]:
            pressure()
            _crop_raster(work / (source_name + '.tif'), staging / (name + '.tif'), job['halo_grid'], job['grid'])
        record = _terrain_counts(staging, job['grid'])
        record.update(recipe_sha256=job['recipe_sha256'], terrain_processing=details,
            effective_terrain_window_pixels=job['terrain']['tile_size'],
            terrain_halo=_product(work / 'dtm.tif', 'terrain-work/dtm.tif'),
            vertical_reference=job['vertical_reference'], vertical_conversion='none',
            visibility_ready=False, field_verified=False, cleanup=[],
            terrain_halo_grid=job['halo_grid'],
            products={name: _product(staging / (name + '.tif')) for name in ['dtm', 'terrain_quality', 'contract_mask']})
        atomic_json(staging / 'terrain.json', record, budget, checkpoint=True)
        _validate_products(staging, 'terrain.json', job['recipe_sha256'])
        # The exact owned raw path was registered before the native writer ran.
        record['cleanup'] += _cleanup_owned(staging, job['owned_disposable'], 'Validated masked/cropped DTM and footprint-halo successor exist', budget)
        atomic_json(staging / 'terrain.json', record, budget, checkpoint=True)
    surface = _surface_stage(job, staging, record, budget, pressure)
    record.update(products={**record['products'], **surface['products']}, worker_memory=_worker_memory(),
        effective_terrain_window_pixels=job['terrain']['tile_size'], window_selection=job.get('window_selection'),
        surface_processing=surface, quality_bits=surface['quality_bits'],
        contract_mask_bits={'inside_requested_support': 1, 'inside_seoul': 2},
        limitations=['NoData beyond the inspected Seoul-only terrain domain.',
            '5 m cell spacing does not establish 5 m accuracy.',
            'Existing within-tile seam probes run; cross-product-tile seams are not independently certified.',
            'Footprint maximum DTM base plus estimated AGL is a screening roof overestimate, not surveyed absolute roof elevation.',
            'Building coverage bits describe the downloaded distribution, not real-world completeness.',
            'No observer-elevation or field visibility validation.'])
    atomic_json(staging / 'tile.json', record, budget, checkpoint=True)
    validate_tile(staging, job['recipe_sha256'])
    # Keep the reusable terrain checkpoint and expanded ground. They allow
    # independent surface repair without recomputing this validated DTM.


def _terrain_counts(directory, grid):
    import numpy as np
    from osgeo import gdal
    from seoul_visibility.terrain import iter_tiles

    quality = gdal.Open(str(directory / 'terrain_quality.tif'))
    mask = gdal.Open(str(directory / 'contract_mask.tif'))
    counts = {'requested_support_cells': 0, 'inside_seoul_cells': 0,
              'terrain_valid_support_cells': 0, 'terrain_valid_seoul_cells': 0}
    for window in iter_tiles(grid, 256):
        q = quality.GetRasterBand(1).ReadAsArray(*window).astype(bool)
        m = mask.GetRasterBand(1).ReadAsArray(*window)
        counts['requested_support_cells'] += int(np.count_nonzero(m & 1))
        counts['inside_seoul_cells'] += int(np.count_nonzero(m & 2))
        counts['terrain_valid_support_cells'] += int(np.count_nonzero(q & ((m & 1) != 0)))
        counts['terrain_valid_seoul_cells'] += int(np.count_nonzero(q & ((m & 2) != 0)))
    quality = mask = None
    return {'grid': grid, 'cell_counts': counts,
        'support_missing_area_m2': (counts['requested_support_cells'] - counts['terrain_valid_support_cells']) * grid['resolution_m'] ** 2,
        'seoul_missing_area_m2': (counts['inside_seoul_cells'] - counts['terrain_valid_seoul_cells']) * grid['resolution_m'] ** 2}


def _surface_stage(job, staging, terrain_record, budget, pressure):
    from seoul_visibility.prepare import _normalise_buildings, _prepare_surfaces
    from osgeo import gdal
    import numpy as np
    from seoul_visibility.terrain import iter_tiles

    if (staging / 'surface.json').exists():
        return _validate_products(staging, 'surface.json', job['recipe_sha256'])
    _preserve_unvalidated(staging, ['surface.tif', 'occupancy.tif', 'quality.tif', 'surface.json.writing'],
                          budget, 'surface', job['recipe_sha256'])
    # A fresh attempt preserves interrupted SQLite transactions for inspection.
    attempt = staging / ('surface-work-' + uuid.uuid4().hex)
    attempt.mkdir()
    owned = [attempt / name for name in ['subset.gpkg', 'subset.gpkg-journal', 'buildings.writing.gpkg',
        'buildings.writing.gpkg-journal', 'buildings.gpkg', 'buildings.gpkg-journal', '.building-dedup.sqlite',
        '.building-dedup.sqlite-journal', 'surface.tif', 'occupancy.tif', 'quality.tif']]
    atomic_json(attempt / 'owner.json', {'recipe_sha256': job['recipe_sha256'],
                'owned_disposable': [str(path.relative_to(staging)) for path in owned]}, budget, checkpoint=True)
    subset = _subset_buildings(job['buildings'], attempt / 'subset.gpkg', job['halo_grid'], pressure)
    buildings = {'path': str(attempt / 'subset.gpkg'), 'layer': 'buildings', 'crs': 'EPSG:5186',
        'height_field': 'height_m', 'height_is_estimated': True,
        'height_estimation_method': 'GBA estimated AGL; variance and original source ID retained in source package',
        'base_estimation_method': 'maximum',
        'base_estimation_justification': 'Existing conservative screening option: highest supported all-touched ground plus estimated AGL; unresolved whenever any footprint ground is missing.',
        'invalid_geometry': 'repair', 'maximum_building_window_cells': 1000000,
        'artifact_max_bytes': 128 * MiB, 'dedup_max_bytes': 16 * MiB, 'feature_batch_size': 512,
        'progress_after_commit_only': True,
        'preserve_intermediates': True, 'coverage_bounds': job['support_bounds'],
        'coverage_boundary': {'path': job['support_boundary'], 'crs': 'EPSG:4326'}}
    ground = staging / terrain_record['terrain_halo']['path']
    normalization = _normalise_buildings(buildings, job['halo_grid'], ground, attempt / 'buildings.gpkg', pressure)
    processing = _prepare_surfaces({'buildings': buildings, 'dtm_path': str(ground), 'surface_tile_size': 512},
                                  job['halo_grid'], attempt, pressure)
    products = {}
    for name in ['surface', 'occupancy', 'quality']:
        pressure()
        _crop_raster(attempt / (name + '.tif'), staging / (name + '.tif'), job['halo_grid'], job['grid'])
        products[name] = _product(staging / (name + '.tif'))
    # Aggregate only the final 5 km product, including its explicitly unknown
    # cells outside the contract; halo counts are reported separately above.
    counts = {key: 0 for key in processing['quality_bits']}
    source = gdal.Open(str(staging / 'quality.tif'))
    for window in iter_tiles(job['grid'], 256):
        values = source.GetRasterBand(1).ReadAsArray(*window)
        for key, bit in processing['quality_bits'].items(): counts[key] += int(np.count_nonzero(values & bit))
    source = None
    record = {'recipe_sha256': job['recipe_sha256'], 'grid': job['grid'], 'products': products,
        'quality_bits': processing['quality_bits'], 'pixel_counts': counts,
        'halo_processing': processing, 'building_normalization': normalization, 'subset': subset,
        'height_reference': 'Source height_m is estimated AGL metres; roof_m is estimated absolute elevation in the DTM reference.',
        'uncertainty': 'No uncertainty conversion; height_var and unresolved/invalid source features remain in the normalized source GeoPackage.',
        'footprint_terrain_halo_m': job['footprint_halo_m'],
        'outside_halo_policy': 'Footprints extending beyond the halo grid remain unresolved; no inferred outside ground.',
        'source_buildings_sha256': job['buildings_sha256'], 'field_verified': False, 'visibility_ready': False,
        'cleanup': []}
    atomic_json(staging / 'surface.json', record, budget, checkpoint=True)
    _validate_products(staging, 'surface.json', job['recipe_sha256'])
    record['cleanup'] = _cleanup_owned(staging, owned, 'Validated obstruction/occupancy/quality rasters exist; original indexed building source remains intact', budget)
    atomic_json(staging / 'surface.json', record, budget, checkpoint=True)
    return record


def _mask_terrain(raw, staging, grid, geometry_path, pressure):
    import numpy as np
    from osgeo import gdal
    from shapely import intersects_xy
    from seoul_visibility.terrain import create_raster, iter_tiles, read_valid, NODATA

    recommendation, support = _source_domains(geometry_path)
    names = {'dtm': gdal.GDT_Float32, 'quality': gdal.GDT_Byte, 'contract_mask': gdal.GDT_Byte}
    if any((staging / (name + '.tif')).exists() or (staging / (name + '.tif')).is_symlink() for name in names):
        raise ValueError('Existing terrain masks preserved; use a fresh or recovered attempt')
    source = gdal.Open(str(raw))
    outputs = {name: create_raster(staging / (name + '.tif'), grid, kind) for name, kind in names.items()}
    counts = {'requested_support_cells': 0, 'inside_seoul_cells': 0,
              'terrain_valid_support_cells': 0, 'terrain_valid_seoul_cells': 0}
    resolution = grid['resolution_m']
    try:
        for x, y, w, h in iter_tiles(grid, 512):
            pressure()
            values, valid = read_valid(source.GetRasterBand(1), x, y, w, h)
            xx = grid['transform'][0] + (x + np.arange(w) + .5) * resolution
            yy = grid['transform'][3] - (y + np.arange(h) + .5) * resolution
            mx, my = np.meshgrid(xx, yy)
            in_seoul = intersects_xy(recommendation, mx, my)
            in_support = intersects_xy(support, mx, my)
            # The source catalogue supplies Seoul terrain only. TIN support is
            # further constrained to that source domain, never extrapolated into
            # the surrounding recommendation support buffer.
            valid &= in_seoul & in_support
            values[~valid] = NODATA
            outputs['dtm'].GetRasterBand(1).WriteArray(values, x, y)
            outputs['quality'].GetRasterBand(1).WriteArray(valid.astype(np.uint8), x, y)
            mask = in_support.astype(np.uint8) | (in_seoul.astype(np.uint8) << 1)
            outputs['contract_mask'].GetRasterBand(1).WriteArray(mask, x, y)
            counts['requested_support_cells'] += int(in_support.sum())
            counts['inside_seoul_cells'] += int(in_seoul.sum())
            counts['terrain_valid_support_cells'] += int(valid.sum())
            counts['terrain_valid_seoul_cells'] += int((valid & in_seoul).sum())
    finally:
        for dataset in outputs.values(): dataset.FlushCache()
        outputs.clear()
        dataset = None
        source = None
    products = {name: {'path': name + '.tif', 'sha256': sha256(staging / (name + '.tif')),
                       'bytes': (staging / (name + '.tif')).stat().st_size} for name in names}
    return {'grid': grid, 'products': products, 'cell_counts': counts,
            'support_missing_area_m2': (counts['requested_support_cells'] - counts['terrain_valid_support_cells']) * resolution ** 2,
            'seoul_missing_area_m2': (counts['inside_seoul_cells'] - counts['terrain_valid_seoul_cells']) * resolution ** 2}


def prepare_tiles(pipeline):
    """Validate the source package, then prepare/checkpoint every requested tile."""
    from pyproj import Transformer
    from shapely.geometry import box
    from shapely.ops import transform
    from osgeo import gdal
    import numpy
    import scipy
    import shapely
    import pyproj
    from scripts.data.prepare_resources import (VERSION, window_policy, select_windows, processing_extents,
                                               approved_legacy, migrate_index, migrate_tile)

    pipeline.validate()  # Source package hashes/schemas precede native writes.
    budget = pipeline.budget
    source = budget.safe_path(pipeline.norm / 'terrain.gpkg')
    source_info = json.loads(source.with_suffix('.source.json').read_text())
    if sha256(source) != source_info['sha256']:
        raise ValueError('Normalized terrain source checksum changed')
    g = pipeline.geometries()
    buildings = budget.safe_path(pipeline.norm / 'buildings.gpkg')
    building_info = json.loads(buildings.with_suffix('.source.json').read_text())
    if sha256(buildings) != building_info['sha256']:
        raise ValueError('Normalized building source checksum changed')
    if building_info['recipe']['support_sha256'] != hashlib.sha256(g['obstruction_support'].wkb).hexdigest():
        raise ValueError('Building source support geometry differs from the requested contract')
    if set(map(int, building_info['checkpoint_groups'])) != set(building_info['selected_row_groups']):
        raise ValueError('Building source has incomplete selected row groups; coverage cannot be certified')
    project = Transformer.from_crs(4326, 5186, always_xy=True).transform
    support = transform(project, g['obstruction_support'])
    geometry_path = pipeline.data / 'geometry' / f'extents.{pipeline.config_id}.geojson'
    c = pipeline.config
    settings = {'kind': 'samples', 'sources': [{'path': str(source), 'layer': layer,
                    'elevation_field': 'elevation_m', 'crs': 'EPSG:5186'} for layer in ['contours', 'spots']],
                'contour_sampling': 'regular_arclength', 'sample_spacing_m': c['terrain']['sample_spacing_m'],
                'max_triangle_edge_m': c['terrain']['max_triangle_edge_m'],
                'halo_m': c['terrain_interpolation_halo_m'], 'tile_size': c['terrain']['tile_size'],
                'max_sample_points': c['terrain']['max_sample_points'],
                'max_points_per_tile': c['terrain']['max_points_per_tile'],
                'max_feature_vertices': 200000, 'max_validation_spots': 0,
                'sample_index_max_bytes': c['terrain']['max_sample_points'] * 256 + 16 * MiB}
    memory_preflight(max(settings['max_points_per_tile'] * 400 + settings['tile_size'] ** 2 * 300,
                         512 ** 2 * 800))
    recipe = {'source_sha256': source_info['sha256'], 'buildings_sha256': building_info['sha256'],
              'geometry_sha256': sha256(geometry_path),
              'settings': settings, 'resolution_m': c['resolution_m'], 'tile_metres': 5000,
              'footprint_terrain_halo_m': c['terrain_interpolation_halo_m'],
              'building_base_method': 'maximum supported whole-footprint DTM; estimated AGL remains estimated',
              'surface_native_stage_cap_bytes': 480 * MiB,
              'processing_version': VERSION, 'window_selection_policy': window_policy(settings),
              'implementation_sha256': {str(path.relative_to(budget.root)): sha256(path) for path in [
                  Path(__file__).resolve(), Path(__file__).resolve().with_name('prepare_resources.py'),
                  budget.root / 'src/seoul_visibility/terrain.py',
                  budget.root / 'src/seoul_visibility/prepare.py']},
              'dependencies': {'gdal': gdal.VersionInfo('RELEASE_NAME'), 'numpy': numpy.__version__,
                  'scipy': scipy.__version__, 'shapely': shapely.__version__, 'pyproj': pyproj.__version__,
                  'python': list(sys.version_info[:3])}}
    recipe_hash = _key(recipe)
    legacy = approved_legacy(recipe, pipeline.data)
    output = budget.safe_path(pipeline.data / 'prepared' / recipe_hash[:16])
    index_bundle = output / 'sample-index'
    index = index_bundle / 'terrain_samples.sqlite'
    index_info = index_bundle / 'terrain_samples.json'
    # The existing engine unions all features in a coverage datasource. Export
    # exactly the 10 km role, never the three-role extents collection.
    from shapely.geometry import mapping
    support_boundary = output / 'building-support.geojson'
    atomic_json(support_boundary, {'type': 'FeatureCollection', 'features': [
        {'type': 'Feature', 'properties': {'role': 'obstruction_support'},
         'geometry': mapping(g['obstruction_support'])}]}, budget)
    grid_list = [grid for grid in tile_grids(support.bounds, c['resolution_m']) if box(*grid['bounds']).intersects(support)]
    source_domain = transform(project, g['recommendation'])
    declared_halo = transform(project, g['terrain_interpolation_support'])
    plan = {'recipe': recipe, 'recipe_sha256': recipe_hash, 'target': str(output),
            'resolution_m': c['resolution_m'], 'source_domain': 'Seoul-only; outside retained as NoData',
            'requested_support_bounds_epsg5186': list(support.bounds),
            'processing_extents': processing_extents(grid_list, c['terrain_interpolation_halo_m'],
                settings['halo_m'], source_domain, support, declared_halo),
            'legacy_reuse_candidate': {'recipe_sha256': legacy['recipe_sha256'],
                'plan_sha256': legacy['plan_sha256']} if legacy else None,
            'whole_preparation_estimate': retained_preparation_bound(grid_list, settings['sample_index_max_bytes'],
                c['terrain_interpolation_halo_m'], c['storage']['uncertain_incremental_margin']),
            'surface_resources': {'native_incremental_peak_bytes': 480 * MiB,
                'safety_margin': '25% plus 64 MiB monitoring lag and bounded log allowance',
                'subset_gpkg_cap_bytes': 128 * MiB, 'roof_gpkg_cap_bytes': 128 * MiB,
                'sqlite_journal_cap_bytes': 128 * MiB, 'dedup_index_and_journal_cap_bytes': 32 * MiB,
                'maximum_footprint_window_cells': 1000000, 'feature_batch_size': 512,
                'mask_and_surface_window_pixels': 512, 'terrain_interpolation_window_pixels': settings['tile_size'],
                'worker_address_space_cap_bytes': c['memory']['working_bytes'],
                'native_backstops': 'SQLite max_page_count and per-file RLIMIT_FSIZE; shared total is policy enforcement, not an OS disk quota'},
            'resume_command': f'bash scripts/data/python.sh scripts/data/citywide.py --config {pipeline.config_path.relative_to(Path(__file__).resolve().parents[2])} prepare'}
    atomic_json(output / 'plan.json', plan, budget)
    shared_job = {'root': str(budget.root), 'extra_roots': [str(p) for p in budget.roots if p != budget.root],
                  'total_limit': budget.limit, 'minimum_free_bytes': budget.min_free,
                  'temporary_limit': budget.stage_limit, 'additional_accounted_bytes': budget.additional_accounted_bytes,
                  'stage_root': str(budget.stage_root), 'recipe_sha256': recipe_hash,
                  'vertical_reference': source_info['vertical_reference'], 'geometry': str(geometry_path),
                  'buildings': str(buildings), 'buildings_sha256': building_info['sha256'],
                  'support_boundary': str(support_boundary), 'support_bounds': list(support.bounds),
                  'footprint_halo_m': c['terrain_interpolation_halo_m'],
                  'memory_limit_bytes': c['memory']['working_bytes']}
    environment = dict(os.environ)
    environment.update(GDAL_CACHEMAX='32', GDAL_NUM_THREADS='1', OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1',
                       TMPDIR=str(budget.stage_root / 'tmp'), XDG_CACHE_HOME=str(budget.stage_root / 'cache'),
                       PROJ_NETWORK='OFF', PYTHONDONTWRITEBYTECODE='1')
    for path in [budget.stage_root / 'tmp', budget.stage_root / 'cache']:
        budget.safe_path(path).mkdir(parents=True, exist_ok=True)
    def run(job, peak, timeout):
        identifier = uuid.uuid4().hex
        job_path = output / 'jobs' / (identifier + '.json')
        atomic_json(job_path, job, budget)
        result = run_bounded([sys.executable, '-m', 'scripts.data.prepare_pipeline', '--worker', str(job_path)],
            budget, peak_bytes=peak, temporary_bytes=peak, cwd=budget.root, env=environment,
            timeout=timeout, log_path=output / 'logs' / (identifier + '.log'))
        atomic_json(output / 'jobs' / (identifier + '.result.json'), result, budget)
        return result
    inspected = _resume_index_bundle(output, recipe_hash, budget)
    if inspected is None and legacy:
        inspected = migrate_index(legacy, index_bundle, recipe_hash, budget)
    if inspected is None:
        # If a prior attempt was interrupted, preserve its partial. Rebuilding
        # from the intact normalized source receives a fresh bounded reservation.
        attempt = uuid.uuid4().hex
        attempt_bundle = budget.safe_path(output / ('sample-index.' + attempt + '.partial'))
        with budget.checkpoint_writer():
            budget.check(additional=16384)
            attempt_bundle.mkdir()
            atomic_json(attempt_bundle / 'owner.json', {'recipe_sha256': recipe_hash,
                'kind': 'terrain_sample_index_bundle', 'owns_directory': True}, budget, checkpoint=True)
        partial = attempt_bundle / 'terrain_samples.sqlite'
        info = attempt_bundle / 'terrain_samples.json'
        job = {**shared_job, 'operation': 'index', 'terrain': settings,
               'grid': grid_list[0], 'output': str(partial), 'report': str(info),
               'per_file_limit': settings['sample_index_max_bytes'], 'owned_disposable': [str(partial)]}
        run(job, 2 * settings['sample_index_max_bytes'] + 32 * MiB, 3600)
        if not info.exists(): raise RuntimeError('Terrain index worker did not produce its checked manifest')
        inspected = _publish_index_bundle(attempt_bundle, index_bundle, recipe_hash, budget)
    # Runtime paths are not acquisition/interpolation settings. Do not mutate
    # the recipe object after its hash and durable plan have been published.
    settings = copy.deepcopy(settings)
    settings['sample_index_path'] = str(index)
    settings['sample_info_path'] = str(index_info)
    selections = select_windows(index, grid_list, settings, source_domain)
    atomic_json(output / 'window-plan.json', {'recipe_sha256': recipe_hash,
        'source_index_sha256': inspected['sha256'], 'policy': window_policy(settings),
        'tiles': selections}, budget)
    plan['effective_window_counts'] = {str(pixels): sum(s['effective_window_pixels'] == pixels for s in selections.values())
        for pixels in sorted({s['effective_window_pixels'] for s in selections.values()})}
    atomic_json(output / 'plan.json', plan, budget)
    results = []
    for grid in grid_list:
        final = output / 'tiles' / grid['id']
        selection = selections[grid['id']]
        if final.exists():
            record = validate_tile(final, recipe_hash)
            if record.get('effective_terrain_window_pixels') != selection['effective_window_pixels']:
                raise ValueError('Validated tile window differs from reproducible resource planning; preserved')
            results.append(record)
            continue
        if legacy:
            reused = migrate_tile(legacy, final, recipe_hash, selection, budget)
            if reused is not None:
                results.append(reused)
                print(json.dumps({'stage': 'terrain_tiles', 'tile': grid['id'], 'completed': len(results),
                    'requested': len(grid_list), 'status': 'reused_validated',
                    'effective_window_pixels': selection['effective_window_pixels']}), flush=True)
                continue
        staging = budget.safe_path(budget.stage_root / 'terrain-tiles' / recipe_hash[:16] / grid['id'])
        tile_settings = {**settings, 'tile_size': selection['effective_window_pixels']}
        job = {**shared_job, 'operation': 'tile', 'terrain': tile_settings, 'grid': grid,
               'window_selection': selection,
               'halo_grid': _halo_grid(grid, c['terrain_interpolation_halo_m']),
               'staging': str(staging), 'per_file_limit': 128 * MiB,
               'owned_disposable': [str(staging / 'terrain-work' / 'raw_dtm.part.tif')]}
        try:
            if (staging / 'tile.json').exists():
                record = validate_tile(staging, recipe_hash)
            else:
                run(job, 480 * MiB, 1800)
                record = validate_tile(staging, recipe_hash)
            budget.check(additional=16384)
            final.parent.mkdir(parents=True, exist_ok=True)
            if staging.stat().st_dev != final.parent.stat().st_dev:
                raise ValueError('Terrain tile publication must be an atomic same-filesystem rename')
            os.replace(staging, final)
            results.append(record)
            print(json.dumps({'stage': 'terrain_tiles', 'tile': grid['id'], 'completed': len(results),
                              'requested': len(grid_list), 'valid_support_cells': record['cell_counts']['terrain_valid_support_cells'],
                              'effective_window_pixels': selection['effective_window_pixels']}), flush=True)
        except Exception as error:
            cause = getattr(error, 'status', None) or 'build_blocked'
            report = {**plan, 'status': cause if cause.endswith('_blocked') else 'build_blocked',
                      'cause_status': cause, 'error': str(error),
                      'completed_tiles': len(results), 'requested_tiles': len(grid_list),
                      'cleanup': [dict(tile_id=tile['grid']['id'], **entry) for tile in results
                          for entry in tile.get('cleanup', []) + tile['surface_processing'].get('cleanup', [])],
                      'visibility_ready': False}
            atomic_json(output / 'status.json', report, budget, checkpoint=True)
            return report
    totals = {name: sum(tile['cell_counts'][name] for tile in results) for name in results[0]['cell_counts']}
    report = {**plan, 'status': 'terrain_and_surface_tiles_ready_support_incomplete',
              'completed_tiles': len(results), 'requested_tiles': len(grid_list),
              'reused_validated_tiles': sum('reuse_provenance' in tile for tile in results),
              'newly_prepared_tiles': sum('reuse_provenance' not in tile for tile in results),
              'cell_counts': totals, 'tiles': [{'id': grid['id'], 'path': f'tiles/{grid["id"]}/tile.json'} for grid in grid_list],
              'support_missing_area_m2': sum(tile['support_missing_area_m2'] for tile in results),
              'seoul_missing_area_m2': sum(tile['seoul_missing_area_m2'] for tile in results),
              'coverage_measurement': '5 m cell centres, separately from exact vector administrative area',
              'terrain_support_complete': totals['requested_support_cells'] == totals['terrain_valid_support_cells'],
              'visibility_ready': False,
              'shared_heldout_spots': inspected.get('shared_heldout_spots'),
              'maximum_worker_peak_rss_bytes': max(inspected.get('worker_memory', {}).get('observed_peak_rss_bytes', 0),
                  *(tile['worker_memory']['observed_peak_rss_bytes'] for tile in results)),
              'building_surface_status': 'Existing maximum-base screening surfaces prepared; missing footprint ground leaves roofs unresolved.',
              'surface_quality_counts': {key: sum(tile['surface_processing']['pixel_counts'][key] for tile in results)
                  for key in results[0]['surface_processing']['pixel_counts']},
              'cleanup': [dict(tile_id=tile['grid']['id'], **entry) for tile in results
                  for entry in tile.get('cleanup', []) + tile['surface_processing'].get('cleanup', [])],
              'retained_bytes': sum(p.stat().st_size for p in output.rglob('*') if p.is_file())}
    atomic_json(output / 'manifest.json', report, budget)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker', type=Path, required=True)
    args = parser.parse_args()
    _worker(json.loads(args.worker.read_text()))
