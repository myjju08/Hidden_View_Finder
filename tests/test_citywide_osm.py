"""Synthetic, offline OSM fixtures. These tests never acquire geographic data."""
from __future__ import annotations

import importlib
import json
import hashlib
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
from shapely.geometry import LineString, Point, Polygon, box


@pytest.fixture
def osm(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'scripts' / 'data'))
    return importlib.import_module('osm_pipeline')


def test_absent_access_evidence_remains_absent(osm):
    result = osm.access_evidence({'highway': 'path'})
    assert result['status'] == 'unknown'
    assert 'foot' not in result['evidence']
    assert result['currently_open'] is None
    assert result['field_verified'] is False
    explicit = osm.access_evidence({'foot': 'yes', 'opening_hours': '24/7'})
    assert explicit['status'] == 'mapped_permission_unverified'
    assert explicit['field_verified'] is False


def test_conditional_and_foot_override(osm):
    assert osm.access_evidence({'access': 'private', 'foot': 'yes'})['status'] == 'mapped_permission_unverified'
    assert osm.access_evidence({'foot': 'yes', 'foot:conditional': 'no @ (sunset-sunrise)'})['status'] == 'conditional_unresolved'


def test_missing_crs_and_unexplained_coordinates_rejected(osm):
    with pytest.raises(osm.OSMValidationError, match='explicitly declare'):
        osm.validate_contract_geometry(box(126, 37, 128, 38), crs='')
    with pytest.raises(osm.OSMValidationError, match='Unexplained coordinates'):
        osm.validate_contract_geometry(box(200000, 500000, 201000, 501000), crs='EPSG:4326')


def test_incomplete_way_and_relation_not_empty_land(osm):
    ref = SimpleNamespace(ref=42, location=SimpleNamespace(valid=lambda: False))
    with pytest.raises(osm.OSMValidationError, match='required node 42'):
        osm.assert_complete_way([ref])
    report = osm.relation_completeness({10, 11}, {10})
    assert report['relation_geometry_complete'] is False
    assert report['unassembled_relation_ids'] == [11]


def test_geometry_diagnostics_preserve_all_reasons_and_typed_ids(osm):
    issues = [
        {'source_id': 'relation/20', 'reason': 'Area construction failed'},
        {'source_id': 'way/20', 'reason': 'Invalid area geometry'},
        {'source_id': 'way/20', 'reason': 'Closed area way was not assembled'}]
    rows = dict(osm.aggregate_geometry_diagnostics(issues, [20]))
    assert len(rows) == 2
    assert rows['relation/20']['reasons'] == ['Area construction failed', 'Area relation was not assembled']
    assert rows['way/20']['reasons'] == ['Invalid area geometry', 'Closed area way was not assembled']
    assert all(row['diagnostic_count'] == 2 and row['location_unknown'] for row in rows.values())
    assert rows['relation/20']['display_point_is_not_error_location'] is True
    assert len(issues) == 3  # caller's original diagnoses remain intact
    with pytest.raises(osm.OSMValidationError, match='bounded report allowance'):
        osm.aggregate_geometry_diagnostics(issues, [20], max_diagnostics=3)


def test_candidate_ids_stable_city_holes_and_exclusions(osm):
    boundary = Polygon([(0, 0), (100, 0), (100, 100), (0, 100)],
                       holes=[[(30, 30), (70, 30), (70, 70), (30, 70)]])
    line = LineString([(-20, 50), (120, 50)])
    exclusions = box(79, 49, 81, 51)
    args = ('way/12', line, {'highway': 'footway'}, boundary, exclusions.covers)
    points = list(osm.sample_path_candidates(*args))
    assert [p.x for _, p, _ in points] == [0, 20, 100]
    assert [x[0] for x in points] == [x[0] for x in osm.sample_path_candidates(*args)]
    assert all(x[2]['field_verified'] is False and x[2]['visibility'] is None for x in points)
    assert points[0][2]['source_chainage_m'] == 20


def test_candidates_flag_elevated_and_restrict_private(osm):
    args = ('way/2', LineString([(0, 1), (40, 1)]))
    boundary = box(0, 0, 50, 50)
    points = list(osm.sample_path_candidates(*args, {'highway': 'footway', 'bridge': 'yes'}, boundary, lambda p: False))
    assert points and all(p[2]['observer_elevation_status'] == 'unsupported_structure' for p in points)
    assert not list(osm.sample_path_candidates(*args, {'highway': 'footway', 'access': 'private'}, boundary, lambda p: False))
    assert not list(osm.sample_path_candidates(*args, {'highway': 'primary'}, boundary, lambda p: False))


def test_landmark_catalogue_does_not_seed_candidates(osm):
    tags = {'tourism': 'viewpoint'}
    assert osm.categories(tags) == []
    args = ('node/1', Point(2, 2), tags, box(0, 0, 10, 10), lambda p: False)
    assert not list(osm.sample_path_candidates(*args))


def test_overlapping_public_space_tags_have_one_category(osm):
    """Synthetic combination matching the actual square classification failure."""
    tags = {'area': 'yes', 'highway': 'pedestrian', 'place': 'square'}
    assert osm.categories(tags) == ['public_spaces']
    assert osm._category_matches(tags) == ['public_spaces', 'public_spaces']
    assert osm.categories({**tags, 'leisure': 'park'}) == ['public_spaces', 'green_space']


def test_gpkg_atomic_holes_index_and_no_implicit_height(osm, tmp_path):
    pytest.importorskip('pyproj')
    calls = []
    output = tmp_path / 'context.gpkg'
    writer = osm.BoundedGPKG(output, calls.append, 8 * osm.MIB)
    writer.create_layer('water')
    polygon = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)], holes=[[(2, 2), (8, 2), (8, 8), (2, 8)]])
    writer.add('water', 'relation/5', polygon, {'natural': 'water'}, {'height_m': None})
    assert not output.exists() and writer.partial.exists()
    report = writer.publish()
    assert report['valid'] and report['layers']['water']['indexed'] == 1
    assert output.exists() and not writer.partial.exists()
    with sqlite3.connect(output) as db:
        blob, evidence = db.execute('SELECT geom,evidence_json FROM water').fetchone()
        assert len(osm.decode_gpkg(blob).interiors) == 1
        assert json.loads(evidence)['height_m'] is None
    assert calls and all(x >= 0 for x in calls)


def test_budget_refused_before_first_write_and_midstream(osm, tmp_path):
    pytest.importorskip('pyproj')
    output = tmp_path / 'context.gpkg'
    def refuse(_):
        raise osm.OSMResourceError('simulated storage pressure')
    with pytest.raises(osm.OSMResourceError):
        osm.BoundedGPKG(output, refuse, 8 * osm.MIB)
    assert not list(tmp_path.iterdir())
    writer = osm.BoundedGPKG(output, lambda _: None, 8 * osm.MIB)
    writer.create_layer('paths')
    before = writer.partial.stat().st_size
    writer.check_budget = refuse
    with pytest.raises(osm.OSMResourceError):
        writer.add('paths', 'way/1', LineString([(0, 0), (10, 10)]), {}, {})
    assert writer.partial.stat().st_size == before and not output.exists()
    writer.close()
    with pytest.raises(FileExistsError, match='partial exists'):
        osm.BoundedGPKG(output, lambda _: None, 8 * osm.MIB)


def test_partial_symlink_rejected(osm, tmp_path):
    pytest.importorskip('pyproj')
    target = tmp_path / 'existing'
    target.write_text('preserved')
    (tmp_path / 'out.gpkg.part').symlink_to(target)
    with pytest.raises(FileExistsError):
        osm.BoundedGPKG(tmp_path / 'out.gpkg', lambda _: None, 8 * osm.MIB)
    assert target.read_text() == 'preserved'


def test_parent_symlink_rejected_before_creation(osm, tmp_path):
    pytest.importorskip('pyproj')
    outside = tmp_path / 'outside'
    outside.mkdir()
    (tmp_path / 'link').symlink_to(outside, target_is_directory=True)
    with pytest.raises(osm.OSMValidationError, match='parent symlink'):
        osm.BoundedGPKG(tmp_path / 'link' / 'out.gpkg', lambda _: None, 8 * osm.MIB)
    assert not list(outside.iterdir())


def test_offline_validation_rejects_truncation(osm, tmp_path):
    path = tmp_path / 'broken.gpkg'
    path.write_bytes(b'SQLite format 3\x00truncated')
    with pytest.raises((sqlite3.DatabaseError, osm.OSMValidationError)):
        osm.validate_gpkg(path)


def test_synthetic_pbf_multipolygon_hole_and_boundary(osm, tmp_path):
    """Synthetic XML is serialized into a tiny PBF; no real data are requested."""
    osmium = pytest.importorskip('osmium')
    pytest.importorskip('pyproj')
    pytest.importorskip('psutil')
    xml = '''<osm version="0.6" generator="synthetic-test">
      <node id="1" lat="37.50" lon="127.00"/><node id="2" lat="37.50" lon="127.04"/>
      <node id="3" lat="37.54" lon="127.04"/><node id="4" lat="37.54" lon="127.00"/>
      <node id="5" lat="37.51" lon="127.01"/><node id="6" lat="37.51" lon="127.03"/>
      <node id="7" lat="37.53" lon="127.03"/><node id="8" lat="37.53" lon="127.01"/>
      <way id="10"><nd ref="1"/><nd ref="2"/><nd ref="3"/></way>
      <way id="11"><nd ref="3"/><nd ref="4"/><nd ref="1"/></way>
      <way id="12"><nd ref="5"/><nd ref="6"/><nd ref="7"/><nd ref="8"/><nd ref="5"/></way>
      <way id="13"><nd ref="1"/><nd ref="2"/><tag k="highway" v="footway"/></way>
      <relation id="20"><member type="way" ref="10" role="outer"/>
        <member type="way" ref="11" role="outer"/><member type="way" ref="12" role="inner"/>
        <tag k="type" v="multipolygon"/><tag k="natural" v="water"/>
      </relation>
      <relation id="21"><member type="way" ref="10" role="outer"/>
        <member type="way" ref="11" role="outer"/>
        <tag k="type" v="boundary"/><tag k="boundary" v="administrative"/><tag k="admin_level" v="6"/>
      </relation>
    </osm>'''
    source = tmp_path / 'fixture.osm'
    source.write_text(xml)
    pbf = tmp_path / 'fixture.osm.pbf'
    header = osmium.io.Header()
    header.add_box(osmium.osm.Box(126.99, 37.49, 127.05, 37.55))
    with osmium.SimpleWriter(str(pbf), header=header) as pbf_writer:
        for entity in osmium.FileProcessor(str(source)):
            pbf_writer.add(entity)
    with pbf.open('rb') as stream:
        assert b'OSMHeader' in stream.read(65536)
    reader = osmium.io.Reader(str(pbf))
    try:
        assert reader.header().box().valid()
    finally:
        reader.close()
    output = tmp_path / 'context.gpkg'
    report = osm.normalize_osm(pbf, output, box(127.005, 37.505, 127.035, 37.535),
        box(126.99, 37.49, 127.05, 37.55), lambda _: None, max_output_bytes=16 * osm.MIB,
        memory_limit_bytes=1024 * osm.MIB)
    assert report['normalized_ready']
    assert report['layers']['water']['features'] == 1
    assert report['layers']['boundaries']['features'] == 1
    with sqlite3.connect(output) as db:
        polygon = osm.decode_gpkg(db.execute('SELECT geom FROM water').fetchone()[0])
        assert sum(len(p.interiors) for p in polygon.geoms) == 1
    audit = osm.audit_context_coverage(output, box(127.005, 37.505, 127.035, 37.535),
                                     box(126.99, 37.49, 127.05, 37.55))
    assert audit['grid_cells'] and audit['district_count'] == 1
    reused = osm.normalize_osm(pbf, output, box(127.005, 37.505, 127.035, 37.535),
        box(126.99, 37.49, 127.05, 37.55), lambda _: None, max_output_bytes=16 * osm.MIB,
        memory_limit_bytes=1024 * osm.MIB)
    assert reused['reused'] and reused['sha256'] == report['sha256']


def test_gpkg_is_readable_by_existing_gdal(osm, tmp_path):
    pytest.importorskip('pyproj')
    ogr = pytest.importorskip('osgeo.ogr')
    output = tmp_path / 'context.gpkg'
    writer = osm.BoundedGPKG(output, lambda _: None, 8 * osm.MIB)
    writer.create_layer('paths')
    writer.add('paths', 'way/1', LineString([(200000, 500000), (200020, 500020)]), {'highway': 'footway'}, {})
    writer.publish()
    dataset = ogr.Open(str(output))
    layer = dataset.GetLayerByName('paths')
    assert layer.GetSpatialRef().GetAuthorityCode(None) == '5186'
    layer.SetSpatialFilterRect(200005, 500005, 200015, 500015)
    assert layer.GetFeatureCount() == 1


@pytest.fixture
def fake_checkpoint_operation(osm, tmp_path, monkeypatch):
    """Synthetic non-GIS bytes isolate state handling; geometry checks are mocked."""
    monkeypatch.setattr(osm, 'validate_gpkg', lambda path: {'valid': True})
    source = tmp_path / 'source.pbf'
    source.write_bytes(b'synthetic-recoverable-source')
    output = tmp_path / 'normalized.gpkg'
    sources = {'source': (source, osm._sha256(source))}
    def build(callback, *, interrupt=None):
        partial = output.with_name(output.name + '.part')
        partial.write_bytes(b'synthetic-not-real-gpkg')
        callback({'sha256': osm._sha256(partial), 'normalized_ready': True, 'bytes': partial.stat().st_size})
        if interrupt == 'before_final':
            raise InterruptedError('fixture interruption before atomic final')
        partial.replace(output)
        if interrupt == 'after_final':
            raise InterruptedError('fixture interruption before sidecar publication')
    return source, output, sources, build


def test_checkpoint_reuses_valid_output_and_rejects_corruption(osm, fake_checkpoint_operation):
    source, output, sources, build = fake_checkpoint_operation
    first = osm._resume_product(output, {'version': 1}, sources, lambda _: None, build)
    assert first['reused'] is False
    second = osm._resume_product(output, {'version': 1}, sources, lambda _: None,
                                 lambda callback: pytest.fail('Validated stage must skip its worker'))
    assert second['reused'] is True
    output.write_bytes(b'truncated')
    with pytest.raises(osm.OSMValidationError, match='checksum mismatch'):
        osm._resume_product(output, {'version': 1}, sources, lambda _: None, build)
    assert output.read_bytes() == b'truncated'


@pytest.mark.parametrize('interrupt', ['before_final', 'after_final'])
def test_checkpoint_recovers_both_publication_boundaries(osm, fake_checkpoint_operation, interrupt):
    source, output, sources, build = fake_checkpoint_operation
    with pytest.raises(InterruptedError):
        osm._resume_product(output, {'version': 1}, sources, lambda _: None,
                            lambda callback: build(callback, interrupt=interrupt))
    assert output.with_suffix('.source.json.part').exists()
    recovered = osm._resume_product(output, {'version': 1}, sources, lambda _: None,
                                    lambda callback: pytest.fail('Receipt recovery must skip worker'))
    assert recovered['reused'] and recovered['normalized_ready']
    assert output.exists() and output.with_suffix('.source.json').exists()
    assert not output.with_suffix('.source.json.part').exists()


def test_checkpoint_changed_settings_and_unowned_final_are_preserved(osm, fake_checkpoint_operation):
    source, output, sources, build = fake_checkpoint_operation
    osm._resume_product(output, {'version': 1}, sources, lambda _: None, build)
    with pytest.raises(osm.OSMValidationError, match='source/settings changed'):
        osm._resume_product(output, {'version': 2}, sources, lambda _: None, build)
    # Fixture deliberately removes its own receipt to test unknown final files.
    output.with_suffix('.source.json').unlink()
    with pytest.raises(osm.OSMValidationError, match='lacks a durable source receipt'):
        osm._resume_product(output, {'version': 1}, sources, lambda _: None, build)
    assert output.exists()


def test_checkpoint_rebuild_retains_owned_partial_during_and_after_worker(osm, fake_checkpoint_operation):
    source, output, sources, build = fake_checkpoint_operation
    class FixtureBudget:
        def safe_path(self, path): return Path(path).absolute()
    budget = FixtureBudget()
    old_bytes = b'interrupted-synthetic-partial'
    def interrupt(callback):
        output.with_name(output.name + '.part').write_bytes(old_bytes)
        raise InterruptedError('fixture interruption during geometry generation')
    with pytest.raises(InterruptedError):
        osm._resume_product(output, {'version': 1}, sources, lambda _: None, interrupt, budget=budget)
    estimates = []
    def rebuild(callback):
        ledger = json.loads(output.with_suffix('.preservation.json').read_text())
        artifact = ledger['attempts'][-1]['artifacts'][0]
        assert Path(artifact['preserved_path']).read_bytes() == old_bytes
        assert artifact['sha256'] == hashlib.sha256(old_bytes).hexdigest()
        assert not ledger['attempts'][-1]['deletion_performed']
        build(callback)
        assert Path(artifact['preserved_path']).read_bytes() == old_bytes
    result = osm._resume_product(output, {'version': 1}, sources, estimates.append, rebuild,
                                 budget=budget, rebuild_peak_bytes=1024)
    assert result['normalized_ready']
    record = result['preserved_interrupted_attempts'][-1]
    assert Path(record['artifacts'][0]['preserved_path']).read_bytes() == old_bytes
    assert 1024 + 16 * 1024**2 in estimates
    assert not output.with_suffix('.cleanup.json').exists()


def test_checkpoint_rebuild_refusal_preserves_original_partial_before_rename(osm, fake_checkpoint_operation):
    source, output, sources, build = fake_checkpoint_operation
    class FixtureBudget:
        def safe_path(self, path): return Path(path).absolute()
    budget = FixtureBudget()
    partial = output.with_name(output.name + '.part')
    def interrupt(callback):
        partial.write_bytes(b'synthetic-interruption')
        raise InterruptedError()
    with pytest.raises(InterruptedError):
        osm._resume_product(output, {'version': 1}, sources, lambda _: None, interrupt, budget=budget)
    def low_space(size):
        if size > 1024**2:
            raise osm.OSMResourceError('synthetic full rebuild peak does not fit')
    with pytest.raises(osm.OSMResourceError, match='full rebuild peak'):
        osm._resume_product(output, {'version': 1}, sources, low_space, build,
                            budget=budget, rebuild_peak_bytes=1024)
    assert partial.read_bytes() == b'synthetic-interruption'
    assert not output.with_suffix('.preservation.json').exists()
    assert not list(output.parent.glob('.*.partial'))


def test_checkpoint_changed_recovery_source_prevents_partial_cleanup(osm, fake_checkpoint_operation):
    source, output, sources, build = fake_checkpoint_operation
    class FixtureBudget:
        def safe_path(self, path): return Path(path).absolute()
    def interrupt(callback):
        output.with_name(output.name + '.part').write_bytes(b'interrupted-synthetic-partial')
        raise InterruptedError()
    with pytest.raises(InterruptedError):
        osm._resume_product(output, {'version': 1}, sources, lambda _: None, interrupt, budget=FixtureBudget())
    source.write_bytes(b'changed-source')
    with pytest.raises(osm.OSMValidationError, match='Recovery input changed'):
        osm._resume_product(output, {'version': 1}, sources, lambda _: None, build, budget=FixtureBudget())
    assert output.with_name(output.name + '.part').exists()


def test_actual_osmium_missing_relation_member_keeps_uncertainty(osm, tmp_path):
    osmium = pytest.importorskip('osmium')
    pytest.importorskip('pyproj')
    fixture = tmp_path / 'incomplete.osm'
    fixture.write_text('''<osm version="0.6" generator="synthetic-test">
      <node id="1" lat="37.50" lon="127.00"/><node id="2" lat="37.50" lon="127.04"/>
      <node id="3" lat="37.54" lon="127.04"/><node id="4" lat="37.54" lon="127.00"/>
      <way id="10"><nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/></way>
      <way id="11"><nd ref="1"/><nd ref="2"/><tag k="highway" v="footway"/></way>
      <relation id="20"><member type="way" ref="10" role="outer"/>
        <member type="way" ref="999" role="inner"/><tag k="type" v="multipolygon"/>
        <tag k="natural" v="water"/></relation></osm>''')
    pbf = tmp_path / 'incomplete.osm.pbf'
    with osmium.SimpleWriter(str(pbf)) as writer:
        for entity in osmium.FileProcessor(str(fixture)):
            writer.add(entity)
    report = osm.normalize_osm(pbf, tmp_path / 'context.gpkg', box(127.005, 37.505, 127.035, 37.535),
        box(126.99, 37.49, 127.05, 37.55), lambda _: None, max_output_bytes=16 * osm.MIB,
        memory_limit_bytes=1024 * osm.MIB)
    assert report['relations']['unassembled_relation_ids'] == [20]
    assert report['geographic_completeness_confirmed'] is False
    assert report['layers']['water']['features'] == 0
    assert report['layers']['paths']['features'] == 1
    assert report['layers']['quality']['features'] == 1


def test_native_area_failure_and_unassembled_relation_publish_one_quality_row(osm, tmp_path, monkeypatch):
    """Synthetic factory failure preserves both reasons and independent valid paths."""
    osmium = pytest.importorskip('osmium')
    pytest.importorskip('pyproj')
    source = tmp_path / 'synthetic-area-failure.osm'
    source.write_text('''<osm version="0.6" generator="synthetic-test">
      <node id="1" lat="37.50" lon="127.00"/><node id="2" lat="37.50" lon="127.04"/>
      <node id="3" lat="37.54" lon="127.04"/><node id="4" lat="37.54" lon="127.00"/>
      <way id="10"><nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/></way>
      <way id="11"><nd ref="1"/><nd ref="2"/><tag k="highway" v="footway"/></way>
      <relation id="20"><member type="way" ref="10" role="outer"/>
        <tag k="type" v="multipolygon"/><tag k="natural" v="water"/></relation></osm>''')
    factory = osmium.geom.WKBFactory()
    class SyntheticFailure:
        def create_multipolygon(self, area):
            if not area.from_way() and area.orig_id() == 20:
                raise RuntimeError('synthetic fixture failure')
            return factory.create_multipolygon(area)
    monkeypatch.setattr(osmium.geom, 'WKBFactory', SyntheticFailure)
    output = tmp_path / 'context.gpkg'
    result = osm.normalize_osm(source, output, box(127.005, 37.505, 127.035, 37.535),
        box(126.99, 37.49, 127.05, 37.55), lambda _: None, max_output_bytes=16 * osm.MIB,
        memory_limit_bytes=1024 * osm.MIB)
    assert result['normalized_ready'] is False
    assert result['geographic_completeness_confirmed'] is False
    assert result['layers']['paths']['features'] == 1
    assert result['layers']['quality']['features'] == 1
    with sqlite3.connect(output) as db:
        identifier, value = db.execute('SELECT source_id,evidence_json FROM quality').fetchone()
    assert identifier == 'relation/20'
    assert json.loads(value)['reasons'] == ['Area construction failed: RuntimeError', 'Area relation was not assembled']


def test_native_pedestrian_square_preserves_one_polygon_and_all_source_tags(osm, tmp_path):
    pytest.importorskip('osmium')
    pytest.importorskip('pyproj')
    source = tmp_path / 'synthetic-square.osm'
    source.write_text('''<osm version="0.6" generator="synthetic-test">
      <node id="1" lat="37.50" lon="127.00"/><node id="2" lat="37.50" lon="127.04"/>
      <node id="3" lat="37.54" lon="127.04"/><node id="4" lat="37.54" lon="127.00"/>
      <way id="10"><nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/>
        <tag k="highway" v="pedestrian"/><tag k="area" v="yes"/>
        <tag k="place" v="square"/><tag k="surface" v="asphalt"/>
      </way></osm>''')
    output = tmp_path / 'context.gpkg'
    result = osm.normalize_osm(source, output, box(127.005, 37.505, 127.035, 37.535),
        box(126.99, 37.49, 127.05, 37.55), lambda _: None, max_output_bytes=16 * osm.MIB,
        memory_limit_bytes=1024 * osm.MIB)
    assert result['normalized_ready'] is True
    assert result['layers']['public_spaces']['features'] == 1
    assert result['counts']['duplicate_category_matches_merged'] == 1
    with sqlite3.connect(output) as db:
        identifier, tags, blob = db.execute('SELECT source_id,tags_json,geom FROM public_spaces').fetchone()
    assert identifier == 'way/10'
    assert json.loads(tags) == {'highway': 'pedestrian', 'area': 'yes', 'place': 'square', 'surface': 'asphalt'}
    assert osm.decode_gpkg(blob).geom_type == 'MultiPolygon'


def test_explicit_public_space_grid_preserves_holes_exclusions_and_stable_ids(osm):
    polygon = Polygon([(0, 0), (80, 0), (80, 80), (0, 80)],
        holes=[[(30, 30), (50, 30), (50, 50), (30, 50)]])
    city = box(0, 0, 60, 60)
    blocked = box(19, 19, 21, 21)
    args = ('way/9', polygon, {'highway': 'pedestrian', 'area': 'yes'}, city, blocked.covers)
    points = list(osm.sample_public_space_candidates(*args, spacing_m=20))
    assert len(points) == 14
    assert all(city.covers(point) and polygon.covers(point) and not blocked.covers(point) for _, point, _ in points)
    assert [row[0] for row in points] == [row[0] for row in osm.sample_public_space_candidates(*args, spacing_m=20)]
    assert all(row[2]['sampling_kind'] == 'explicit_pedestrian_area_grid' and row[2]['field_verified'] is False for row in points)
    # Shrinking the administrative selection keeps IDs for surviving grid cells.
    smaller = list(osm.sample_public_space_candidates('way/9', polygon, args[2], box(0, 0, 20, 20), blocked.covers))
    original = {(point.x, point.y): identifier for identifier, point, _ in points}
    assert all(original[(point.x, point.y)] == identifier for identifier, point, _ in smaller)


def test_public_space_requires_explicit_pedestrian_evidence(osm):
    polygon = box(0, 0, 40, 40)
    for tags in ({'leisure': 'park'}, {'leisure': 'garden'}, {'place': 'square'},
                 {'place': 'square', 'foot': 'yes', 'foot:conditional': 'no @ (night)'},
                 {'highway': 'pedestrian', 'area': 'yes', 'access': 'private'}):
        assert not list(osm.sample_public_space_candidates('way/1', polygon, tags, polygon, lambda p: False))
    points = list(osm.sample_public_space_candidates('way/1', polygon,
        {'place': 'square', 'foot': 'yes', 'bridge': 'yes'}, polygon, lambda p: False))
    assert len(points) == 9
    assert all(row[2]['observer_elevation_status'] == 'unsupported_structure' for row in points)


def test_oversized_supported_area_stops_before_sampling(osm):
    calls = []
    polygon = box(0, 0, 200, 200)
    with pytest.raises(osm.OSMResourceError, match='requires 121 grid probes'):
        list(osm.sample_public_space_candidates('way/2', polygon,
            {'highway': 'pedestrian', 'area': 'yes'}, polygon, lambda p: calls.append(p),
            spacing_m=20, max_grid_points=100))
    assert not calls


def test_native_public_area_candidates_exclude_buildings_and_water(osm, tmp_path):
    pytest.importorskip('pyproj')
    ogr = pytest.importorskip('osgeo.ogr')
    from osgeo import osr
    from pyproj import Transformer
    from shapely.ops import transform
    context = tmp_path / 'context.gpkg'
    writer = osm.BoundedGPKG(context, lambda n: None, 16 * osm.MIB)
    for name in ('paths', 'public_spaces', 'water'):
        writer.create_layer(name)
    writer.add('public_spaces', 'way/1', box(200000, 550000, 200040, 550040),
        {'highway': 'pedestrian', 'area': 'yes'}, {})
    writer.add('public_spaces', 'way/2', box(200000, 550000, 200040, 550040), {'leisure': 'park'}, {})
    writer.add('water', 'way/3', box(200019, 550019, 200021, 550021), {'natural': 'water'}, {})
    writer.publish()
    buildings = tmp_path / 'buildings.gpkg'
    ds = ogr.GetDriverByName('GPKG').CreateDataSource(str(buildings))
    srs = osr.SpatialReference(); srs.ImportFromEPSG(5186)
    layer = ds.CreateLayer('buildings', srs, ogr.wkbPolygon, options=['SPATIAL_INDEX=YES'])
    feature = ogr.Feature(layer.GetLayerDefn())
    feature.SetGeometry(ogr.CreateGeometryFromWkb(box(199999, 549999, 200001, 550001).wkb))
    layer.CreateFeature(feature)
    feature = None; layer = None; ds = None
    city = transform(Transformer.from_crs(5186, 4326, always_xy=True).transform,
                     box(199998, 549998, 200042, 550042))
    result = osm.generate_candidates(context, buildings, 'buildings', city,
        tmp_path / 'candidates.gpkg', lambda n: None, spacing_m=20, max_output_bytes=16 * osm.MIB)
    assert result['candidate_count'] == 7
    assert result['counts_by_sampling_kind'] == {'explicit_pedestrian_area_grid': 7}
    assert result['eligible_public_space_features'] == 1
    assert result['recipe']['version'] == 3


def test_native_node_only_filter_preserves_complete_members_and_holes(osm):
    """In-memory synthetic fixture proves filter ordering against native pyosmium."""
    osmium = pytest.importorskip('osmium')
    xml = b'''<osm version="0.6" generator="synthetic-filter-fixture">
      <node id="1" lat="37.50" lon="127.00"/><node id="2" lat="37.50" lon="127.04"/>
      <node id="3" lat="37.54" lon="127.04"/><node id="4" lat="37.54" lon="127.00"/>
      <node id="5" lat="37.51" lon="127.01"/><node id="6" lat="37.51" lon="127.03"/>
      <node id="7" lat="37.53" lon="127.03"/><node id="8" lat="37.53" lon="127.01"/>
      <node id="9" lat="37.55" lon="127.01"><tag k="natural" v="peak"/></node>
      <way id="10"><nd ref="1"/><nd ref="2"/><nd ref="3"/></way>
      <way id="11"><nd ref="3"/><nd ref="4"/><nd ref="1"/></way>
      <way id="12"><nd ref="5"/><nd ref="6"/><nd ref="7"/><nd ref="8"/><nd ref="5"/></way>
      <way id="13"><nd ref="1"/><nd ref="9"/><tag k="highway" v="footway"/></way>
      <relation id="20"><member type="way" ref="10" role="outer"/>
        <member type="way" ref="11" role="outer"/><member type="way" ref="12" role="inner"/>
        <tag k="type" v="multipolygon"/><tag k="natural" v="water"/>
      </relation>
    </osm>'''
    factory = osmium.geom.WKBFactory()

    class Capture(osmium.SimpleHandler):
        def __init__(self):
            super().__init__()
            self.nodes, self.ways, self.relations, self.areas = {}, {}, {}, {}

        def node(self, node):
            self.nodes[node.id] = dict(node.tags)

        def way(self, way):
            assert all(node.location.valid() for node in way.nodes)
            self.ways[way.id] = [(node.ref, node.lon, node.lat) for node in way.nodes]

        def relation(self, relation):
            self.relations[relation.id] = [(member.type, member.ref, member.role) for member in relation.members]

        def area(self, area):
            self.areas[area.orig_id()] = factory.create_multipolygon(area)

    original, filtered = Capture(), Capture()
    original.apply_buffer(xml, 'osm', locations=True, idx='sparse_mem_array')
    filtered.apply_buffer(xml, 'osm', locations=True, idx='sparse_mem_array',
                          filters=[osm.native_tagged_node_filter()])
    assert len(original.nodes) == 9 and filtered.nodes == {9: {'natural': 'peak'}}
    assert original.ways == filtered.ways and len(filtered.ways) == 4
    assert original.relations == filtered.relations and len(filtered.relations) == 1
    assert original.areas == filtered.areas and 20 in filtered.areas
    assert len(osm.from_wkb(filtered.areas[20]).geoms[0].interiors) == 1


def test_inspection_cache_rejects_changed_source_counts_and_fingerprint(osm, tmp_path):
    pytest.importorskip('osmium')
    source = tmp_path / 'synthetic.osm'
    source.write_text('<osm version="0.6"><node id="1" lon="127" lat="37"/></osm>')
    report = osm.inspect_osm(source)
    record = osm.inspection_cache_record(source, report)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    args = dict(max_nodes=50_000_000, max_relations=150_000, max_ways=5_000_000)
    assert osm._validated_inspection_record(record, digest, **args)['inspection_cache_reused']
    with pytest.raises(osm.OSMResourceError, match='count caps'):
        osm._validated_inspection_record(record, digest, **{**args, 'max_nodes': 0})
    with pytest.raises(osm.OSMValidationError, match='mismatch'):
        osm._validated_inspection_record(record, 'changed-object', **args)
    record['inspection']['counts']['nodes'] = 2
    with pytest.raises(osm.OSMValidationError, match='fingerprint mismatch'):
        osm._validated_inspection_record(record, digest, **args)


def test_normalization_reuses_verified_inspection_without_scanning(osm, tmp_path, monkeypatch):
    pytest.importorskip('osmium')
    source = tmp_path / 'synthetic.osm'
    source.write_text('<osm version="0.6"><node id="1" lon="127" lat="37"/></osm>')
    report = osm.inspect_osm(source)
    cache = tmp_path / 'inspection.json'
    cache.write_text(json.dumps(osm.inspection_cache_record(source, report)))
    def refuse_scan(*args, **kwargs):
        raise AssertionError('A validated cache must skip the source scan')
    monkeypatch.setattr(osm, 'inspect_osm', refuse_scan)
    def capture_worker(*args, **kwargs):
        return kwargs['preflight_report']
    monkeypatch.setattr(osm, '_normalize_osm_once', capture_worker)
    monkeypatch.setattr(osm, '_resume_product', lambda output, recipe, sources, check, worker, **kwargs: worker(None))
    result = osm.normalize_osm(source, tmp_path / 'context.gpkg', box(126, 36, 128, 38),
        box(125, 35, 129, 39), lambda _: None, inspection_cache=cache)
    assert result['inspection_cache_reused'] and result['counts']['nodes'] == 1
    source.write_text('<osm version="0.6"><node id="2" lon="127" lat="37"/></osm>')
    with pytest.raises(osm.OSMValidationError, match='mismatch'):
        osm.normalize_osm(source, tmp_path / 'context.gpkg', box(126, 36, 128, 38),
            box(125, 35, 129, 39), lambda _: None, inspection_cache=cache)
