"""Bounded citywide source-domain and mapped-feature coverage reporting.

Source-domain envelopes and mapped objects are evidence with limited meanings.
They never certify complete building detection, interpolation support, public
access, current opening, attractive scenery, or field-validated visibility.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
import os
import uuid
from pathlib import Path
import sqlite3

from shapely import from_wkb
from shapely.geometry import GeometryCollection, box
from shapely.ops import transform, unary_union
from shapely.strtree import STRtree

from seoul_visibility.acquisition_safety import sha256
from scripts.data.osm_pipeline import (BoundedGPKG, MIB, OSMValidationError,
    audit_context_coverage, decode_gpkg, validate_contract_geometry, validate_gpkg, _checkpoint_json)


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def source_domain_report(city, support, terrain_halo, terrain_present, building_plan, project) -> tuple:
    """Pure geometry helper; inputs are projected metres except source row bounds."""
    exterior = terrain_halo.difference(city)
    report = {
        'requested_seoul_area_m2': city.area,
        'requested_obstruction_support_area_m2': support.area,
        'requested_terrain_interpolation_area_m2': terrain_halo.area,
        'terrain': {
            'source_present': terrain_present,
            'potential_seoul_source_domain_area_m2': city.area if terrain_present else None,
            'known_outside_official_source_area_m2': exterior.area,
            'source_domain_only': True,
            'interpolation_coverage_validated': False,
            'support_area_complete': False,
            'explanation': 'Official source is Seoul-only. Its administrative domain is not a raster/TIN support mask; interior interpolation gaps remain unvalidated.'},
        'buildings': {'row_group_envelope_coverage_fraction': None,
            'row_group_envelope_gap_area_m2': None, 'real_world_detection_complete': None,
            'evidence': 'No inspected row-group selection plan available.'}}
    envelope_domain = None
    if building_plan:
        groups = building_plan.get('selected_groups', [])
        if len(groups) > 10000:
            raise OSMValidationError('Building coverage plan exceeds bounded 10,000-group metadata cap')
        boxes = []
        for group in groups:
            bounds = group.get('bounds')
            if bounds is None or len(bounds) != 4 or not all(math.isfinite(v) for v in bounds):
                raise OSMValidationError('Building row-group coverage bounds are missing or invalid')
            west, south, east, north = bounds
            if not (-180 <= west <= east <= 180 and -90 <= south <= north <= 90):
                raise OSMValidationError('Building row-group coverage bounds have unexplained CRS')
            if west < east and south < north:
                boxes.append(transform(project, box(*bounds)))
        envelope_domain = unary_union(boxes).intersection(support) if boxes else GeometryCollection()
        report['buildings'] = {
            'row_groups_selected': len(groups),
            'row_group_envelope_area_m2': envelope_domain.area,
            'row_group_envelope_coverage_fraction': envelope_domain.area / support.area,
            'row_group_envelope_gap_area_m2': support.difference(envelope_domain).area,
            'real_world_detection_complete': None,
            'evidence': 'Union of selected Parquet row-group statistics envelopes, intersected with requested support. Envelopes are selection evidence, not observed full-coverage masks.'}
    return report, exterior, envelope_domain


def _open_layer(path: Path, name: str):
    from osgeo import ogr
    if not path.exists():
        return None, None
    dataset = ogr.Open(str(path))
    layer = dataset.GetLayerByName(name) if dataset else None
    if layer is None:
        raise OSMValidationError(f'Missing required coverage layer {name}')
    srs = layer.GetSpatialRef()
    if srs is None:
        raise OSMValidationError(f'Coverage input {name} has missing CRS')
    srs.AutoIdentifyEPSG()
    if srs.GetAuthorityCode(None) != '5186':
        raise OSMValidationError(f'Coverage input {name} must declare EPSG:5186')
    return dataset, layer


def _count_intersecting(layer, polygon, *, building_attributes=False):
    from osgeo import ogr
    if layer is None:
        return {'features': None, 'status': 'source_unavailable'}
    region = ogr.CreateGeometryFromWkb(polygon.wkb)
    layer.SetSpatialFilter(region)
    counts = Counter()
    names = {layer.GetLayerDefn().GetFieldDefn(i).GetName() for i in range(layer.GetLayerDefn().GetFieldCount())} if building_attributes else set()
    try:
        for feature in layer:
            geom = feature.GetGeometryRef()
            if geom is None:
                counts['unlocatable_geometry'] += 1
                continue
            if geom.WkbSize() > 8 * MIB:
                raise OSMValidationError('Coverage query geometry exceeds 8 MiB decoding bound')
            try:
                intersects = geom.Intersects(region)
            except RuntimeError:
                counts['spatial_predicate_unresolved'] += 1
                continue
            if not intersects:
                continue
            counts['features'] += 1
            if building_attributes:
                if 'unresolved' in names:
                    counts['unresolved_height'] += bool(feature['unresolved'])
                if 'invalid_geometry' in names:
                    counts['invalid_geometry'] += bool(feature['invalid_geometry'])
        return {'features': counts['features'], 'status': 'mapped_intersections', **dict(counts)}
    finally:
        layer.SetSpatialFilter(None)


def _districts(context: Path, city):
    if not context.exists():
        return []
    district_list = []
    with sqlite3.connect(f'file:{context.resolve()}?mode=ro', uri=True) as db:
        for identifier, name, tags, blob in db.execute('SELECT source_id,name,tags_json,geom FROM boundaries'):
            tags = json.loads(tags)
            if tags.get('admin_level') != '6':
                continue
            geometry = decode_gpkg(blob)
            if not geometry.is_valid:
                raise OSMValidationError('Invalid district relation geometry')
            if geometry.intersects(city) and geometry.intersection(city).area > geometry.area * 0.5:
                district_list.append({'source_id': identifier, 'name': name, 'geometry': geometry})
                if len(district_list) > 100:
                    raise OSMValidationError('More than 100 candidate Seoul districts; inspect boundary semantics')
    return sorted(district_list, key=lambda row: row['source_id'])


def _point_district_counts(path: Path, layer_name: str, districts, city):
    """Deterministic smallest source-ID tie break at shared district boundaries."""
    if not path.exists():
        return {'available': False, 'total_inside_seoul': None, 'unassigned': None, 'by_district': {}}
    geometries = [row['geometry'] for row in districts]
    tree = STRtree(geometries)
    counts = Counter()
    inside, unassigned = 0, 0
    with sqlite3.connect(f'file:{path.resolve()}?mode=ro', uri=True) as db:
        for blob, in db.execute(f'SELECT geom FROM {layer_name}'):
            geometry = decode_gpkg(blob)
            # Landmark source polygons/lines use representative points only for
            # audit grouping. No representative point becomes a standing candidate.
            point = geometry if geometry.geom_type == 'Point' else geometry.representative_point()
            if not city.covers(point):
                continue
            inside += 1
            possible = sorted(int(index) for index in tree.query(point))
            match = next((index for index in possible if geometries[index].covers(point)), None)
            if match is None:
                unassigned += 1
            else:
                counts[districts[match]['source_id']] += 1
    return {'available': True, 'total_inside_seoul': inside, 'unassigned': unassigned,
            'by_district': dict(counts), 'shared_boundary_rule': 'smallest district source ID',
            'landmark_grouping': 'source geometry representative point; no observer/standing-location implication'}


def _coverage_receipt(path: Path) -> dict:
    if not path.is_file() or path.stat().st_size > 8 * MIB:
        raise OSMValidationError('Coverage receipt is missing or exceeds its bounded metadata size')
    return json.loads(path.read_text())


def _verify_coverage(path: Path, receipt: dict) -> None:
    if (receipt.get('path') != 'coverage.gpkg' or receipt.get('metadata_path') != 'coverage.json'
            or not isinstance(receipt.get('recipe'), dict)
            or receipt.get('sha256') != sha256(path)):
        raise OSMValidationError('Coverage output fingerprint or source receipt changed; preserved')
    if not validate_gpkg(path)['valid']:
        raise OSMValidationError('Existing coverage geometry/index validation failed; preserved')


def _coverage_metadata(path, value, reservation, budget):
    _checkpoint_json(path, value,
                     lambda n: reservation.check_write(n, path.with_name(path.name + '.writing')), budget)


def _coverage_ledger(path):
    ledger = _coverage_receipt(path) if path.exists() else {
        'schema': 'citywide-coverage-preservation-v1', 'versions': []}
    if (ledger.get('schema') != 'citywide-coverage-preservation-v1'
            or not isinstance(ledger.get('versions'), list)
            or any(not isinstance(entry, dict) or entry.get('status') not in
                   {'preservation_planned', 'preserved'} for entry in ledger['versions'])):
        raise OSMValidationError('Invalid coverage preservation ledger; existing files preserved')
    return ledger


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _finish_coverage_preservation(norm, ledger, reservation, budget):
    """Finish only manifest-identified renames, including interrupted two-file moves."""
    for entry in ledger['versions']:
        if entry['status'] != 'preservation_planned':
            continue
        artifacts = entry.get('artifacts')
        if not isinstance(artifacts, list) or not artifacts:
            raise OSMValidationError('Coverage preservation ledger has no artifact evidence')
        for artifact in artifacts:
            original_name = Path(artifact.get('original_path', ''))
            retained_name = Path(artifact.get('preserved_path', ''))
            if (len(original_name.parts) != 1 or original_name.is_absolute()
                    or original_name.name not in {'coverage.gpkg', 'coverage.json',
                        'coverage.owner.json', 'coverage.gpkg.part', 'coverage.json.part',
                        'coverage.gpkg.part-wal', 'coverage.gpkg.part-shm', 'coverage.gpkg.part-journal'}
                    or retained_name.is_absolute() or len(retained_name.parts) != 2
                    or retained_name.name != original_name.name
                    or not retained_name.parts[0].startswith('.coverage.')
                    or not retained_name.parts[0].endswith(('.partial', '.retained'))):
                raise OSMValidationError('Unsafe coverage preservation paths; existing files preserved')
            original = budget.safe_path(norm / original_name)
            retained = budget.safe_path(norm / retained_name)
            if original.is_symlink() or retained.is_symlink() or retained.parent.is_symlink():
                raise OSMValidationError('Coverage preservation symlink is not permitted')
            if retained.exists():
                if original.exists() or not retained.is_file() or sha256(retained) != artifact.get('sha256'):
                    raise OSMValidationError('Retained coverage artifact changed or has conflicting original; preserved')
                continue
            if not original.is_file() or sha256(original) != artifact.get('sha256'):
                raise OSMValidationError('Coverage preservation source changed or disappeared; preserved')
            reservation.check_write(8192, retained)
            original.replace(retained)
            _sync_directory(retained.parent)
            _sync_directory(norm)
        entry['status'] = 'preserved'
        _coverage_metadata(norm / 'coverage.preservation.json', ledger, reservation, budget)


def _preserve_coverage_artifacts(norm, paths, report, reason, reservation, budget):
    """Keep previous coverage bytes and receipts in the same accounted filesystem."""
    ledger_path = norm / 'coverage.preservation.json'
    ledger = _coverage_ledger(ledger_path)
    # Validated previous products are retained versions, not temporary work.
    # Incomplete attempts retain their .partial classification and staging cost.
    suffix = 'retained' if report.get('sha256') else 'partial'
    attempt = budget.safe_path(norm / f'.coverage.{uuid.uuid4().hex}.{suffix}')
    reservation.check_write(16384, attempt)
    attempt.mkdir(exist_ok=False)
    artifacts = []
    for path in paths:
        path = budget.safe_path(path)
        if path.is_symlink() or not path.is_file():
            raise OSMValidationError('Coverage preservation requires regular owned artifacts')
        artifacts.append({'original_path': path.name,
                          'preserved_path': str((attempt / path.name).relative_to(norm)),
                          'sha256': sha256(path), 'logical_bytes': path.stat().st_size,
                          'allocated_bytes': path.stat().st_blocks * 512})
    entry = {'status': 'preservation_planned', 'reason': reason, 'deletion_performed': False,
             'artifacts': artifacts, 'prior_recipe': report.get('recipe'),
             'prior_output_sha256': report.get('sha256')}
    ledger['versions'].append(entry)
    _coverage_metadata(ledger_path, ledger, reservation, budget)
    _sync_directory(norm)
    _finish_coverage_preservation(norm, ledger, reservation, budget)
    return entry


def _coverage_recovery(norm, recipe, budget, reservation=None):
    """Reuse valid current coverage or preserve older versions before rebuilding.

    A read-only call returns completed matching coverage only. Mutations require
    the full new-build reservation, whose baseline already includes old outputs.
    """
    output, metadata = norm / 'coverage.gpkg', norm / 'coverage.json'
    pending, partial = norm / 'coverage.json.part', norm / 'coverage.gpkg.part'
    owner = norm / 'coverage.owner.json'
    ledger = norm / 'coverage.preservation.json'
    for path in (output, metadata, pending, partial, owner, ledger):
        budget.safe_path(path)
        if path.is_symlink():
            raise OSMValidationError('Coverage artifact/receipt symlink is not permitted')
    previous = _coverage_ledger(ledger)
    if any(entry['status'] == 'preservation_planned' for entry in previous['versions']):
        if reservation is None:
            return None
        reservation.check_write(40 * MIB, partial)
        _finish_coverage_preservation(norm, previous, reservation, budget)
    if output.exists():
        receipt_path = metadata if metadata.exists() else pending if pending.exists() else None
        if receipt_path is None:
            raise OSMValidationError('Coverage output lacks a durable source report; preserved for inspection')
        report = _coverage_receipt(receipt_path)
        _verify_coverage(output, report)
        if report['recipe'] == recipe:
            if receipt_path == pending:
                if reservation is None:
                    return None
                reservation.check_write(8192, metadata)
                pending.replace(metadata)
                _sync_directory(norm)
            return report
        if reservation is None:
            return None
        # Reserve/check the full successor before moving the verified old output.
        reservation.check_write(40 * MIB, partial)
        paths = [output, receipt_path]
        if owner.exists() and not partial.exists():
            paths.append(owner)
        _preserve_coverage_artifacts(norm, paths, report, 'source inputs or coverage settings changed', reservation, budget)
    if partial.exists():
        pending_report = _coverage_receipt(pending) if pending.exists() else None
        if pending_report is not None and pending_report.get('recipe') == recipe:
            try:
                _verify_coverage(partial, pending_report)
            except (OSMValidationError, sqlite3.DatabaseError):
                pass
            else:
                if reservation is None:
                    return None
                reservation.check_write(8192, output)
                partial.replace(output)
                pending.replace(metadata)
                _sync_directory(norm)
                return pending_report
        if reservation is None:
            return None
        if not owner.exists():
            raise OSMValidationError('Unowned interrupted coverage partial is preserved; inspection required')
        ownership = _coverage_receipt(owner)
        if (ownership.get('schema') != 'citywide-owned-partial-v1'
                or ownership.get('task_owned') is not True
                or ownership.get('exclusive_creation') is not True
                or ownership.get('path') != partial.name):
            raise OSMValidationError('Interrupted coverage partial has no matching ownership record; preserved')
        reservation.check_write(40 * MIB, partial)
        paths = [partial, owner]
        if pending.exists():
            paths.append(pending)
        registered = set(ownership.get('associated_paths', []))
        for suffix in ('-wal', '-shm', '-journal'):
            journal = Path(str(partial) + suffix)
            if journal.exists():
                if journal.name not in registered:
                    raise OSMValidationError('Unregistered coverage journal preserved; inspection required')
                paths.append(journal)
        _preserve_coverage_artifacts(norm, paths, ownership, 'interrupted coverage rebuild', reservation, budget)
    if metadata.exists() or pending.exists():
        raise OSMValidationError('Orphaned coverage receipt is preserved; inspection required')
    if reservation is not None:
        if owner.exists():
            ownership = _coverage_receipt(owner)
            if (ownership.get('schema') != 'citywide-owned-partial-v1'
                    or ownership.get('task_owned') is not True
                    or ownership.get('exclusive_creation') is not True
                    or ownership.get('path') != partial.name):
                raise OSMValidationError('Coverage ownership record is unrecognized; preserved')
        _coverage_metadata(owner, {'schema': 'citywide-owned-partial-v1', 'task_owned': True,
            'exclusive_creation': True, 'path': partial.name, 'recipe': recipe,
            'associated_paths': [partial.name + suffix for suffix in ('-wal', '-shm', '-journal')]},
            reservation, budget)
    return None


def coverage_report(norm: Path, g: dict, budget, config) -> dict:
    """Reserve once, audit sources in bounded batches, atomically publish a GPKG.

    Returns compact aggregates and a relative metadata path. Detailed spatial
    counts remain in indexed coverage_grid/districts layers, not giant GeoJSON.
    """
    from pyproj import Transformer
    norm = budget.safe_path(norm)
    projected = Transformer.from_crs(4326, 5186, always_xy=True).transform
    for role in ('recommendation', 'obstruction_support', 'terrain_interpolation_support'):
        validate_contract_geometry(g[role], crs='EPSG:4326')
    city, support, halo = [transform(projected, g[key]) for key in ('recommendation', 'obstruction_support', 'terrain_interpolation_support')]
    if not support.buffer(.001).covers(city) or not halo.buffer(.001).covers(support):
        raise OSMValidationError('Coverage geometry roles are not properly nested')
    files = {key: norm / filename for key, filename in {
        'osm': 'osm.gpkg', 'buildings': 'buildings.gpkg', 'terrain': 'terrain.gpkg',
        'candidates': 'candidates.gpkg', 'building_provenance': 'buildings.source.json'}.items()}
    plan_path = norm.parent.parent / f'building-plan.{norm.name}.json'
    if plan_path.exists() and plan_path.stat().st_size > 8 * MIB:
        raise OSMValidationError('Building coverage plan metadata exceeds 8 MiB')
    plan = json.loads(plan_path.read_text()) if plan_path.exists() else None
    recipe = {'version': 1, 'inputs': {k: sha256(p) if p.exists() else None for k, p in files.items()},
              'building_plan': sha256(plan_path) if plan_path.exists() else None,
              'geometries': {k: hashlib.sha256(v.wkb).hexdigest() for k, v in g.items()},
              'sight_distance_m': config['maximum_sight_distance_m'], 'grid_m': 5000}
    output, metadata = norm / 'coverage.gpkg', norm / 'coverage.json'
    completed = _coverage_recovery(norm, recipe, budget)
    if completed is not None:
        return completed
    aggregate, exterior, envelope_domain = source_domain_report(city, support, halo, files['terrain'].exists(), plan, projected)
    if files['building_provenance'].exists():
        source = json.loads(files['building_provenance'].read_text())
        selected = set(source.get('selected_row_groups', []))
        committed = {int(key) for key in source.get('checkpoint_groups', {})}
        aggregate['buildings']['selected_row_groups_committed'] = bool(selected) and selected == committed
        aggregate['buildings']['source_identity'] = source.get('identity')
        aggregate['buildings']['retained_features'] = source.get('features')
        aggregate['buildings']['retained_invalid_geometries'] = source.get('invalid_geometry')
        aggregate['buildings']['unresolved_heights'] = source.get('unresolved_height')
    aggregate['buildings']['normalized_input_present'] = files['buildings'].exists()
    datasets = []
    with budget.reserve(48 * MIB, temporary_bytes=40 * MIB, label='citywide coverage and district audit') as reservation:
        completed = _coverage_recovery(norm, recipe, budget, reservation)
        if completed is not None:
            return completed
        published = {}
        def before_publish(product):
            # All aggregate values have been assembled when publish invokes this
            # callback. Persist their receipt before the canonical GPKG appears.
            report = make_report(product)
            _coverage_metadata(norm / 'coverage.json.part', report, reservation, budget)
            published['report'] = report
        writer = BoundedGPKG(output, lambda n: reservation.check_write(n, output.with_name(output.name + '.part')), 32 * MIB,
                             publication_callback=before_publish)
        try:
            for name in ('requested_extents', 'terrain_support', 'building_source_domain', 'coverage_grid', 'districts'):
                writer.create_layer(name)
            for role, polygon in [('recommendation', city), ('obstruction_support', support), ('terrain_interpolation_support', halo)]:
                writer.add('requested_extents', role, polygon, {'name': role}, {'area_m2': polygon.area, 'requested': True})
            writer.add('terrain_support', 'official-seoul-potential-domain', city, {}, {
                'status': 'potential_source_domain' if files['terrain'].exists() else 'source_unavailable',
                'source_present': files['terrain'].exists(), 'interpolation_support_validated': False})
            if not exterior.is_empty:
                writer.add('terrain_support', 'outside-official-seoul-source', exterior, {}, {
                    'status': 'unsupported_by_acquired_official_source', 'area_m2': exterior.area})
            if envelope_domain is not None and not envelope_domain.is_empty:
                writer.add('building_source_domain', 'selected-row-group-envelopes', envelope_domain, {}, aggregate['buildings'])
                missing = support.difference(envelope_domain)
                if not missing.is_empty and missing.area > .01:
                    writer.add('building_source_domain', 'outside-selected-row-group-envelopes', missing, {}, {
                        'status': 'no_selected_distribution_envelope', 'area_m2': missing.area})
            context_audit = audit_context_coverage(files['osm'], g['recommendation'], g['obstruction_support']) if files['osm'].exists() else None
            district_list = _districts(files['osm'], city)
            candidate_counts = _point_district_counts(files['candidates'], 'candidates', district_list, city)
            landmark_counts = _point_district_counts(files['osm'], 'landmarks', district_list, city)
            building_ds, buildings = _open_layer(files['buildings'], 'buildings'); datasets.append(building_ds)
            candidate_ds, candidates = _open_layer(files['candidates'], 'candidates'); datasets.append(candidate_ds)
            grid_rows = {row['grid_id']: row for row in context_audit['grid_cells']} if context_audit else {}
            grid_count = 0
            building_empty_cells = 0
            xmin, ymin, xmax, ymax = support.bounds
            for gx in range(math.floor(xmin / 5000), math.ceil(xmax / 5000)):
                for gy in range(math.floor(ymin / 5000), math.ceil(ymax / 5000)):
                    polygon = box(gx * 5000, gy * 5000, (gx + 1) * 5000, (gy + 1) * 5000).intersection(support)
                    if polygon.is_empty or polygon.area < .01:
                        continue
                    identifier = f'{gx}:{gy}'
                    evidence = dict(grid_rows.get(identifier, {'grid_id': identifier, 'features': None}))
                    evidence.update(buildings=_count_intersecting(buildings, polygon, building_attributes=True),
                                    candidates=_count_intersecting(candidates, polygon),
                                    seoul_area_m2=polygon.intersection(city).area,
                                    support_area_m2=polygon.area,
                                    potential_terrain_source_area_m2=polygon.intersection(city).area if files['terrain'].exists() else None,
                                    completeness_assertion=False)
                    building_empty_cells += evidence['buildings']['features'] == 0
                    writer.add('coverage_grid', identifier, polygon, {}, evidence)
                    grid_count += 1
                    if grid_count % 32 == 0:
                        reservation.observe()
            districts_report = []
            for district in district_list:
                identifier = district['source_id']
                evidence = {'candidate_count': candidate_counts['by_district'].get(identifier, 0) if candidate_counts['available'] else None,
                    'landmark_source_count': landmark_counts['by_district'].get(identifier, 0) if landmark_counts['available'] else None,
                    'buildings': _count_intersecting(buildings, district['geometry'], building_attributes=True),
                    'source_boundary_area_m2': district['geometry'].area, 'field_verified': False}
                writer.add('districts', identifier, district['geometry'], {'name': district['name']}, evidence)
                districts_report.append({'source_id': identifier, 'name': district['name'], **evidence})
            def make_report(product):
                return {**aggregate, 'recipe': recipe, 'sha256': product['sha256'], 'bytes': product['bytes'],
                    'path': 'coverage.gpkg', 'metadata_path': 'coverage.json', 'crs': 'EPSG:5186',
                    'grid_cells': grid_count, 'grid_size_m': 5000, 'building_empty_grid_cells': building_empty_cells,
                    'empty_cell_interpretation': 'No mapped intersections; neither real absence nor complete inventory is certified.',
                    'district_count': len(district_list), 'expected_seoul_district_count': 25,
                    'district_count_check_passed': len(district_list) == 25, 'districts': districts_report,
                    'candidate_counts': candidate_counts, 'landmark_source_counts': landmark_counts,
                    'seoul_candidate_coverage_contract': 'All retained path and explicitly supported pedestrian-area candidates use the full recommendation polygon; an empty district remains visible in this report.',
                    'citywide_source_completeness_confirmed': False, 'support_area_terrain_complete': False,
                    'field_verified': False, 'validation': product,
                    'limitations': ['Terrain administrative domain is not a valid-interpolation or 5 m accuracy claim.',
                        'Row-group envelopes and nonzero building counts do not prove source detection completeness.',
                        'Candidate points and mapped public spaces do not certify access or current opening.',
                        'Building polygons crossing a grid/district boundary are counted in every intersected unit.']}
            writer.publish()
            report = published['report']
            _verify_coverage(output, report)
            reservation.check_write(8192, metadata)
            (norm / 'coverage.json.part').replace(metadata)
            _sync_directory(norm)
            return report
        except BaseException:
            writer.close()
            raise
        finally:
            datasets.clear()
