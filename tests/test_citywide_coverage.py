"""Synthetic offline coverage tests. No real source/network acquisition."""
from contextlib import contextmanager
from pathlib import Path
import json
import pytest
from shapely.geometry import box, Point

from scripts.data.coverage import source_domain_report, _point_district_counts, coverage_report
from scripts.data.osm_pipeline import OSMValidationError, BoundedGPKG, MIB
from seoul_visibility.acquisition_safety import AcquisitionError, sha256


def identity(x, y, z=None):
    return (x, y) if z is None else (x, y, z)


def test_envelope_gap_is_not_building_completeness():
    city, support, halo = box(.5, .5, 1.5, 1.5), box(0, 0, 2, 2), box(-1, -1, 3, 3)
    report, exterior, domain = source_domain_report(city, support, halo, True,
        {'selected_groups': [{'bounds': [0, 0, 1, 2]}]}, identity)
    assert report['buildings']['row_group_envelope_coverage_fraction'] == .5
    assert report['buildings']['row_group_envelope_gap_area_m2'] == 2
    assert report['buildings']['real_world_detection_complete'] is None
    assert report['terrain']['support_area_complete'] is False
    assert report['terrain']['interpolation_coverage_validated'] is False
    assert exterior.area == 15 and domain.area == 2


def test_missing_source_stays_unknown_and_crs_is_checked():
    report, _, domain = source_domain_report(box(0, 0, 1, 1), box(0, 0, 2, 2), box(0, 0, 3, 3), False, None, identity)
    assert report['terrain']['potential_seoul_source_domain_area_m2'] is None
    assert report['buildings']['row_group_envelope_gap_area_m2'] is None and domain is None
    with pytest.raises(OSMValidationError, match='unexplained CRS'):
        source_domain_report(box(0, 0, 1, 1), box(0, 0, 2, 2), box(0, 0, 3, 3), False,
                             {'selected_groups': [{'bounds': [200000, 500000, 210000, 510000]}]}, identity)


def test_shared_district_boundary_candidates_count_once(tmp_path):
    pytest.importorskip('pyproj')
    output = tmp_path / 'candidates.gpkg'
    writer = BoundedGPKG(output, lambda n: None, 8 * MIB)
    writer.create_layer('candidates')
    writer.add('candidates', 'candidate1', Point(1, 1), {}, {})
    writer.publish()
    districts = [{'source_id': 'relation/1', 'geometry': box(0, 0, 1, 2)},
                 {'source_id': 'relation/2', 'geometry': box(1, 0, 2, 2)}]
    result = _point_district_counts(output, 'candidates', districts, box(0, 0, 2, 2))
    assert result['total_inside_seoul'] == 1
    assert result['by_district'] == {'relation/1': 1}
    assert result['unassigned'] == 0


class FakeBudget:
    """Fixture records reservations without exercising real disk exhaustion."""
    _reservation = None
    def __init__(self): self.reservations = []
    def safe_path(self, path): return Path(path)
    def check(self, *args, **kwargs): return {}
    @contextmanager
    def reserve(self, incremental_bytes, temporary_bytes=0, label=''):
        self.reservations.append((incremental_bytes, temporary_bytes))
        self._reservation = self
        try: yield self
        finally: self._reservation = None
    def check_write(self, n, path): assert n >= 0
    def observe(self): return {}


def test_coverage_keeps_missing_sources_and_is_restartable(tmp_path):
    pytest.importorskip('pyproj')
    pytest.importorskip('osgeo.ogr')
    norm = tmp_path / 'normalized' / 'fixture'
    budget = FakeBudget()
    geometry = {'recommendation': box(127.0, 37.5, 127.01, 37.51),
                'obstruction_support': box(126.99, 37.49, 127.02, 37.52),
                'terrain_interpolation_support': box(126.98, 37.48, 127.03, 37.53)}
    result = coverage_report(norm, geometry, budget, {'maximum_sight_distance_m': 10000})
    assert result['support_area_terrain_complete'] is False
    assert result['buildings']['normalized_input_present'] is False
    assert result['candidate_counts']['total_inside_seoul'] is None
    assert result['district_count_check_passed'] is False
    assert result['grid_cells'] > 0 and (norm / 'coverage.gpkg').exists()
    assert budget.reservations == [(48 * MIB, 40 * MIB)]
    resumed = coverage_report(norm, geometry, budget, {'maximum_sight_distance_m': 10000})
    assert resumed['sha256'] == result['sha256']
    assert len(budget.reservations) == 1
    changed = coverage_report(norm, geometry, budget, {'maximum_sight_distance_m': 9000})
    assert changed['recipe']['sight_distance_m'] == 9000
    ledger = json.loads((norm / 'coverage.preservation.json').read_text())
    assert ledger['versions'][0]['status'] == 'preserved'
    assert ledger['versions'][0]['deletion_performed'] is False
    old_paths = {row['original_path']: norm / row['preserved_path']
                 for row in ledger['versions'][0]['artifacts']}
    assert sha256(old_paths['coverage.gpkg']) == result['sha256']
    assert json.loads(old_paths['coverage.json'].read_text())['recipe'] == result['recipe']


@pytest.fixture
def coverage_fixture(tmp_path):
    pytest.importorskip('pyproj')
    pytest.importorskip('osgeo.ogr')
    norm = tmp_path / 'normalized' / 'fixture'
    geometry = {'recommendation': box(127.0, 37.5, 127.01, 37.51),
                'obstruction_support': box(126.99, 37.49, 127.02, 37.52),
                'terrain_interpolation_support': box(126.98, 37.48, 127.03, 37.53)}
    return norm, geometry, FakeBudget(), {'maximum_sight_distance_m': 10000}


def _add_synthetic_candidate(norm):
    from pyproj import Transformer
    location = Transformer.from_crs(4326, 5186, always_xy=True).transform(127.005, 37.505)
    writer = BoundedGPKG(norm / 'candidates.gpkg', lambda n: None, 8 * MIB)
    writer.create_layer('candidates')
    writer.add('candidates', 'synthetic/path/1/point/1', Point(*location), {}, {'fixture': True})
    writer.publish()


def test_new_input_rebuilds_coverage_and_retains_valid_prior_version(coverage_fixture):
    norm, geometry, budget, config = coverage_fixture
    before = coverage_report(norm, geometry, budget, config)
    _add_synthetic_candidate(norm)
    after = coverage_report(norm, geometry, budget, config)
    assert before['candidate_counts']['total_inside_seoul'] is None
    assert after['candidate_counts']['total_inside_seoul'] == 1
    assert after['recipe']['inputs']['candidates'] == sha256(norm / 'candidates.gpkg')
    assert after['recipe'] != before['recipe']
    ledger = json.loads((norm / 'coverage.preservation.json').read_text())
    assert len(ledger['versions']) == 1
    old = ledger['versions'][0]
    assert old['status'] == 'preserved' and old['prior_output_sha256'] == before['sha256']
    assert old['deletion_performed'] is False
    for item in old['artifacts']:
        assert sha256(norm / item['preserved_path']) == item['sha256']
    count = len(budget.reservations)
    assert coverage_report(norm, geometry, budget, config)['sha256'] == after['sha256']
    assert len(budget.reservations) == count


@pytest.mark.parametrize('after_rename', [False, True])
def test_coverage_recovers_durable_publication_receipt_without_rebuilding(
        coverage_fixture, monkeypatch, after_rename):
    norm, geometry, budget, config = coverage_fixture
    original_publish = BoundedGPKG.publish

    def interrupted_publish(writer, *args, **kwargs):
        if after_rename:
            original_publish(writer, *args, **kwargs)
            raise InterruptedError('synthetic interruption after atomic GPKG rename')
        callback = writer.publication_callback
        def interrupted_callback(product):
            callback(product)
            raise InterruptedError('synthetic interruption after durable receipt')
        writer.publication_callback = interrupted_callback
        return original_publish(writer, *args, **kwargs)

    monkeypatch.setattr(BoundedGPKG, 'publish', interrupted_publish)
    with pytest.raises(InterruptedError, match='synthetic interruption'):
        coverage_report(norm, geometry, budget, config)
    pending = json.loads((norm / 'coverage.json.part').read_text())
    preserved_data = norm / ('coverage.gpkg' if after_rename else 'coverage.gpkg.part')
    assert sha256(preserved_data) == pending['sha256']
    monkeypatch.setattr(BoundedGPKG, 'publish', lambda *a, **k: pytest.fail('Valid pending coverage must be reused'))
    report = coverage_report(norm, geometry, budget, config)
    assert report['sha256'] == pending['sha256']
    assert (norm / 'coverage.gpkg').is_file() and (norm / 'coverage.json').is_file()
    assert not (norm / 'coverage.json.part').exists()


def test_interruption_between_preserving_output_and_receipt_is_reconciled(coverage_fixture, monkeypatch):
    norm, geometry, budget, config = coverage_fixture
    before = coverage_report(norm, geometry, budget, config)
    _add_synthetic_candidate(norm)
    original_replace = Path.replace
    def interrupted_replace(path, destination):
        if path == norm / 'coverage.json' and Path(destination).parent.name.endswith('.retained'):
            raise InterruptedError('synthetic receipt move interruption')
        return original_replace(path, destination)
    monkeypatch.setattr(Path, 'replace', interrupted_replace)
    with pytest.raises(InterruptedError, match='receipt move'):
        coverage_report(norm, geometry, budget, config)
    pending_ledger = json.loads((norm / 'coverage.preservation.json').read_text())
    assert pending_ledger['versions'][0]['status'] == 'preservation_planned'
    assert not (norm / 'coverage.gpkg').exists() and (norm / 'coverage.json').exists()
    monkeypatch.setattr(Path, 'replace', original_replace)
    after = coverage_report(norm, geometry, budget, config)
    ledger = json.loads((norm / 'coverage.preservation.json').read_text())
    assert ledger['versions'][0]['status'] == 'preserved'
    old_paths = {row['original_path']: norm / row['preserved_path']
                 for row in ledger['versions'][0]['artifacts']}
    assert sha256(old_paths['coverage.gpkg']) == before['sha256']
    assert json.loads(old_paths['coverage.json'].read_text())['sha256'] == before['sha256']
    assert after['candidate_counts']['total_inside_seoul'] == 1


def test_changed_input_never_overwrites_corrupt_previous_coverage(coverage_fixture):
    norm, geometry, budget, config = coverage_fixture
    coverage_report(norm, geometry, budget, config)
    output = norm / 'coverage.gpkg'
    with output.open('ab') as stream:
        stream.write(b'synthetic corruption')
    corrupt_hash = sha256(output)
    _add_synthetic_candidate(norm)
    with pytest.raises(OSMValidationError, match='fingerprint'):
        coverage_report(norm, geometry, budget, config)
    assert sha256(output) == corrupt_hash
    assert not (norm / 'coverage.preservation.json').exists()


def test_successor_reservation_refusal_preserves_current_files(coverage_fixture):
    norm, geometry, budget, config = coverage_fixture
    before = coverage_report(norm, geometry, budget, config)
    _add_synthetic_candidate(norm)
    class RefusingBudget(FakeBudget):
        @contextmanager
        def reserve(self, *args, **kwargs):
            raise AcquisitionError('storage_blocked', 'synthetic successor peak cannot fit')
            yield
    with pytest.raises(AcquisitionError, match='successor peak'):
        coverage_report(norm, geometry, RefusingBudget(), config)
    assert sha256(norm / 'coverage.gpkg') == before['sha256']
    assert json.loads((norm / 'coverage.json').read_text())['sha256'] == before['sha256']
    assert not (norm / 'coverage.preservation.json').exists()
