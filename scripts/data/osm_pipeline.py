"""Bounded, relation-aware OSM normalization; this module never downloads data.

Pyosmium's documented two-pass area assembler reads the complete country snapshot
before the support polygon is applied. Clipping raw member ways first would lose
holes and crossing relations. GPKG geometries retain complete intersecting objects;
standing candidates alone are constrained to the recommendation polygon.

Callers reserve the full peak and hold the shared pipeline lock. ``check_budget``
is called BEFORE each bounded SQLite write, and periodically while reading. It
accepts a conservative upcoming-byte estimate and may raise to checkpoint safely.
SQLite has a page ceiling, small RAM cache, and RAM journals; all permanent writes
are to an unpublished .part file. This is an application byte policy, not a quota.
"""
from __future__ import annotations

from collections import Counter
from array import array
from bisect import bisect_left
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import struct
import uuid
from typing import Callable, Iterable

from shapely import from_wkb, get_num_coordinates
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import transform

MIB = 1024**2
ACCESS_KEYS = ('access', 'foot', 'access:conditional', 'foot:conditional', 'opening_hours',
               'steps', 'incline', 'wheelchair', 'barrier', 'bridge', 'tunnel', 'layer',
               'sidewalk', 'sidewalk:left', 'sidewalk:right', 'highway', 'indoor')
PEDESTRIAN_HIGHWAYS = {'footway', 'path', 'pedestrian', 'steps', 'living_street',
                      'residential', 'service', 'unclassified', 'tertiary',
                      'tertiary_link', 'secondary', 'secondary_link', 'primary',
                      'primary_link', 'track', 'cycleway'}
LAYER_NAMES = ('paths', 'public_spaces', 'water', 'green_space', 'peaks_ridges',
               'bridges', 'landmarks', 'barriers', 'boundaries', 'quality')
GPKG_LAYERS = {*LAYER_NAMES, 'candidates', 'requested_extents', 'terrain_support',
               'building_source_domain', 'coverage_grid', 'districts'}


class OSMValidationError(ValueError):
    """A source cannot be represented as complete, valid OSM geometry."""


class OSMResourceError(RuntimeError):
    """Stop with the PBF and unpublished checkpoint preserved."""


def _category_matches(tags: dict, geometry_type: str = '') -> list[str]:
    result = []
    highway = tags.get('highway')
    if highway in PEDESTRIAN_HIGHWAYS or (highway and tags.get('foot') in {'yes', 'designated'}):
        result.append('public_spaces' if tags.get('area') == 'yes' else 'paths')
    if tags.get('leisure') in {'park', 'garden', 'recreation_ground', 'common'} or tags.get('place') == 'square':
        result.append('public_spaces')
    if tags.get('natural') == 'water' or tags.get('waterway') in {'river', 'stream', 'canal', 'riverbank', 'drain', 'ditch'} or tags.get('landuse') == 'reservoir':
        result.append('water')
    if tags.get('natural') in {'wood', 'scrub', 'grassland', 'heath', 'wetland'} or tags.get('landuse') in {'forest', 'grass', 'meadow', 'orchard'} or tags.get('leisure') == 'park':
        result.append('green_space')
    if tags.get('natural') in {'peak', 'ridge', 'saddle', 'cliff'}:
        result.append('peaks_ridges')
    if tags.get('bridge') not in {None, 'no'} or tags.get('man_made') == 'bridge':
        result.append('bridges')
    if tags.get('tourism') in {'attraction', 'museum', 'artwork'} or tags.get('historic') in {'monument', 'memorial', 'castle', 'ruins', 'archaeological_site'} or tags.get('man_made') in {'tower', 'lighthouse'} or tags.get('natural') == 'peak':
        # Scenic-viewpoint catalogue tags are deliberately not collected.
        result.append('landmarks')
    if 'barrier' in tags:
        result.append('barriers')
    if tags.get('boundary') == 'administrative' and tags.get('admin_level') in {'6', '7', '8', '9', '10'}:
        # The recommendation boundary is supplied separately. District/local
        # relations provide grouping; national coastlines add no required evidence.
        result.append('boundaries')
    return result


def categories(tags: dict, geometry_type: str = '') -> list[str]:
    """Stable unique layers; several tags may independently identify one layer.

    For example a pedestrian area also tagged place=square is one public-space
    feature. This merges classification matches, never distinct geometries.
    """
    return list(dict.fromkeys(_category_matches(tags, geometry_type)))


def access_evidence(tags: dict) -> dict:
    """A mapped permission is not public-access or current-opening verification."""
    conditional = any(key in tags for key in ('foot:conditional', 'access:conditional'))
    effective = tags.get('foot', tags.get('access'))
    if conditional:
        status = 'conditional_unresolved'
    elif effective in {'no', 'private', 'customers', 'destination', 'use_sidepath'}:
        status = 'restricted'
    elif effective in {'yes', 'designated', 'official'}:
        status = 'mapped_permission_unverified'
    elif effective == 'permissive':
        status = 'permissive_unverified'
    else:
        status = 'unknown'
    return {'status': status, 'evidence': {k: tags[k] for k in ACCESS_KEYS if k in tags},
            'field_verified': False, 'currently_open': None}


def validate_contract_geometry(geometry, *, crs: str) -> None:
    if crs != 'EPSG:4326':
        raise OSMValidationError('Interface geometry must explicitly declare EPSG:4326')
    if geometry.geom_type not in {'Polygon', 'MultiPolygon'} or geometry.is_empty or not geometry.is_valid:
        raise OSMValidationError('A valid nonempty Polygon/MultiPolygon is required')
    west, south, east, north = geometry.bounds
    if not all(math.isfinite(x) for x in geometry.bounds) or not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise OSMValidationError('Unexplained coordinates: expected WGS84 longitude/latitude')


def assert_complete_way(node_refs: Iterable) -> list[tuple[float, float]]:
    coords = []
    for ref in node_refs:
        if not ref.location.valid():
            raise OSMValidationError(f'Way missing required node {ref.ref}')
        coords.append((ref.lon, ref.lat))
    if len(coords) < 2:
        raise OSMValidationError('Way has fewer than two resolved nodes')
    return coords


def relation_completeness(expected: set[int], assembled: set[int]) -> dict:
    missing = sorted(expected - assembled)
    return {'expected_area_relations': len(expected), 'assembled_area_relations': len(expected & assembled),
            'unassembled_relation_ids': missing, 'relation_geometry_complete': not missing,
            'scope': 'all relevant country relations; missing geometry has unknown location'}


def aggregate_geometry_diagnostics(issues: list[dict], missing_relations: Iterable[int],
                                   *, max_diagnostics: int = 2000) -> list[tuple[str, dict]]:
    """Keep every diagnostic reason in one quality row per complete source ID.

    An area may fail WKB construction and subsequently be reported unassembled.
    Those are two useful diagnoses of one object, not two unique source objects.
    Numeric node/way/relation IDs remain distinct through their type prefixes.
    """
    grouped = {}
    count = 0

    def add(source_id, reason):
        nonlocal count
        if count >= max_diagnostics:
            raise OSMValidationError('Combined geometry diagnostics exceed bounded report allowance; source retained')
        grouped.setdefault(source_id, []).append(reason)
        count += 1

    for issue in issues:
        add(issue['source_id'], issue['reason'])
    for identifier in missing_relations:
        add(f'relation/{identifier}', 'Area relation was not assembled')
    return [(source_id, {'source_id': source_id, 'reason': reasons[0],
                        'reasons': reasons, 'diagnostic_count': len(reasons),
                        'location_unknown': True, 'display_point_is_not_error_location': True})
            for source_id, reasons in sorted(grouped.items())]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as source:
        while block := source.read(MIB):
            digest.update(block)
    return digest.hexdigest()


def _gpkg_blob(geometry) -> bytes:
    return b'GP\x00\x01' + struct.pack('<i', 5186) + geometry.wkb


def decode_gpkg(blob: bytes):
    if len(blob) < 8 or blob[:2] != b'GP':
        raise OSMValidationError('Not a GeoPackage geometry')
    envelope = (blob[3] >> 1) & 7
    offsets = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}
    if envelope not in offsets:
        raise OSMValidationError('Invalid GeoPackage envelope')
    return from_wkb(blob[8 + offsets[envelope]:])


class BoundedGPKG:
    """Small write-once GPKG with RTree indexes and a SQLite page ceiling.

    Journals stay in a bounded single-record RAM transaction. No SQL sort or
    unbounded aggregation is used. The .part database is never resumed as valid.
    A crash leaves it for manifest-owned preservation and a separately budgeted rebuild.
    """
    def __init__(self, output: Path, check_budget: Callable[[int], None], max_bytes: int,
                 publication_callback=None):
        from pyproj import CRS
        self.output = Path(output)
        self.partial = self.output.with_name(self.output.name + '.part')
        if max_bytes < 4 * MIB:
            raise OSMResourceError('At least 4 MiB is required for the bounded GPKG stage')
        if any(parent.is_symlink() for parent in self.output.parents):
            raise OSMValidationError('Output parent symlink is not an accounted direct path')
        if self.output.exists() or self.partial.exists() or self.output.is_symlink() or self.partial.is_symlink():
            raise FileExistsError('Preserved output/partial exists; validate or record owned cleanup before rebuilding')
        self.check_budget, self.max_bytes = check_budget, max_bytes
        self.publication_callback = publication_callback
        self.budget_credit = 0
        check_budget(2 * MIB)
        self.partial.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.partial)
        self.db.execute('PRAGMA page_size=4096')
        self.db.execute(f'PRAGMA max_page_count={(max_bytes - MIB) // 4096}')
        self.db.execute('PRAGMA journal_mode=MEMORY')
        self.db.execute('PRAGMA temp_store=MEMORY')
        self.db.execute('PRAGMA cache_size=-8192')
        self.db.execute('PRAGMA synchronous=OFF')
        self.db.execute('PRAGMA application_id=1196444487')
        self.db.execute('PRAGMA user_version=10300')
        self.db.executescript('''
        CREATE TABLE gpkg_spatial_ref_sys(srs_name TEXT NOT NULL, srs_id INTEGER NOT NULL PRIMARY KEY,
          organization TEXT NOT NULL, organization_coordsys_id INTEGER NOT NULL, definition TEXT NOT NULL, description TEXT);
        CREATE TABLE gpkg_contents(table_name TEXT NOT NULL PRIMARY KEY, data_type TEXT NOT NULL,
          identifier TEXT UNIQUE, description TEXT DEFAULT '', last_change DATETIME NOT NULL,
          min_x DOUBLE, min_y DOUBLE, max_x DOUBLE, max_y DOUBLE, srs_id INTEGER);
        CREATE TABLE gpkg_geometry_columns(table_name TEXT NOT NULL, column_name TEXT NOT NULL,
          geometry_type_name TEXT NOT NULL, srs_id INTEGER NOT NULL, z TINYINT NOT NULL, m TINYINT NOT NULL,
          PRIMARY KEY(table_name,column_name));
        CREATE TABLE gpkg_extensions(table_name TEXT,column_name TEXT,extension_name TEXT NOT NULL,
          definition TEXT NOT NULL,scope TEXT NOT NULL,UNIQUE(table_name,column_name,extension_name));
        ''')
        self.db.executemany('INSERT INTO gpkg_spatial_ref_sys VALUES(?,?,?,?,?,?)', [
            ('Undefined Cartesian', -1, 'NONE', -1, 'undefined', ''),
            ('Undefined Geographic', 0, 'NONE', 0, 'undefined', ''),
            ('WGS 84', 4326, 'EPSG', 4326, CRS.from_epsg(4326).to_wkt(version='WKT1_GDAL'), ''),
            ('KGD2002 / Central Belt 2010', 5186, 'EPSG', 5186, CRS.from_epsg(5186).to_wkt(version='WKT1_GDAL'),
             'Horizontal projection only; no vertical conversion')])
        self.db.commit()
        self.counts = Counter()
        self.extents = {}
        self.max_observed_bytes = self.partial.stat().st_size

    def ensure_budget(self, upcoming: int) -> None:
        # Recheck at bounded byte batches. Parent holds the full-stage reservation;
        # credit is only the measured-filesystem polling margin, never extra budget.
        if upcoming > self.budget_credit:
            bounded_batch = max(8 * MIB, upcoming)
            self.check_budget(bounded_batch)
            self.budget_credit = bounded_batch
        self.budget_credit -= upcoming

    def create_layer(self, name: str) -> None:
        if name not in GPKG_LAYERS:
            raise ValueError('Unexpected layer name')
        self.check_budget(MIB)
        self.db.execute(f'''CREATE TABLE {name}(fid INTEGER PRIMARY KEY, geom BLOB,
            source_id TEXT NOT NULL UNIQUE, name TEXT, tags_json TEXT NOT NULL,
            evidence_json TEXT NOT NULL, source_version INTEGER, source_timestamp TEXT)''')
        self.db.execute(f'CREATE VIRTUAL TABLE rtree_{name}_geom USING rtree(id,minx,maxx,miny,maxy)')
        self.db.execute('INSERT INTO gpkg_contents(table_name,data_type,identifier,last_change,srs_id) VALUES(?,?,?,?,?)',
                        (name, 'features', name, datetime.now(timezone.utc).isoformat(), 5186))
        self.db.execute('INSERT INTO gpkg_geometry_columns VALUES(?,?,?,?,?,?)', (name, 'geom', 'GEOMETRY', 5186, 0, 0))
        self.db.execute('INSERT INTO gpkg_extensions VALUES(?,?,?,?,?)', (name, 'geom', 'gpkg_rtree_index',
                        'http://www.geopackage.org/spec/#extension_rtree', 'write-only'))
        # Standard triggers keep the index valid when opened by a conforming GIS
        # editor that supplies the GPKG spatial SQL functions.
        self.db.executescript(f'''
        CREATE TRIGGER rtree_{name}_geom_insert AFTER INSERT ON {name}
        WHEN NEW.geom NOT NULL AND NOT ST_IsEmpty(NEW.geom) BEGIN
          INSERT OR REPLACE INTO rtree_{name}_geom VALUES(NEW.fid, ST_MinX(NEW.geom), ST_MaxX(NEW.geom), ST_MinY(NEW.geom), ST_MaxY(NEW.geom)); END;
        CREATE TRIGGER rtree_{name}_geom_update1 AFTER UPDATE OF geom ON {name}
        WHEN OLD.fid=NEW.fid AND NEW.geom NOTNULL AND NOT ST_IsEmpty(NEW.geom) BEGIN
          INSERT OR REPLACE INTO rtree_{name}_geom VALUES(NEW.fid, ST_MinX(NEW.geom), ST_MaxX(NEW.geom), ST_MinY(NEW.geom), ST_MaxY(NEW.geom)); END;
        CREATE TRIGGER rtree_{name}_geom_update2 AFTER UPDATE OF geom ON {name}
        WHEN OLD.fid=NEW.fid AND (NEW.geom ISNULL OR ST_IsEmpty(NEW.geom)) BEGIN
          DELETE FROM rtree_{name}_geom WHERE id=OLD.fid; END;
        CREATE TRIGGER rtree_{name}_geom_update3 AFTER UPDATE ON {name}
        WHEN OLD.fid!=NEW.fid AND NEW.geom NOTNULL AND NOT ST_IsEmpty(NEW.geom) BEGIN
          DELETE FROM rtree_{name}_geom WHERE id=OLD.fid;
          INSERT OR REPLACE INTO rtree_{name}_geom VALUES(NEW.fid, ST_MinX(NEW.geom), ST_MaxX(NEW.geom), ST_MinY(NEW.geom), ST_MaxY(NEW.geom)); END;
        CREATE TRIGGER rtree_{name}_geom_update4 AFTER UPDATE ON {name}
        WHEN OLD.fid!=NEW.fid AND (NEW.geom ISNULL OR ST_IsEmpty(NEW.geom)) BEGIN
          DELETE FROM rtree_{name}_geom WHERE id IN(OLD.fid,NEW.fid); END;
        CREATE TRIGGER rtree_{name}_geom_delete AFTER DELETE ON {name} BEGIN
          DELETE FROM rtree_{name}_geom WHERE id=OLD.fid; END;
        ''')
        for function, index in [('ST_MinX', 0), ('ST_MinY', 1), ('ST_MaxX', 2), ('ST_MaxY', 3)]:
            self.db.create_function(function, 1, lambda blob, i=index: decode_gpkg(blob).bounds[i])
        self.db.create_function('ST_IsEmpty', 1, lambda blob: int(decode_gpkg(blob).is_empty))
        self.db.commit()

    def add(self, layer: str, source_id: str, geometry, tags: dict, evidence: dict,
            version: int | None = None, timestamp: str | None = None) -> None:
        if layer not in GPKG_LAYERS:
            raise ValueError('Unexpected layer')
        if get_num_coordinates(geometry) > 250_000:
            raise OSMResourceError('Geometry exceeds the 250,000-coordinate decode cap; source preserved')
        blob = _gpkg_blob(geometry)
        attrs = json.dumps(tags, ensure_ascii=False, separators=(',', ':'))
        proof = json.dumps(evidence, ensure_ascii=False, separators=(',', ':'))
        record_bytes = len(blob) + len(attrs.encode()) + len(proof.encode())
        if record_bytes > 8 * MIB:
            raise OSMResourceError('Single feature exceeds the 8 MiB record cap')
        # RTree/page splits plus the geometry/attributes; retain one MiB between
        # the DB page ceiling and the artifact ceiling for final metadata.
        upcoming = record_bytes * 4 + 32 * 1024
        self.ensure_budget(upcoming)
        if self.partial.stat().st_size + upcoming > self.max_bytes:
            raise OSMResourceError('GPKG would exceed its per-artifact byte ceiling before this record')
        with self.db:
            self.db.execute(f'INSERT INTO {layer}(geom,source_id,name,tags_json,evidence_json,source_version,source_timestamp) VALUES(?,?,?,?,?,?,?)',
                            (blob, source_id, tags.get('name', tags.get('name:en')), attrs, proof, version, timestamp))
        self.counts[layer] += 1
        xmin, ymin, xmax, ymax = geometry.bounds
        old = self.extents.get(layer, (xmin, ymin, xmax, ymax))
        self.extents[layer] = (min(xmin, old[0]), min(ymin, old[1]), max(xmax, old[2]), max(ymax, old[3]))
        self.max_observed_bytes = max(self.max_observed_bytes, self.partial.stat().st_size)

    def publish(self, extra_report=None) -> dict:
        self.check_budget(MIB)
        for layer, bounds in self.extents.items():
            self.db.execute('UPDATE gpkg_contents SET min_x=?,min_y=?,max_x=?,max_y=? WHERE table_name=?', (*bounds, layer))
        self.db.commit()
        if self.db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise OSMValidationError('Unpublished GPKG failed SQLite quick_check')
        self.db.close()
        report = validate_gpkg(self.partial)
        if not report['valid']:
            raise OSMValidationError(f'Unpublished GPKG failed validation: {report}')
        with self.partial.open('rb') as stream:
            os.fsync(stream.fileno())
        self.check_budget(0)
        if self.output.exists():
            raise FileExistsError('Output appeared before atomic publication')
        result = {**report, 'sha256': _sha256(self.partial), 'bytes': self.partial.stat().st_size,
                  'peak_output_bytes_sampled': self.max_observed_bytes, **(extra_report or {})}
        if self.publication_callback is not None:
            self.publication_callback(result)
        self.partial.replace(self.output)
        return result

    def close(self):
        self.db.close()


def validate_gpkg(path: Path) -> dict:
    """Offline, streaming geometry/CRS/index validation without loading a city."""
    db = sqlite3.connect(f'file:{Path(path).resolve()}?mode=ro', uri=True)
    report = {'valid': True, 'layers': {}, 'invalid_geometries': 0, 'duplicate_source_ids': 0}
    try:
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise OSMValidationError('SQLite quick_check failed')
        layers = db.execute('SELECT table_name,srs_id FROM gpkg_geometry_columns').fetchall()
        if not layers:
            raise OSMValidationError('Missing GPKG geometry/CRS metadata')
        for name, srs_id in layers:
            if name not in GPKG_LAYERS or srs_id != 5186:
                raise OSMValidationError('Missing/unexpected layer CRS')
            count, invalid = 0, 0
            for blob, in db.execute(f'SELECT geom FROM {name}'):
                geom = decode_gpkg(blob)
                invalid += int(not geom.is_valid or geom.is_empty)
                count += 1
            indexed = db.execute(f'SELECT count(*) FROM rtree_{name}_geom').fetchone()[0]
            duplicates = db.execute(f'SELECT count(*) - count(DISTINCT source_id) FROM {name}').fetchone()[0]
            report['layers'][name] = {'features': count, 'indexed': indexed, 'invalid': invalid, 'crs': 'EPSG:5186'}
            report['invalid_geometries'] += invalid
            report['duplicate_source_ids'] += duplicates
            if invalid or indexed != count or duplicates:
                report['valid'] = False
    finally:
        db.close()
    return report


def inspect_osm(pbf: Path, *, max_nodes: int = 50_000_000, max_relations: int = 150_000,
                max_ways: int = 5_000_000) -> dict:
    """Sequential local metadata scan, with no location cache and no disk writes.

    Counting area members before index construction bounds compressed-PBF memory
    expansion. The 50M node cap was raised after the pinned South Korea snapshot
    proved to contain 38,346,399 nodes; independent RAM limits remain unchanged.
    This is not the CLI's small metadata-only network plan.
    """
    import osmium
    counts = Counter()
    way_ids = array('q')
    incomplete = set()

    class Inspector(osmium.SimpleHandler):
        def node(self, node):
            counts['nodes'] += 1
            if counts['nodes'] > max_nodes:
                raise OSMResourceError('OSM node count exceeds bounded cache plan')

        def way(self, way):
            counts['ways'] += 1
            if counts['ways'] > max_ways:
                raise OSMResourceError('OSM way-reference preflight reached its 40 MiB ID-array cap')
            if way_ids and way.id <= way_ids[-1]:
                raise OSMValidationError('OSM snapshot ways must be sorted and unique for bounded reference validation')
            way_ids.append(way.id)
            counts['way_nodes'] += len(way.nodes)
            counts['max_way_nodes'] = max(counts['max_way_nodes'], len(way.nodes))

        def relation(self, relation):
            counts['relations'] += 1
            if counts['relations'] > max_relations:
                raise OSMResourceError('OSM relation count exceeds bounded assembler plan')
            if relation.tags.get('type') in {'multipolygon', 'boundary'}:
                counts['area_relations'] += 1
                counts['area_relation_members'] += len(relation.members)
                if categories(dict(relation.tags)):
                    for member in relation.members:
                        if member.type == 'w':
                            index = bisect_left(way_ids, member.ref)
                            if index == len(way_ids) or way_ids[index] != member.ref:
                                incomplete.add(relation.id)
                    if len(incomplete) > 1000:
                        raise OSMValidationError('More than 1000 incomplete country relations; source retained')

    Inspector().apply_file(str(pbf))
    for key in ('nodes', 'ways', 'way_nodes', 'max_way_nodes', 'relations', 'area_relations', 'area_relation_members'):
        counts.setdefault(key, 0)
    incremental = counts['nodes'] * 40 + counts['area_relation_members'] * 512 + 128 * MIB
    return {'counts': dict(counts), 'estimated_incremental_memory_bytes': incremental,
            'incomplete_relation_ids': sorted(incomplete),
            'memory_estimate': '40 bytes/node + 512 bytes/area member + 128 MiB decoder margin',
            'geometry_crs': 'EPSG:4326', 'format_parseable': True}


def inspection_cache_record(pbf: Path, completed_report: dict) -> dict:
    """Bind a completed real inspection to verified whole-source bytes and scanner.

    This does not infer counts or run another country scan. The caller supplies
    the actual report from inspect_osm; source SHA-256 is recomputed by streaming.
    """
    import importlib.metadata
    size = Path(pbf).stat().st_size
    if completed_report.get('source_bytes', size) != size:
        raise OSMValidationError('Completed OSM inspection describes a different source size')
    record = {'schema': 'citywide-osm-inspection-v1', 'scanner_revision': 1,
              'osmium_version': importlib.metadata.version('osmium'),
              'source_sha256': _sha256(Path(pbf)), 'source_bytes': size,
              'inspection': completed_report}
    record['record_sha256'] = hashlib.sha256(json.dumps(record, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    _validated_inspection_record(record, record['source_sha256'], max_nodes=50_000_000,
                                 max_relations=150_000, max_ways=5_000_000)
    return record


def _validated_inspection_record(record, source_sha256, *, max_nodes, max_relations, max_ways):
    import importlib.metadata
    unsigned = {key: value for key, value in record.items() if key != 'record_sha256'}
    fingerprint = hashlib.sha256(json.dumps(unsigned, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    if (record.get('schema') != 'citywide-osm-inspection-v1' or record.get('scanner_revision') != 1
            or record.get('osmium_version') != importlib.metadata.version('osmium')
            or record.get('source_sha256') != source_sha256
            or record.get('record_sha256') != fingerprint):
        raise OSMValidationError('OSM inspection cache source/scanner/fingerprint mismatch; rescan required')
    if type(record.get('source_bytes')) is not int or record['source_bytes'] <= 0:
        raise OSMValidationError('OSM cache source size is invalid')
    report = record['inspection']
    if report.get('source_bytes', record['source_bytes']) != record['source_bytes']:
        raise OSMValidationError('OSM cache source sizes disagree')
    if report.get('status', 'inspected') != 'inspected' or report.get('format_parseable') is not True or report.get('geometry_crs') != 'EPSG:4326':
        raise OSMValidationError('OSM cache does not describe a completed valid inspection')
    counts = report.get('counts', {})
    for key in ('nodes', 'ways', 'way_nodes', 'max_way_nodes', 'relations', 'area_relations', 'area_relation_members'):
        if key not in counts or type(counts[key]) is not int or counts[key] < 0:
            raise OSMValidationError('OSM cache count schema is invalid')
    if counts.get('nodes', 0) > max_nodes or counts.get('ways', 0) > max_ways or counts.get('relations', 0) > max_relations:
        raise OSMResourceError('Cached complete source exceeds current count caps; geographical extent unchanged')
    incomplete = report.get('incomplete_relation_ids')
    if not isinstance(incomplete, list) or len(incomplete) > 1000 or any(type(v) is not int or v <= 0 for v in incomplete):
        raise OSMValidationError('OSM cache incomplete-relation schema is invalid')
    incremental = counts.get('nodes', 0) * 40 + counts.get('area_relation_members', 0) * 512 + 128 * MIB
    if report.get('estimated_incremental_memory_bytes') != incremental:
        raise OSMValidationError('Cached OSM memory estimate disagrees with complete source counts')
    return {**report, 'inspection_cache_reused': True}


def write_inspection_cache(pbf: Path, path: Path, completed_report: dict, budget) -> dict:
    """Persist an actual completed inspection under the caller's shared budget."""
    from seoul_visibility.acquisition_safety import atomic_json
    record = inspection_cache_record(pbf, completed_report)
    path = budget.safe_path(path)
    if path.exists():
        if path.stat().st_size > 8 * MIB:
            raise OSMValidationError('OSM inspection cache exceeds bounded metadata size')
        previous = json.loads(path.read_text())
        _validated_inspection_record(previous, record['source_sha256'], max_nodes=50_000_000,
                                     max_relations=150_000, max_ways=5_000_000)
        if previous['inspection'] != completed_report:
            raise OSMValidationError('Existing source inspection differs; preserve a separately versioned record')
        return previous
    atomic_json(path, record, budget)
    return record


def audit_context_coverage(path: Path, recommendation_wgs84, support_wgs84, *, grid_m: float = 5000) -> dict:
    """Distributed observations, including empty cells and boundary regions.

    Mapped observations are not inventory-completeness or public-access claims.
    """
    from pyproj import Transformer
    from shapely.geometry import box
    project = Transformer.from_crs(4326, 5186, always_xy=True).transform
    city, support = transform(project, recommendation_wgs84), transform(project, support_wgs84)
    if grid_m < 1000:
        raise ValueError('Coverage audit grid must be at least 1000 m to bound report size')
    db = sqlite3.connect(f'file:{Path(path).resolve()}?mode=ro', uri=True)
    grid = []
    try:
        layers = [x[0] for x in db.execute('SELECT table_name FROM gpkg_geometry_columns')]
        xmin, ymin, xmax, ymax = support.bounds
        for gx in range(math.floor(xmin / grid_m), math.ceil(xmax / grid_m)):
            for gy in range(math.floor(ymin / grid_m), math.ceil(ymax / grid_m)):
                tile = box(gx * grid_m, gy * grid_m, (gx + 1) * grid_m, (gy + 1) * grid_m)
                part = tile.intersection(support)
                if part.is_empty:
                    continue
                row = {'grid_id': f'{gx}:{gy}', 'support_area_m2': part.area,
                       'seoul_area_m2': tile.intersection(city).area, 'features': {}}
                for layer in layers:
                    if layer not in LAYER_NAMES:
                        continue
                    seen = 0
                    for blob, in db.execute(f'''SELECT f.geom FROM {layer} f JOIN rtree_{layer}_geom r ON r.id=f.fid
                        WHERE r.minx<=? AND r.maxx>=? AND r.miny<=? AND r.maxy>=?''',
                        (tile.bounds[2], tile.bounds[0], tile.bounds[3], tile.bounds[1])):
                        seen += int(decode_gpkg(blob).intersects(part))
                    row['features'][layer] = seen
                grid.append(row)
        districts = []
        if 'boundaries' in layers:
            for source_id, name, tags, blob in db.execute('SELECT source_id,name,tags_json,geom FROM boundaries'):
                tags = json.loads(tags)
                geometry = decode_gpkg(blob)
                if tags.get('admin_level') == '6' and geometry.intersects(city) and geometry.intersection(city).area > geometry.area * 0.5:
                    districts.append({'source_id': source_id, 'name': name, 'area_m2': geometry.area})
        return {'grid_m': grid_m, 'grid_cells': grid, 'mapped_seoul_districts': districts,
                'district_count': len(districts), 'requested_seoul_area_m2': city.area,
                'requested_support_area_m2': support.area,
                'coverage_interpretation': 'Mapped-feature observations; absence and bounding boxes do not certify inventory completeness.'}
    finally:
        db.close()


def native_tagged_node_filter():
    """Skip only empty-tag NODE callbacks after all native location/area handling.

    Verified against installed pyosmium 4.1.1 simple_handler.py: handlers are
    [NodeLocationsForWays, area.second_pass_handler(*filters,self), *filters,self].
    BaseFilter.enable_for's documented rule automatically passes other types.
    Thus untagged way members, relation members, and area holes remain intact.
    """
    import osmium
    native = osmium.filter.EmptyTagFilter()
    native.enable_for(osmium.osm.osm_entity_bits.NODE)
    return native


def _normalize_osm_once(pbf: Path, output: Path, recommendation_wgs84, support_wgs84,
                  check_budget: Callable[[int], None], *, max_output_bytes: int = 768 * MIB,
                  memory_limit_bytes: int = 1024 * MIB, max_nodes: int = 50_000_000,
                  max_features: int = 2_000_000, max_relations: int = 150_000,
                  publication_callback=None, preflight_report=None) -> dict:
    """Read the complete permitted snapshot; select complete support-area objects.

    The parent should run this in a worker with an address-space/RSS margin. The
    sparse RAM node index avoids a huge dense array indexed by global OSM IDs.
    All nodes are counted in verified preflight; RSS is sampled every 4096 tagged
    node/way callbacks. Native location handling precedes tagged-node filtering.
    libosmium read buffers/area assembler retain a reserved 128 MiB lag margin.
    """
    import osmium
    import psutil
    from pyproj import Transformer
    validate_contract_geometry(recommendation_wgs84, crs='EPSG:4326')
    validate_contract_geometry(support_wgs84, crs='EPSG:4326')
    if not support_wgs84.covers(recommendation_wgs84):
        raise OSMValidationError('Support geometry does not cover the recommendation boundary')
    if not Path(pbf).is_file():
        raise FileNotFoundError(pbf)
    if memory_limit_bytes < 256 * MIB:
        raise OSMResourceError('OSM memory allowance must include at least 256 MiB')
    preflight = preflight_report or inspect_osm(pbf, max_nodes=max_nodes, max_relations=max_relations)
    if psutil.Process().memory_info().rss + preflight['estimated_incremental_memory_bytes'] > memory_limit_bytes:
        raise OSMResourceError(f"OSM memory plan needs {preflight['estimated_incremental_memory_bytes']} incremental bytes before assembly")
    project = Transformer.from_crs(4326, 5186, always_xy=True).transform
    writer = BoundedGPKG(output, check_budget, max_output_bytes, publication_callback)
    for name in LAYER_NAMES:
        writer.create_layer(name)
    factory = osmium.geom.WKBFactory()
    process = psutil.Process()
    counts = Counter()
    expected, assembled = set(), set()
    expected_ways, assembled_ways = set(), set()
    incomplete_relations = {f'relation/{identifier}' for identifier in preflight['incomplete_relation_ids']}
    issues = []
    peak_rss = 0

    def pressure(check_storage=False):
        nonlocal peak_rss
        if check_storage:
            check_budget(0)
        rss = process.memory_info().rss
        peak_rss = max(rss, peak_rss)
        if rss + 128 * MIB > memory_limit_bytes:
            raise OSMResourceError('OSM RSS reached bounded allowance including 128 MiB decoder margin')

    def issue(source_id, reason):
        counts['geometry_issues'] += 1
        if len(issues) >= 1000:
            raise OSMValidationError('More than 1000 geometry issues; bounded diagnostic limit reached')
        issues.append({'source_id': source_id, 'reason': reason})

    def source_time(obj):
        timestamp = obj.timestamp
        return timestamp.isoformat() if timestamp is not None and timestamp.year > 1970 else None

    def emit(source_id, geometry, tags, version=None, timestamp=None):
        if source_id in incomplete_relations:
            return  # Raw members remain available; incomplete shapes are not published.
        if geometry.is_empty or not geometry.intersects(support_wgs84):
            return
        if not geometry.is_valid:
            issue(source_id, 'Invalid source geometry retained in raw PBF; no silent repair/drop')
            return
        projected = transform(project, geometry)
        if not projected.is_valid:
            issue(source_id, 'Invalid geometry after horizontal projection')
            return
        evidence = {**access_evidence(tags), 'horizontal_crs': 'EPSG:5186',
                    'height_reference': None, 'height_m': None, 'elevation_tag_uninterpreted': tags.get('ele'),
                    'geometry_selection': 'complete object intersects support polygon',
                    'source_id': source_id}
        matches = _category_matches(tags, geometry.geom_type)
        selected_categories = list(dict.fromkeys(matches))
        counts['duplicate_category_matches_merged'] += len(matches) - len(selected_categories)
        for category in selected_categories:
            # Area-enabled ways are emitted separately as polygons; walking
            # paths stay lines and do not become walkable park interiors.
            if category == 'paths' and geometry.geom_type not in {'LineString', 'MultiLineString'}:
                continue
            if counts['output_features'] >= max_features:
                raise OSMResourceError('Maximum normalized feature count reached')
            writer.add(category, source_id, projected, tags, evidence, version, timestamp)
            counts['output_features'] += 1

    class Handler(osmium.SimpleHandler):
        def node(self, node):
            counts['tagged_node_callbacks'] += 1
            if counts['tagged_node_callbacks'] % 4096 == 0:
                pressure(counts['tagged_node_callbacks'] % 32768 == 0)
            tags = dict(node.tags)
            if categories(tags) and node.location.valid():
                emit(f'node/{node.id}', Point(node.lon, node.lat), tags, node.version or None, source_time(node))

        def way(self, way):
            counts['ways_read'] += 1
            if counts['ways_read'] % 4096 == 0:
                pressure()
            tags = dict(way.tags)
            cats = categories(tags)
            if not cats:
                return
            if len(way.nodes) > 250_000:
                raise OSMResourceError('Way exceeds coordinate decode cap')
            try:
                coords = assert_complete_way(way.nodes)
            except OSMValidationError as error:
                issue(f'way/{way.id}', str(error))
                return
            # Closed area features are emitted by libosmium area callbacks.
            # Closed linear paths/rivers/ridges remain lines when area!=yes.
            linear = ('paths' in cats or tags.get('waterway') in {'river', 'stream', 'canal', 'drain', 'ditch'}
                      or tags.get('natural') in {'ridge', 'cliff'} or 'barrier' in tags)
            if not way.is_closed() or (linear and tags.get('area') != 'yes'):
                emit(f'way/{way.id}', LineString(coords), tags, way.version or None, source_time(way))
            else:
                expected_ways.add(way.id)

        def relation(self, relation):
            counts['relations_read'] += 1
            if counts['relations_read'] % 512 == 0:
                pressure()
            tags = dict(relation.tags)
            if tags.get('type') in {'multipolygon', 'boundary'} and categories(tags):
                expected.add(relation.id)
                if len(expected) > max_relations or len(relation.members) > 250_000:
                    raise OSMResourceError('Relation assembler metadata bound reached')

        def area(self, area):
            tags = dict(area.tags)
            if not categories(tags):
                return
            source_id = f'{"way" if area.from_way() else "relation"}/{area.orig_id()}'
            coords = sum(len(r) + sum(len(h) for h in area.inner_rings(r)) for r in area.outer_rings())
            if coords > 250_000:
                raise OSMResourceError('Area exceeds 250,000-coordinate decode cap')
            try:
                geometry = from_wkb(factory.create_multipolygon(area))
            except (RuntimeError, ValueError) as error:
                issue(source_id, f'Area construction failed: {type(error).__name__}')
                return
            if not area.from_way():
                assembled.add(area.orig_id())
            else:
                assembled_ways.add(area.orig_id())
            # Pure closed line tags do not also create irrelevant area records.
            if tags.get('area') != 'yes':
                linear = ('paths' in categories(tags)
                          or tags.get('waterway') in {'river', 'stream', 'canal', 'drain', 'ditch'}
                          or tags.get('natural') in {'ridge', 'cliff'} or 'barrier' in tags)
                if area.from_way() and linear:
                    return
            emit(source_id, geometry, tags, area.version or None, source_time(area))

    try:
        pressure(True)
        Handler().apply_file(str(pbf), locations=True, idx='sparse_mem_array', filters=[native_tagged_node_filter()])
        pressure(True)
        counts['nodes_read'] = preflight['counts']['nodes']
        relation_report = relation_completeness(expected, assembled)
        missing = sorted(set(relation_report['unassembled_relation_ids']) | set(preflight['incomplete_relation_ids']))
        relation_report.update(unassembled_relation_ids=missing, relation_geometry_complete=not missing)
        missing_ways = sorted(expected_ways - assembled_ways)
        if len(missing_ways) > 1000:
            raise OSMValidationError('More than 1000 unassembled closed ways; source retained')
        for identifier in missing_ways:
            issue(f'way/{identifier}', 'Closed area way was not assembled')
        # Incomplete relevant relations are retained as explicit diagnostics.
        # They prevent readiness even if valid independent objects are published.
        diagnostics = aggregate_geometry_diagnostics(issues, relation_report['unassembled_relation_ids'])
        diagnostic_marker = transform(project, recommendation_wgs84.representative_point())
        for source_id, evidence in diagnostics:
            writer.add('quality', source_id, diagnostic_marker, {}, evidence)
        semantic = {'counts': dict(counts), 'relations': relation_report, 'geometry_issues': issues,
                'geometry_repairs': 0, 'peak_rss_bytes_sampled': peak_rss, 'memory_preflight': preflight,
                'normalized_ready': not issues,
                'geographic_completeness_confirmed': relation_report['relation_geometry_complete'] and not issues,
                'source_crs': 'EPSG:4326', 'output_crs': 'EPSG:5186', 'height_units': None,
                'classification_duplicate_policy': 'Repeated tags matching one layer produce one identical source geometry; count recorded in duplicate_category_matches_merged; cross-layer features retain source references',
                'node_callback_policy': 'Native empty-tag NODE filter after complete node-location caching and area assembly; all way/relation members preserved',
                'count_cap_policy': {'nodes': max_nodes, 'ways': 5_000_000, 'relations': max_relations,
                    'reason': 'Pinned South Korea snapshot has 38,346,399 nodes; counts do not override memory preflight/RSS limits'},
                'source_sha256': _sha256(Path(pbf)), 'license': 'ODbL-1.0',
                'attribution': '© OpenStreetMap contributors',
                'limitations': ['Mapped objects do not establish public access, current opening, visibility, or field validation.',
                                'Complete snapshot extraction does not prove real-world inventory completeness.',
                                'No terrain elevation or building height was inferred from OSM ele/height tags.',
                                'Complete intersecting geometries may extend outside the requested support polygon.']}
        result = writer.publish(extra_report=semantic)
    except BaseException:
        writer.close()
        raise
    return result


def sample_path_candidates(source_id: str, line, tags: dict, boundary,
                           is_obstructed: Callable, spacing_m: float = 20.0):
    """Stable source-way chainages; no park-interior inference or height imputation."""
    if not math.isfinite(spacing_m) or spacing_m < 1:
        raise ValueError('Candidate spacing must be finite and at least one metre')
    if line.geom_type != 'LineString' or not line.is_valid:
        return
    evidence = access_evidence(tags)
    if evidence['status'] in {'restricted', 'conditional_unresolved'}:
        return
    highway = tags.get('highway')
    if highway not in {'path', 'footway', 'pedestrian', 'steps', 'living_street', 'residential', 'service', 'track'}:
        if tags.get('foot') not in {'yes', 'designated', 'official'} and not any(tags.get(key) in {'yes', 'both', 'left', 'right', 'separate'} for key in ('sidewalk', 'sidewalk:left', 'sidewalk:right')):
            return
    elevated = tags.get('bridge') not in {None, 'no'} or tags.get('tunnel') not in {None, 'no'} or tags.get('layer') not in {None, '0'} or tags.get('indoor') == 'yes'
    for sample in range(int(line.length // spacing_m) + 1):
        chainage = sample * spacing_m
        point = line.interpolate(chainage)
        if not boundary.covers(point) or is_obstructed(point):
            continue
        identifier = 'osm-' + hashlib.sha256(f'{source_id}|{spacing_m:.6f}|{sample}'.encode()).hexdigest()[:24]
        yield identifier, point, {**evidence, 'source_id': source_id, 'source_chainage_m': chainage,
            'sampling_kind': 'path_chainage',
            'spacing_m': spacing_m, 'observer_elevation_status': 'unsupported_structure' if elevated else 'terrain_required',
            'bridge_tunnel_layer_uncertainty': elevated, 'scenic': None, 'visibility': None,
            'building_water_exclusion': 'mapped_geometry_only', 'standing_location_uncertainty': 'mapped path centerline; usable standing width unverified'}


def supported_pedestrian_area(tags: dict) -> bool:
    """Park/garden/square labels alone do not establish pedestrian surfaces."""
    if access_evidence(tags)['status'] in {'restricted', 'conditional_unresolved'}:
        return False
    return ((tags.get('highway') == 'pedestrian' and tags.get('area') == 'yes')
            or (tags.get('place') == 'square' and tags.get('foot') in {'yes', 'designated', 'official'}))


def sample_public_space_candidates(source_id: str, geometry, tags: dict, boundary,
                                   is_obstructed: Callable, spacing_m: float = 20.0,
                                   max_grid_points: int = 1_000_000):
    """Stream fixed EPSG:5186 grid samples of explicitly mapped pedestrian areas.

    Geographic contract and spacing remain fixed. An oversized probe is a
    resource checkpoint, never permission to drop an eligible area silently.
    """
    if not math.isfinite(spacing_m) or spacing_m < 1:
        raise ValueError('Candidate spacing must be finite and at least one metre')
    if not supported_pedestrian_area(tags):
        return
    if geometry.geom_type not in {'Polygon', 'MultiPolygon'} or not geometry.is_valid:
        raise OSMValidationError(f'Eligible public-space geometry is not a valid polygon: {source_id}')
    clipped = geometry.intersection(boundary)
    if clipped.is_empty or clipped.area <= 0:
        return
    xmin, ymin, xmax, ymax = clipped.bounds
    low_x, high_x = math.ceil(xmin / spacing_m), math.floor(xmax / spacing_m)
    low_y, high_y = math.ceil(ymin / spacing_m), math.floor(ymax / spacing_m)
    probes = max(0, high_x - low_x + 1) * max(0, high_y - low_y + 1)
    if probes > max_grid_points:
        raise OSMResourceError(f'Public-space {source_id} requires {probes} grid probes; bounded allowance is {max_grid_points}; geometry and spacing preserved')
    evidence = access_evidence(tags)
    elevated = tags.get('bridge') not in {None, 'no'} or tags.get('tunnel') not in {None, 'no'} or tags.get('layer') not in {None, '0'} or tags.get('indoor') == 'yes'
    for gx in range(low_x, high_x + 1):
        for gy in range(low_y, high_y + 1):
            point = Point(gx * spacing_m, gy * spacing_m)
            if not clipped.covers(point) or is_obstructed(point):
                continue
            identifier = 'osm-area-' + hashlib.sha256(f'{source_id}|{spacing_m:.6f}|{gx}|{gy}'.encode()).hexdigest()[:24]
            yield identifier, point, {**evidence, 'source_id': source_id,
                'sampling_kind': 'explicit_pedestrian_area_grid', 'grid_index': [gx, gy],
                'grid_origin_epsg5186': [0, 0], 'spacing_m': spacing_m,
                'observer_elevation_status': 'unsupported_structure' if elevated else 'terrain_required',
                'bridge_tunnel_layer_uncertainty': elevated, 'scenic': None, 'visibility': None,
                'building_water_exclusion': 'mapped_geometry_only',
                'standing_location_uncertainty': 'mapped pedestrian-area surface; local obstacles, standing usability, and current access unverified'}


def _generate_candidates_once(context_gpkg: Path, buildings_gpkg: Path, buildings_layer: str,
                        recommendation_wgs84, output: Path, check_budget: Callable[[int], None], *,
                        spacing_m: float = 20.0, max_output_bytes: int = 512 * MIB,
                        max_candidates: int = 1_000_000, publication_callback=None,
                        max_public_space_grid_points: int = 1_000_000) -> dict:
    """Generate bounded path and explicit pedestrian-area standing candidates.

    Uses indexed OGR spatial filters on building and water geometry, one standing
    point at a time; no all-city union or in-memory building inventory.
    """
    from osgeo import ogr
    from pyproj import Transformer
    validate_contract_geometry(recommendation_wgs84, crs='EPSG:4326')
    if not Path(buildings_gpkg).is_file():
        raise OSMValidationError('Candidate generation requires the acquired building exclusion input')
    ogr.UseExceptions()
    buildings = ogr.Open(str(buildings_gpkg))
    obstruction = buildings.GetLayerByName(buildings_layer) if buildings else None
    context = ogr.Open(str(context_gpkg))
    water = context.GetLayerByName('water') if context else None
    paths = context.GetLayerByName('paths') if context else None
    spaces = context.GetLayerByName('public_spaces') if context else None
    if obstruction is None or water is None or paths is None or spaces is None:
        raise OSMValidationError('Missing building/water/path/public-space layer')
    for layer in (obstruction, water, paths, spaces):
        srs = layer.GetSpatialRef()
        if srs is None:
            raise OSMValidationError('Candidate exclusion input has missing CRS')
        srs.AutoIdentifyEPSG()
        if srs.GetAuthorityCode(None) != '5186':
            raise OSMValidationError('Candidate input requires transformed EPSG:5186 geometry')
    project = Transformer.from_crs(4326, 5186, always_xy=True).transform
    boundary = transform(project, recommendation_wgs84)
    writer = BoundedGPKG(output, check_budget, max_output_bytes, publication_callback)
    writer.create_layer('candidates')
    groups = Counter()
    sampling_counts = Counter()
    eligible_areas = 0

    def blocked(point):
        search = ogr.CreateGeometryFromWkb(point.wkb)
        for layer in (obstruction, water):
            layer.SetSpatialFilter(search)
            for feature in layer:
                geom = feature.GetGeometryRef()
                if geom and geom.Intersects(search):
                    return True
        return False

    count = 0
    try:
        for feature in paths:
            geometry = feature.GetGeometryRef()
            if geometry is None:
                raise OSMValidationError('Path lacks geometry')
            line = from_wkb(bytes(geometry.ExportToWkb()))
            tags = json.loads(feature['tags_json'])
            for identifier, point, evidence in sample_path_candidates(feature['source_id'], line, tags, boundary, blocked, spacing_m):
                if count >= max_candidates:
                    raise OSMResourceError('Candidate count cap reached; geographical contract was not reduced')
                writer.add('candidates', identifier, point, tags, evidence)
                # Deterministic 5 km EPSG:5186 audit grid, including boundary areas.
                groups[f'{int(point.x // 5000)}:{int(point.y // 5000)}'] += 1
                sampling_counts['path_chainage'] += 1
                count += 1
        for feature in spaces:
            tags = json.loads(feature['tags_json'])
            if not supported_pedestrian_area(tags):
                continue
            geometry = feature.GetGeometryRef()
            if geometry is None or geometry.WkbSize() > 8 * MIB:
                raise OSMValidationError('Eligible public-space geometry is absent or exceeds the 8 MiB decoding bound')
            polygon = from_wkb(bytes(geometry.ExportToWkb()))
            eligible_areas += 1
            for identifier, point, evidence in sample_public_space_candidates(feature['source_id'], polygon, tags,
                    boundary, blocked, spacing_m, max_public_space_grid_points):
                if count >= max_candidates:
                    raise OSMResourceError('Candidate count cap reached at an eligible public area; geography and spacing preserved')
                writer.add('candidates', identifier, point, tags, evidence)
                groups[f'{int(point.x // 5000)}:{int(point.y // 5000)}'] += 1
                sampling_counts['explicit_pedestrian_area_grid'] += 1
                count += 1
        semantic = {'candidate_count': count, 'spacing_m': spacing_m, 'counts_by_5km_grid': dict(groups),
                'resource_policy': {'maximum_output_bytes':max_output_bytes,'maximum_candidates':max_candidates,
                    'maximum_grid_probes_per_public_space':max_public_space_grid_points},
                'sampling': 'source-way chainage before city clipping; fixed EPSG:5186 grid inside explicitly supported pedestrian areas',
                'counts_by_sampling_kind': dict(sampling_counts), 'eligible_public_space_features': eligible_areas,
                'public_area_policy': 'highway=pedestrian+area=yes or place=square+explicit foot permission; restricted and conditional access excluded',
                'field_verified': False,
                'limitations': ['Candidate exclusions cover mapped source geometry only.',
                                'Park interiors are not assumed walkable.', 'Elevated structures have unsupported observer elevations.']}
        result = writer.publish(extra_report=semantic)
    except BaseException:
        writer.close()
        raise
    return result


def _checkpoint_json(path: Path, value: dict, check_budget, budget=None):
    """Small atomic journal writes; unique .writing names preserve interruptions."""
    encoded = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()
    if len(encoded) > 8 * MIB:
        raise OSMResourceError('Normalization checkpoint exceeds its 8 MiB metadata cap')
    if budget is not None:
        budget.safe_path(path)
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise OSMValidationError('Checkpoint path contains a symlink')
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.writing')
    check_budget(len(encoded) + 16384)
    path.parent.mkdir(parents=True, exist_ok=True)
    with temporary.open('xb') as destination:
        destination.write(encoded)
        destination.flush()
        os.fsync(destination.fileno())
    temporary.replace(path)


def _resume_product(output: Path, recipe: dict, recovery_sources: dict, check_budget,
                    operation, *, budget=None, validator=None, partial_path=None,
                    rebuild_peak_bytes: int | None = None) -> dict:
    """Reuse validated outputs, promote durable receipts, or rebuild owned partials.

    The whole-stage shared reservation is held by the caller. A per-artifact lock
    also protects callers using only a test/dedicated budget callback. Existing
    final outputs whose inputs changed are invalidated and preserved, never
    silently overwritten. Source data and previous completed outputs are retained.
    """
    output = Path(output).absolute()
    if budget is not None:
        output = budget.safe_path(output)
    if output.is_symlink() or any(parent.is_symlink() for parent in output.parents):
        raise OSMValidationError('Output path contains a symlink')
    partial = Path(partial_path).absolute() if partial_path is not None else output.with_name(output.name + '.part')
    if budget is not None:
        partial = budget.safe_path(partial)
    if partial.parent != output.parent:
        raise OSMValidationError('Normalized partial and final must use the same publication directory')
    metadata = output.with_suffix('.source.json')
    pending = output.with_suffix('.source.json.part')
    owner = output.with_suffix('.owner.json')
    lock = output.with_suffix('.worker.lock')
    preservation = output.with_suffix('.preservation.json')
    recipe_hash = hashlib.sha256(json.dumps(recipe, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    check_budget(16384)
    output.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise OSMResourceError('Another live worker owns this normalized artifact; files preserved') from error
        for path in (partial, metadata, pending, owner, preservation):
            if path.is_symlink():
                raise OSMValidationError('Normalization sidecar symlink is not permitted')
            if path.exists() and path in (metadata, pending, owner, preservation) and path.stat().st_size > 8 * MIB:
                raise OSMValidationError('Normalization metadata exceeds bounded read size')

        def verified_receipt(path):
            value = json.loads(path.read_text())
            if value.get('recipe') != recipe or value.get('recipe_sha256') != recipe_hash:
                raise OSMValidationError('Normalization source/settings changed; dependent output invalidated and preserved')
            return value

        def verify_file(path, receipt):
            if receipt.get('sha256') != _sha256(path):
                raise OSMValidationError('Normalized artifact checksum mismatch; corrupt/truncated file preserved')
            if not (validator or validate_gpkg)(path)['valid']:
                raise OSMValidationError('Normalized artifact geometry/index validation failed')

        if output.exists():
            report_path = metadata if metadata.exists() else pending if pending.exists() else None
            if report_path is None:
                raise OSMValidationError('Final output lacks a durable source receipt; preserved without claiming validity')
            report = verified_receipt(report_path)
            verify_file(output, report)
            if report_path == pending:
                check_budget(8192)
                pending.replace(metadata)
            return {**report, 'reused': True}

        if pending.exists() and partial.exists():
            report = verified_receipt(pending)
            try:
                verify_file(partial, report)
            except (ValueError, sqlite3.DatabaseError):
                pass  # A receipt is not permission to publish a corrupt partial.
            else:
                check_budget(8192)
                partial.replace(output)
                pending.replace(metadata)
                return {**report, 'reused': True, 'recovered_atomic_publication': True}

        preserved = json.loads(preservation.read_text()) if preservation.exists() else {
            'schema': 'citywide-preserved-interrupted-products-v1', 'attempts': []}
        if partial.exists():
            if budget is None or not owner.exists():
                raise OSMValidationError('Partial preserved: a persisted ownership record and shared Budget are required for safe rebuild')
            ownership = json.loads(owner.read_text())
            if (ownership.get('schema') != 'citywide-owned-partial-v1'
                    or ownership.get('task_owned') is not True
                    or ownership.get('exclusive_creation') is not True
                    or Path(ownership.get('path', '')).absolute() != partial):
                raise OSMValidationError('Interrupted artifact has no matching durable ownership; preserved')
            if not isinstance(ownership.get('recovery_sources'), dict) or not ownership['recovery_sources']:
                raise OSMValidationError('Interrupted artifact lacks registered recovery-source ownership; preserved')
            for record in ownership['recovery_sources'].values():
                path = budget.safe_path(record['path'])
                if not path.is_file() or _sha256(path) != record['sha256']:
                    raise OSMValidationError('Recovery input changed or missing; interrupted output is preserved')
            if type(rebuild_peak_bytes) is not int or rebuild_peak_bytes <= 0:
                raise OSMResourceError('Interrupted output retained: an explicit full rebuild peak is required before a new attempt')
            # Existing partial bytes remain in the shared baseline. Check the
            # *full* successor peak before renaming anything; no recovery source
            # or old artifact is deleted to manufacture headroom.
            check_budget(rebuild_peak_bytes + 16 * MIB)
            related = [partial]
            registered = set(ownership.get('associated_paths', []))
            for suffix in ('-wal', '-shm', '-journal'):
                sidecar = Path(str(partial) + suffix)
                if sidecar.exists():
                    if str(sidecar) not in registered or sidecar.is_symlink():
                        raise OSMValidationError('Unregistered interrupted SQLite sidecar preserved; inspect ownership before rebuild')
                    related.append(sidecar)
            attempt = output.parent / f'.{output.stem}.interrupted-{uuid.uuid4().hex}.partial'
            budget.safe_path(attempt)
            attempt.mkdir(exist_ok=False)
            artifacts = [{'original_path': str(path), 'preserved_path': str(attempt / path.name),
                          'sha256': _sha256(path), 'logical_bytes': path.stat().st_size,
                          'allocated_bytes': path.stat().st_blocks * 512} for path in related]
            record = {'status': 'preservation_planned', 'attempt_directory': str(attempt),
                      'artifacts': artifacts, 'ownership_copy': str(attempt / 'ownership.json'),
                      'new_output_bound_bytes': rebuild_peak_bytes,
                      'deletion_performed': False,
                      'reason': 'Retain interrupted output through rebuild and after validated successor'}
            _checkpoint_json(attempt / 'ownership.json', ownership, check_budget, budget)
            preserved['attempts'].append(record)
            _checkpoint_json(preservation, preserved, check_budget, budget)
            # Both endpoints share the publication filesystem; rename has no
            # duplicate payload cost and preserves every received/generated byte.
            for artifact in artifacts:
                target = Path(artifact['preserved_path'])
                budget.safe_path(target)
                Path(artifact['original_path']).replace(target)
            if pending.exists():
                pending.replace(attempt / 'previous-pending-receipt.json')
                record['previous_pending_receipt'] = str(attempt / 'previous-pending-receipt.json')
            record['status'] = 'preserved'
            _checkpoint_json(preservation, preserved, check_budget, budget)

        sources = {key: {'path': str(Path(path).absolute()), 'sha256': digest}
                   for key, (path, digest) in recovery_sources.items()}
        first = next(iter(sources.values()))
        ownership = {'schema': 'citywide-owned-partial-v1', 'task_owned': True,
            'disposable': True, 'exclusive_creation': True, 'path': str(partial),
            'recovery_source': first['path'], 'recovery_source_sha256': first['sha256'],
            'recovery_sources': sources, 'recipe': recipe, 'recipe_sha256': recipe_hash,
            'associated_paths': [str(partial) + suffix for suffix in ('-wal', '-shm', '-journal')]}
        _checkpoint_json(owner, ownership, check_budget, budget)

        def before_publish(report):
            # The full semantic report and file checksum are durable BEFORE the
            # final filename becomes visible; either publication boundary resumes.
            complete = {**report, 'recipe': recipe, 'recipe_sha256': recipe_hash,
                        'source_manifest': metadata.name, 'reused': False,
                        'preserved_interrupted_attempts': preserved['attempts']}
            _checkpoint_json(pending, complete, check_budget, budget)

        operation(before_publish)
        if not output.exists() or not pending.exists():
            raise OSMValidationError('Worker did not create a durable validated publication receipt')
        report = verified_receipt(pending)
        verify_file(output, report)
        check_budget(8192)
        pending.replace(metadata)
        return report
    finally:
        os.close(fd)


def normalize_osm(pbf: Path, output: Path, recommendation_wgs84, support_wgs84,
                  check_budget: Callable[[int], None], *, max_output_bytes: int = 768 * MIB,
                  memory_limit_bytes: int = 1024 * MIB, max_nodes: int = 50_000_000,
                  max_features: int = 2_000_000, max_relations: int = 150_000,
                  budget=None, inspection_cache: Path | None = None) -> dict:
    """Restartable public wrapper; interrupted partials remain accounted and retained."""
    digest = _sha256(Path(pbf))
    preflight = None
    if inspection_cache is not None:
        cache = Path(inspection_cache)
        if budget is not None:
            cache = budget.safe_path(cache)
        if not cache.is_file() or cache.is_symlink() or cache.stat().st_size > 8 * MIB:
            raise OSMValidationError('Requested OSM inspection cache missing, unsafe or oversized')
        record = json.loads(cache.read_text())
        if record.get('source_bytes') != Path(pbf).stat().st_size:
            raise OSMValidationError('OSM inspection cache source size mismatch')
        preflight = _validated_inspection_record(record, digest, max_nodes=max_nodes,
            max_relations=max_relations, max_ways=5_000_000)
    recipe = {'version': 2, 'kind': 'osm-normalization', 'source_sha256': digest,
        'recommendation_sha256': hashlib.sha256(recommendation_wgs84.wkb).hexdigest(),
        'support_sha256': hashlib.sha256(support_wgs84.wkb).hexdigest(),
        'crs': 'EPSG:5186', 'categories': list(LAYER_NAMES)}
    return _resume_product(output, recipe, {'pbf': (pbf, digest)}, check_budget,
        lambda callback: _normalize_osm_once(pbf, output, recommendation_wgs84, support_wgs84, check_budget,
            max_output_bytes=max_output_bytes, memory_limit_bytes=memory_limit_bytes,
            max_nodes=max_nodes, max_features=max_features, max_relations=max_relations,
            publication_callback=callback, preflight_report=preflight), budget=budget, rebuild_peak_bytes=max_output_bytes)


def generate_candidates(context_gpkg: Path, buildings_gpkg: Path, buildings_layer: str,
                        recommendation_wgs84, output: Path, check_budget: Callable[[int], None], *,
                        spacing_m: float = 20.0, max_output_bytes: int = 512 * MIB,
                        max_candidates: int = 1_000_000, budget=None,
                        max_public_space_grid_points: int = 1_000_000) -> dict:
    """Restartable candidates, pinned to complete context/building input hashes."""
    context_hash, buildings_hash = _sha256(Path(context_gpkg)), _sha256(Path(buildings_gpkg))
    recipe = {'version': 3, 'kind': 'path-and-pedestrian-area-candidates', 'context_sha256': context_hash,
        'buildings_sha256': buildings_hash, 'buildings_layer': buildings_layer,
        'recommendation_sha256': hashlib.sha256(recommendation_wgs84.wkb).hexdigest(),
        'spacing_m': spacing_m, 'crs': 'EPSG:5186'}
    return _resume_product(output, recipe,
        {'context': (context_gpkg, context_hash), 'buildings': (buildings_gpkg, buildings_hash)}, check_budget,
        lambda callback: _generate_candidates_once(context_gpkg, buildings_gpkg, buildings_layer,
            recommendation_wgs84, output, check_budget, spacing_m=spacing_m,
            max_output_bytes=max_output_bytes, max_candidates=max_candidates,
            publication_callback=callback, max_public_space_grid_points=max_public_space_grid_points), budget=budget,
        rebuild_peak_bytes=max_output_bytes)
