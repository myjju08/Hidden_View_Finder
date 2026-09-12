"""Synthetic terrain fixtures; archive pins are monkeypatched only inside tests."""
from contextlib import contextmanager
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
import hashlib
import json

import pytest
from shapely.geometry import box

from scripts.data import terrain_pipeline as terrain
from scripts.data.osm_pipeline import OSMValidationError


class FixtureBudget:
    def __init__(self):
        self._reservation = None
        self.writes = []
    def safe_path(self, path): return Path(path).absolute()
    def check(self, *args, **kwargs): return {}
    def check_write(self, size, path):
        assert size >= 0
        self.writes.append((size, str(path)))
    def observe(self): return {}
    @contextmanager
    def reserve(self, *args, **kwargs):
        assert self._reservation is None
        self._reservation = self
        try: yield self
        finally: self._reservation = None


def pin_fixture_archive(monkeypatch, raw):
    """Source bytes are synthetic and are never confused with production pins."""
    archive = raw / 'synthetic-terrain.zip'
    files = [path for path in (raw / 'source').rglob('*') if path.is_file()]
    with ZipFile(archive, 'w', compression=ZIP_DEFLATED) as zipped:
        for path in files:
            zipped.write(path, str(path.relative_to(raw / 'source')))
    monkeypatch.setattr(terrain.source_contract, 'ARCHIVE_NAME', archive.name)
    monkeypatch.setattr(terrain.source_contract, 'ARCHIVE_BYTES', archive.stat().st_size)
    monkeypatch.setattr(terrain.source_contract, 'ARCHIVE_SHA256', terrain.sha256(archive))
    monkeypatch.setattr(terrain.source_contract, 'EXPANDED_BYTES', sum(path.stat().st_size for path in files))
    return archive


@pytest.fixture
def synthetic_source(tmp_path, monkeypatch):
    raw = tmp_path / 'raw'
    for relative, *_ in terrain.TERRAIN_LAYERS:
        for suffix in ('.shp', '.shx', '.dbf', '.prj'):
            path = raw / 'source' / Path(relative).with_suffix(suffix)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f'synthetic-not-gis:{path.name}'.encode())
    archive = pin_fixture_archive(monkeypatch, raw)
    return raw, archive


def test_source_validation_checks_retained_archive_and_every_sidecar(synthetic_source):
    raw, archive = synthetic_source
    inspected = terrain._verified_terrain_sources(raw, FixtureBudget())
    assert len(inspected['members_sha256']) == 8
    sidecar = next((raw / 'source').rglob('*.dbf'))
    original = sidecar.read_bytes()
    sidecar.write_bytes(b'X' * len(original))
    with pytest.raises(ValueError, match='differs from retained pinned archive'):
        terrain._verified_terrain_sources(raw, FixtureBudget())
    assert archive.exists() and sidecar.read_bytes() != original


def test_archive_pin_mismatch_is_not_accepted(synthetic_source):
    raw, archive = synthetic_source
    archive.write_bytes(b'changed source')
    with pytest.raises(ValueError, match='unchanged pinned size/SHA256'):
        terrain._verified_terrain_sources(raw, FixtureBudget())
    assert archive.read_bytes() == b'changed source'


def fake_worker(raw, output, partial, key, reservation, callback):
    partial.write_bytes(b'synthetic-normalized-terrain')
    report = {'valid': True, 'sha256': terrain.sha256(partial), 'bytes': partial.stat().st_size,
              'vertical_conversion': 'none', 'coverage_supported_beyond_seoul': False}
    callback(report)
    partial.replace(output)
    return report


def test_terrain_reuse_pins_support_geometry_and_rejects_corruption(synthetic_source, tmp_path, monkeypatch):
    raw, archive = synthetic_source
    monkeypatch.setattr(terrain, '_normalize_terrain_once', fake_worker)
    monkeypatch.setattr(terrain, 'validate_terrain', lambda path: {'valid': True})
    support = box(126.9, 37.4, 127.2, 37.7)
    output = tmp_path / 'terrain.gpkg'
    first = terrain.normalize_terrain(raw, output, support, FixtureBudget())
    assert first['recipe']['support_geometry_sha256'] == hashlib.sha256(support.wkb).hexdigest()
    second = terrain.normalize_terrain(raw, output, support, FixtureBudget())
    assert second['reused'] and second['sha256'] == first['sha256']
    with pytest.raises(OSMValidationError, match='source/settings changed'):
        terrain.normalize_terrain(raw, output, box(126.8, 37.4, 127.3, 37.7), FixtureBudget())
    output.write_bytes(b'truncated')
    with pytest.raises(OSMValidationError, match='checksum mismatch'):
        terrain.normalize_terrain(raw, output, support, FixtureBudget())


@pytest.mark.parametrize('phase', ['receipt', 'final'])
def test_terrain_recovers_atomic_publication_receipt(synthetic_source, tmp_path, monkeypatch, phase):
    raw, archive = synthetic_source
    monkeypatch.setattr(terrain, 'validate_terrain', lambda path: {'valid': True})
    def interrupt(raw, output, partial, key, reservation, callback):
        partial.write_bytes(b'synthetic-normalized-terrain')
        callback({'sha256': terrain.sha256(partial), 'valid': True})
        if phase == 'final': partial.replace(output)
        raise InterruptedError('fixture interruption at publication boundary')
    monkeypatch.setattr(terrain, '_normalize_terrain_once', interrupt)
    support = box(126.9, 37.4, 127.2, 37.7)
    output = tmp_path / 'terrain.gpkg'
    with pytest.raises(InterruptedError):
        terrain.normalize_terrain(raw, output, support, FixtureBudget())
    monkeypatch.setattr(terrain, '_normalize_terrain_once', lambda *args: pytest.fail('Receipt must avoid replay'))
    report = terrain.normalize_terrain(raw, output, support, FixtureBudget())
    assert report['reused'] and output.with_suffix('.source.json').exists()
    assert not output.with_suffix('.source.json.part').exists()


def test_terrain_owned_partial_is_retained_with_original_archive(synthetic_source, tmp_path, monkeypatch):
    raw, archive = synthetic_source
    monkeypatch.setattr(terrain, 'validate_terrain', lambda path: {'valid': True})
    def interrupt(raw, output, partial, key, reservation, callback):
        partial.write_bytes(b'synthetic-interrupted-terrain')
        raise InterruptedError('fixture interruption during conversion')
    monkeypatch.setattr(terrain, '_normalize_terrain_once', interrupt)
    output = tmp_path / 'terrain.gpkg'
    support = box(126.9, 37.4, 127.2, 37.7)
    with pytest.raises(InterruptedError):
        terrain.normalize_terrain(raw, output, support, FixtureBudget())
    monkeypatch.setattr(terrain, '_normalize_terrain_once', fake_worker)
    result = terrain.normalize_terrain(raw, output, support, FixtureBudget())
    attempts = json.loads(output.with_suffix('.preservation.json').read_text())['attempts']
    assert attempts[-1]['status'] == 'preserved'
    assert attempts[-1]['deletion_performed'] is False
    old_partial = Path(attempts[-1]['artifacts'][0]['preserved_path'])
    assert old_partial.read_bytes() == b'synthetic-interrupted-terrain'
    owner = json.loads(Path(attempts[-1]['ownership_copy']).read_text())
    assert owner['recovery_source_sha256'] == terrain.sha256(archive)
    assert archive.exists() and result['valid']


def test_native_terrain_normalization_with_tiny_synthetic_shapefiles(tmp_path, monkeypatch):
    ogr = pytest.importorskip('osgeo.ogr')
    from osgeo import osr
    raw = tmp_path / 'raw'
    specifications = (
        ('contours/lines.shp', 'contours', 'CONT', 1, 'MultiLineString'),
        ('spots/points.shp', 'spots', 'NUME', 1, 'Point'))
    monkeypatch.setattr(terrain, 'TERRAIN_LAYERS', specifications)
    for relative, name, field, _, geometry_name in specifications:
        path = raw / 'source' / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        ds = ogr.GetDriverByName('ESRI Shapefile').CreateDataSource(str(path))
        srs = osr.SpatialReference(); srs.ImportFromEPSG(5174)
        layer = ds.CreateLayer(path.stem, srs, ogr.wkbLineString if name == 'contours' else ogr.wkbPoint)
        layer.CreateField(ogr.FieldDefn('UFID', ogr.OFTString))
        layer.CreateField(ogr.FieldDefn(field, ogr.OFTReal))
        feature = ogr.Feature(layer.GetLayerDefn())
        feature.SetField('UFID', 'synthetic-' + name)
        feature.SetField(field, 100.0 if name == 'contours' else 123.4)
        feature.SetGeometry(ogr.CreateGeometryFromWkt('LINESTRING(200000 450000,200100 450100)' if name == 'contours' else 'POINT(200050 450050)'))
        layer.CreateFeature(feature)
        feature = None; layer = None; ds = None
    pin_fixture_archive(monkeypatch, raw)
    output = tmp_path / 'terrain.gpkg'
    report = terrain.normalize_terrain(raw, output, box(126.9, 37.4, 127.2, 37.7), FixtureBudget())
    assert report['validation']['valid'] and report['vertical_conversion'] == 'none'
    assert report['validation']['layers']['spots']['features'] == 1
    assert report['recipe']['archive_sha256'] == terrain.source_contract.ARCHIVE_SHA256
    assert terrain.normalize_terrain(raw, output, box(126.9, 37.4, 127.2, 37.7), FixtureBudget())['reused']
