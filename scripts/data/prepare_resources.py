"""Bounded window selection and explicitly audited reuse of prepared inputs."""
from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import sqlite3
import time
import uuid

from seoul_visibility.acquisition_safety import AcquisitionError, atomic_json, sha256

LEGACY_RECIPE = '4de79125480f7de151da47bd83b10851c6be152526f52365fb043a01e8b29db3'
LEGACY_PLAN_SHA256 = '8df968deaadc892934c797914151447b6bb179e9f9c0cc74bd785251f22c4065'
LEGACY_ORCHESTRATOR = '5f3dbd22924d548e1c21cd7948911624b450f31b132b517e9945f5561d62ab3b'
LEGACY_VERSION = 'citywide-existing-tin-and-surfaces-v3-atomic-index'
VERSION = 'citywide-existing-tin-and-surfaces-v4-window-policy'


def window_policy(settings):
    return {'default_pixels': settings['tile_size'], 'minimum_pixels': 16,
        'maximum_samples': settings['max_points_per_tile'], 'maximum_queries_per_tile': 12000,
        'maximum_seconds_per_tile': 120,
        'selection': 'Largest admissible window, halving pixels only; unchanged cell spacing, sampling, halos and triangle-edge limit'}


def select_windows(index, grids, settings, source_domain):
    """Count the exact existing local-point query; never fetch all its rows."""
    from shapely.geometry import box
    from seoul_visibility.terrain import iter_tiles
    from scripts.data.prepare_pipeline import _halo_grid

    policy = window_policy(settings)
    initial = policy['default_pixels']
    if not isinstance(initial, int) or not 16 <= initial <= 2048 or initial & (initial - 1):
        raise ValueError('Window planning requires a power-of-two initial size in 16..2048')
    plans = {}
    with sqlite3.connect(f'file:{index}?mode=ro&immutable=1', uri=True, timeout=1) as connection:
        for sql in ['PRAGMA query_only=ON', 'PRAGMA cache_size=-16384',
                    'PRAGMA temp_store=MEMORY', 'PRAGMA mmap_size=0', 'PRAGMA threads=1']:
            connection.execute(sql)
        for grid in grids:
            work = _halo_grid(grid, settings['halo_m'])
            selected = {'effective_window_pixels': initial, 'source_domain_nodata': box(*work['bounds']).disjoint(source_domain),
                        'attempts': [], 'grid': grid, 'halo_grid': work}
            plans[grid['id']] = selected
            if selected['source_domain_nodata']:
                continue
            deadline = time.monotonic() + policy['maximum_seconds_per_tile']
            connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            pixels, queries = initial, 0
            while True:
                maximum, maximum_window = 0, None
                count_queries = 0
                gt, resolution, halo = work['transform'], work['resolution_m'], settings['halo_m']
                try:
                    for x, y, width, height in iter_tiles(work, pixels):
                        if queries >= policy['maximum_queries_per_tile'] or time.monotonic() >= deadline:
                            raise AcquisitionError('resource_blocked', 'Terrain window planning reached its bounded query/time cap')
                        xmin, ymax = gt[0] + x * resolution, gt[3] - y * resolution
                        xmax, ymin = xmin + width * resolution, ymax - height * resolution
                        bounds = [xmin - halo, ymin - halo, xmax + halo, ymax + halo]
                        count = connection.execute('SELECT count(*) FROM points p JOIN spatial s ON p.id=s.id '
                            'WHERE s.maxx>=? AND s.minx<=? AND s.maxy>=? AND s.miny<=?',
                            (bounds[0], bounds[2], bounds[1], bounds[3])).fetchone()[0]
                        queries += 1
                        count_queries += 1
                        if count > maximum:
                            maximum, maximum_window = count, {'pixels': [x, y, width, height], 'query_bounds_epsg5186': bounds}
                except sqlite3.OperationalError as error:
                    if 'interrupt' in str(error).lower():
                        raise AcquisitionError('resource_blocked', 'Terrain window planning exceeded its 120-second deadline') from error
                    raise
                selected['attempts'].append({'window_pixels': pixels, 'count_queries': count_queries,
                                             'maximum_samples': maximum, 'maximum_window': maximum_window})
                if maximum <= policy['maximum_samples']:
                    selected['effective_window_pixels'] = pixels
                    break
                if pixels == policy['minimum_pixels']:
                    raise AcquisitionError('resource_blocked', f'Tile {grid["id"]} still exceeds '
                        f'{policy["maximum_samples"]} local samples at 16 pixels; source and geography preserved')
                pixels = max(policy['minimum_pixels'], pixels // 2)
    return plans


def processing_extents(grids, footprint_halo, query_halo, source_domain, support, declared_halo):
    from shapely.geometry import box, mapping
    from shapely.ops import unary_union, transform
    from pyproj import Transformer
    from scripts.data.prepare_pipeline import _halo_grid

    workspaces, queries, active = [], [], []
    for grid in grids:
        work = _halo_grid(grid, footprint_halo)
        workspace = box(*work['bounds'])
        query = box(*_halo_grid(work, query_halo)['bounds'])
        workspaces.append(workspace)
        queries.append(query)
        if not workspace.disjoint(source_domain):
            active.append(query)
    to_lonlat = Transformer.from_crs(5186, 4326, always_xy=True).transform
    def describe(geometries):
        geometry = unary_union(geometries)
        return {'area_m2': geometry.area, 'bounds_epsg5186': list(geometry.bounds),
            'geometry_crs': 'OGC:CRS84', 'geometry_wgs84': mapping(transform(to_lonlat, geometry)),
            'outside_declared_terrain_halo_area_m2': geometry.difference(declared_halo).area,
            'outside_requested_obstruction_support_area_m2': geometry.difference(support).area,
            'outside_official_source_domain_area_m2': geometry.difference(source_domain).area}
    return {'footprint_workspace_halo_m': footprint_halo, 'additional_local_query_halo_m': query_halo,
        'workspace_union': describe(workspaces), 'conservative_all_workspace_query_union': describe(queries),
        'actual_source_aware_query_union': describe(active), 'actual_tin_workspace_count': len(active),
        'caveat': 'Processing rectangles include discarded/masked cells. Query extent is not valid interpolation coverage; unsupported source cells remain NoData.'}


def approved_legacy(new_recipe, data_root):
    """One audited predecessor only; every other semantic field must be equal."""
    from scripts.data.prepare_pipeline import _key

    data_root = Path(data_root).absolute()
    directory = data_root / 'prepared' / LEGACY_RECIPE[:16]
    path = directory / 'plan.json'
    if (data_root != data_root.resolve() or data_root.is_symlink()
            or any(p.is_symlink() for p in [directory.parent, directory, path])
            or not path.resolve().is_relative_to(data_root.resolve())):
        raise ValueError('Previous preparation path contains a symlink escape; no source read')
    if not path.exists():
        return None
    if path.is_symlink() or sha256(path) != LEGACY_PLAN_SHA256:
        raise ValueError('Audited previous preparation plan changed; existing outputs preserved')
    plan = json.loads(path.read_text())
    old_recipe = plan['recipe']
    if plan.get('recipe_sha256') != LEGACY_RECIPE or _key(old_recipe) != LEGACY_RECIPE:
        raise ValueError('Previous preparation recipe hash does not match its contents')
    previous, current = copy.deepcopy(old_recipe), copy.deepcopy(new_recipe)
    if previous.pop('processing_version') != LEGACY_VERSION or current.pop('processing_version') != VERSION:
        raise ValueError('Preparation migration version is not audited')
    if current.pop('window_selection_policy') != window_policy(current['settings']):
        raise ValueError('Preparation window policy differs from the audited resource-only change')
    old_code = previous['implementation_sha256'].pop('scripts/data/prepare_pipeline.py')
    current['implementation_sha256'].pop('scripts/data/prepare_pipeline.py')
    current['implementation_sha256'].pop('scripts/data/prepare_resources.py')
    if old_code != LEGACY_ORCHESTRATOR or previous != current:
        raise ValueError('Previous source/native/dependency/grid/halo/sampling/base settings are incompatible; no migration performed')
    return {'directory': directory, 'plan': plan, 'plan_sha256': LEGACY_PLAN_SHA256,
            'recipe_sha256': LEGACY_RECIPE}


def _link_files(old_directory, attempt, relative_paths, budget, reservation):
    records = []
    for relative in relative_paths:
        path = Path(relative)
        if path.is_absolute() or '..' in path.parts:
            raise ValueError('Migration file path escapes its inspected directory')
        source, destination = budget.safe_path(old_directory / path), budget.safe_path(attempt / path)
        if source.stat().st_dev != attempt.stat().st_dev:
            raise ValueError('Audited reuse requires same-filesystem hardlinks; no unbudgeted copy fallback')
        reservation.check_write(16384, destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        digest = sha256(source)
        os.link(source, destination, follow_symlinks=False)
        records.append({'path': str(path), 'bytes': source.stat().st_size, 'sha256': digest,
                        'method': 'same-filesystem hardlink; original preserved'})
    return records


def migrate_index(legacy, destination, recipe_sha256, budget):
    from scripts.data.prepare_pipeline import _validate_index_bundle, _publish_index_bundle

    source = legacy['directory'] / 'sample-index'
    inspected = _validate_index_bundle(source, legacy['recipe_sha256'])
    metadata = copy.deepcopy(inspected)
    source_bytes = (source / 'terrain_samples.sqlite').stat().st_size
    attempt = destination.parent / ('sample-index.migration-' + uuid.uuid4().hex + '.partial')
    with budget.reserve(256 * 1024, source_bytes + 256 * 1024, 'reuse validated terrain index') as reservation:
        attempt.mkdir()
        atomic_json(attempt / 'owner.json', {'recipe_sha256': recipe_sha256,
            'kind': 'terrain_sample_index_bundle', 'owns_directory': True}, budget)
        atomic_json(attempt / 'migration-intent.json', {'source_recipe_sha256': legacy['recipe_sha256'],
            'source_plan_sha256': legacy['plan_sha256'], 'source_directory': str(source.relative_to(budget.root)),
            'planned_paths': ['terrain_samples.sqlite'], 'expected_index_sha256': inspected['sha256'],
            'status': 'owned_before_hardlink_creation'}, budget)
        files = _link_files(source, attempt, ['terrain_samples.sqlite'], budget, reservation)
        provenance = {'status': 'reused_validated', 'source_recipe_sha256': legacy['recipe_sha256'],
            'source_plan_sha256': legacy['plan_sha256'], 'source_receipt_sha256': sha256(source / 'terrain_samples.json'),
            'source_directory': str(source.relative_to(budget.root)), 'files': files,
            'claim': 'Previously built sample index reused without resampling or modification'}
        metadata.update(recipe_sha256=recipe_sha256, reuse_provenance=provenance)
        atomic_json(attempt / 'migration.json', provenance, budget)
        atomic_json(attempt / 'terrain_samples.json', metadata, budget)
        return _publish_index_bundle(attempt, destination, recipe_sha256, budget)


def migrate_tile(legacy, destination, recipe_sha256, selection, budget):
    from scripts.data.prepare_pipeline import validate_tile, _validate_products, TILE_OWNED_PATHS

    source = legacy['directory'] / 'tiles' / selection['grid']['id']
    if not source.exists() or selection['effective_window_pixels'] != legacy['plan']['recipe']['settings']['tile_size']:
        return None
    original = validate_tile(source, legacy['recipe_sha256'])
    terrain = _validate_products(source, 'terrain.json', legacy['recipe_sha256'])
    surface = _validate_products(source, 'surface.json', legacy['recipe_sha256'])
    pixels = selection['effective_window_pixels']
    if (any(record['grid'] != selection['grid'] for record in [original, terrain, surface])
            or original['terrain_halo_grid'] != selection['halo_grid']
            or terrain['terrain_halo_grid'] != selection['halo_grid']):
        raise ValueError('Previous tile grid/halo differs; no migration performed')
    if any(record.get('effective_terrain_window_pixels', pixels) != pixels
           for record in [original, terrain, surface]):
        raise ValueError('Previous tile window evidence differs; no migration performed')
    details = terrain['terrain_processing']
    if (original['terrain_processing'] != details
            or original['surface_processing'] != surface):
        raise ValueError('Previous tile processing receipts disagree; no migration performed')
    nodata = details.get('kind') == 'source_domain_nodata'
    if nodata != selection['source_domain_nodata']:
        raise ValueError('Previous tile source-domain policy differs; no migration performed')
    if nodata:
        if details.get('interpolation_performed') is not False or details.get('valid_pixels') != 0:
            raise ValueError('Previous NoData tile interpolation evidence is inconsistent')
    else:
        halo_grid = selection['halo_grid']
        expected_windows = math.ceil(halo_grid['width'] / pixels) * math.ceil(halo_grid['height'] / pixels)
        if details.get('tin_builds') != expected_windows or details.get('tiles_completed') != expected_windows:
            raise ValueError('Previous tile TIN window count differs from its audited recipe')
    halo = terrain['terrain_halo']
    if sha256(source / halo['path']) != halo['sha256']:
        raise ValueError('Previous footprint-halo ground is corrupt; source preserved')
    paths = sorted({p['path'] for p in original['products'].values()} | {halo['path']})
    size = sum((source / p).stat().st_size for p in paths)
    attempt = destination.parent / (destination.name + '.migration-' + uuid.uuid4().hex + '.partial')
    with budget.reserve(512 * 1024, size + 512 * 1024, 'reuse validated terrain and obstruction tile') as reservation:
        attempt.mkdir(parents=True)
        atomic_json(attempt / 'owner.json', {'recipe_sha256': recipe_sha256,
            'kind': 'citywide_prepare_tile', 'owned_relative_paths': TILE_OWNED_PATHS}, budget)
        atomic_json(attempt / 'migration-intent.json', {'source_recipe_sha256': legacy['recipe_sha256'],
            'source_plan_sha256': legacy['plan_sha256'], 'source_directory': str(source.relative_to(budget.root)),
            'planned_paths': paths, 'source_tile_receipt_sha256': sha256(source / 'tile.json'),
            'status': 'owned_before_hardlink_creation'}, budget)
        files = _link_files(source, attempt, paths, budget, reservation)
        provenance = {'status': 'reused_validated', 'source_recipe_sha256': legacy['recipe_sha256'],
            'source_plan_sha256': legacy['plan_sha256'], 'source_directory': str(source.relative_to(budget.root)),
            'source_receipts_sha256': {name: sha256(source / name) for name in ['terrain.json', 'surface.json', 'tile.json']},
            'files': files, 'effective_window_pixels': selection['effective_window_pixels'],
            'window_evidence': 'Fixed window from pinned predecessor plan/coordinator, with consistent tile processing receipts',
            'claim': 'Previously validated raster values reused unchanged; original manifests and historical cleanup remain at source'}
        updated_surface = copy.deepcopy(surface)
        updated_terrain = copy.deepcopy(terrain)
        updated_tile = copy.deepcopy(original)
        for record in [updated_surface, updated_terrain, updated_tile]:
            record.update(recipe_sha256=recipe_sha256, reuse_provenance=provenance, cleanup=[],
                          effective_terrain_window_pixels=selection['effective_window_pixels'])
        updated_tile.update(surface_processing=updated_surface, window_selection=selection)
        for name, record in [('terrain.json', updated_terrain), ('surface.json', updated_surface), ('tile.json', updated_tile), ('migration.json', provenance)]:
            atomic_json(attempt / name, record, budget)
        validate_tile(attempt, recipe_sha256)
        if destination.exists() or destination.is_symlink():
            raise ValueError('Existing prepared tile preserved; migration cannot overwrite it')
        os.rename(attempt, destination)
        return updated_tile
