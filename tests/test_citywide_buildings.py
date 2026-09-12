"""Synthetic, offline fixtures for GBA range identity and resumable subsets."""
from __future__ import annotations

import io
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import acquire_buildings as old
from scripts.data import buildings_pipeline as pipeline
from seoul_visibility.acquisition_safety import Budget, AcquisitionError


class Response:
    def __init__(self, body, headers, *, status=206, url=old.URL):
        self.data = io.BytesIO(body)
        self.headers = headers
        self.status = status
        self.url = url
    def geturl(self): return self.url
    def read(self, n): return self.data.read(n)
    def __enter__(self): return self
    def __exit__(self, *args): return False


def range_response(body=b'\x00\x00\x00\x00PAR1', **changes):
    headers = {'Content-Range': 'bytes 8-15/16', 'Content-Length': '8',
               'ETag': '"fixture-v1"', 'x-amz-version-id': 'v1'}
    headers.update(changes)
    return Response(body, headers)


@pytest.mark.parametrize('case', ['http200', 'wrong_range', 'weak_etag', 'redirect', 'wrong_length'])
def test_strict_range_rejection(monkeypatch, case):
    response = range_response()
    if case == 'http200': response.status = 200
    if case == 'wrong_range': response.headers['Content-Range'] = 'bytes 0-7/16'
    if case == 'weak_etag': response.headers['ETag'] = 'W/"fixture-v1"'
    if case == 'redirect': response.url = 'https://unapproved.example/data'
    if case == 'wrong_length': response.headers['Content-Length'] = '16'
    monkeypatch.setattr(old.urllib.request, 'urlopen', lambda *a, **k: response)
    with pytest.raises(RuntimeError): old.Ranges(old.URL, old.MiB, retries=0)
    assert response.data.tell() == 0


def test_missing_content_length_is_bounded(monkeypatch):
    response = range_response()
    del response.headers['Content-Length']
    monkeypatch.setattr(old.urllib.request, 'urlopen', lambda *a, **k: response)
    remote = old.Ranges(old.URL, old.MiB, retries=0)
    assert remote.bytes_read == 8
    assert remote.size == 16


def test_identity_change_between_ranges(monkeypatch):
    responses = iter([range_response(), Response(b'abcdefgh', {
        'Content-Range': 'bytes 0-7/16',
        'ETag': '"different"', 'x-amz-version-id': 'v2'})])
    monkeypatch.setattr(old.urllib.request, 'urlopen', lambda *a, **k: next(responses))
    remote = old.Ranges(old.URL, old.MiB, retries=0)
    with pytest.raises(RuntimeError, match='identity mismatch'): remote.read(8)
    assert remote.bytes_read == 8


def test_resume_rejects_changed_identity(monkeypatch):
    monkeypatch.setattr(old.urllib.request, 'urlopen', lambda *a, **k: range_response())
    with pytest.raises(RuntimeError, match='identity mismatch'):
        old.Ranges(old.URL, old.MiB, retries=0, expected_identity={'etag': '"old"'})


def test_truncated_range_not_accepted(monkeypatch):
    monkeypatch.setattr(old.urllib.request, 'urlopen', lambda *a, **k: range_response(body=b'PAR1'))
    with pytest.raises(AcquisitionError, match='Incomplete') as caught:
        old.Ranges(old.URL, old.MiB, retries=0)
    assert caught.value.status == 'corrupt_content'


@pytest.mark.parametrize('code,status', [(401, 'authentication_blocked'), (403, 'authentication_blocked'),
    (404, 'missing_source'), (410, 'missing_source'), (429, 'rate_limited'),
    (412, 'source_changed'), (416, 'range_protocol_error'), (400, 'range_protocol_error'),
    (451, 'permission_blocked'), (503, 'network_error')])
def test_range_http_categories_survive_orchestration(monkeypatch, code, status):
    calls = []
    def request(*args, **kwargs):
        calls.append(True)
        raise old.urllib.error.HTTPError(old.URL, code, 'Synthetic response', {}, None)
    monkeypatch.setattr(old.urllib.request, 'urlopen', request)
    with pytest.raises(AcquisitionError, match=f'HTTP failure {code}') as caught:
        old.Ranges(old.URL, old.MiB, retries=0)
    assert caught.value.status == status
    assert len(calls) == 1


def test_range_network_retry_exhaustion_is_bounded_and_redacted(monkeypatch):
    calls, sleeps = [], []
    def request(*args, **kwargs):
        calls.append(True)
        raise old.urllib.error.URLError('token=synthetic-secret')
    monkeypatch.setattr(old.urllib.request, 'urlopen', request)
    monkeypatch.setattr(old.time, 'sleep', sleeps.append)
    with pytest.raises(AcquisitionError) as caught:
        old.Ranges(old.URL, old.MiB, retries=2)
    assert caught.value.status == 'network_error'
    assert 'synthetic-secret' not in str(caught.value)
    assert len(calls) == 3 and len(sleeps) == 2


def test_metadata_plan_preserves_range_failure_category(monkeypatch):
    calls = []
    def request(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1: return range_response()
        raise old.urllib.error.HTTPError(old.URL, 403, 'Synthetic forbidden footer', {}, None)
    monkeypatch.setattr(old.urllib.request, 'urlopen', request)
    # The exception must survive the real PyArrow metadata-reader boundary so
    # orchestration reports an access barrier, not generic invalid geography.
    with pytest.raises(AcquisitionError) as caught:
        pipeline.plan_buildings([126.8, 37.4, 127.1, 37.6])
    assert caught.value.status == 'authentication_blocked'
    assert len(calls) == 2


@pytest.mark.parametrize('header,status', [('3600', 'rate_limited'), ('nan', 'range_protocol_error'),
    ('not-a-date', 'range_protocol_error')])
def test_range_retry_after_stops_without_unbounded_wait(monkeypatch, header, status):
    def request(*args, **kwargs):
        raise old.urllib.error.HTTPError(old.URL, 429, 'Synthetic rate limit', {'Retry-After': header}, None)
    monkeypatch.setattr(old.urllib.request, 'urlopen', request)
    monkeypatch.setattr(old.time, 'sleep', lambda _: pytest.fail('Unexpected unbounded pause'))
    with pytest.raises(AcquisitionError) as caught:
        old.Ranges(old.URL, old.MiB, retries=1)
    assert caught.value.status == status


def test_query_secrets_and_unbounded_retries_rejected_before_network(monkeypatch):
    monkeypatch.setattr(old.urllib.request, 'urlopen', lambda *a, **k: pytest.fail('Unexpected network'))
    with pytest.raises(ValueError, match='query secrets'):
        old.Ranges(old.URL + '?token=synthetic-secret', old.MiB)
    with pytest.raises(ValueError, match='zero and five'):
        old.Ranges(old.URL, old.MiB, retries=6)


def test_prefetch_serves_exact_offsets_without_repeated_transfer(monkeypatch):
    responses = iter([range_response(), Response(b'abcdefgh', {
        'Content-Range': 'bytes 0-7/16', 'Content-Length': '8',
        'ETag': '"fixture-v1"', 'x-amz-version-id': 'v1'})])
    requests = []
    def opening(request, **kwargs):
        requests.append(request)
        return next(responses)
    monkeypatch.setattr(old.urllib.request, 'urlopen', opening)
    remote = old.Ranges(old.URL, old.MiB, retries=0)
    record = remote.prefetch(0, 8)
    assert remote.tell() == 0
    assert record['bytes'] == 8
    assert remote.bytes_read == 16
    remote.cap = 16  # Already-acquired cached bytes spend no further transfer.
    assert remote.read(3) == b'abc'
    remote.seek(5)
    assert remote.read(3) == b'fgh'
    assert remote.bytes_read == 16 and len(requests) == 2
    assert remote.reads[1] == [0, 8, record['sha256']]
    remote.clearcache()
    remote.seek(0)
    with pytest.raises(RuntimeError, match='transfer cap'):
        remote.read(1)
    assert len(requests) == 2


@pytest.mark.parametrize('case', ['http200', 'wrong_range', 'changed_identity'])
def test_prefetch_keeps_strict_http_and_identity_checks(monkeypatch, case):
    second = Response(b'abcdefgh', {'Content-Range': 'bytes 0-7/16',
        'ETag': '"fixture-v1"', 'x-amz-version-id': 'v1'})
    if case == 'http200': second.status = 200
    if case == 'wrong_range': second.headers['Content-Range'] = 'bytes 1-8/16'
    if case == 'changed_identity': second.headers['ETag'] = '"changed"'
    responses = iter([range_response(), second])
    monkeypatch.setattr(old.urllib.request, 'urlopen', lambda *a, **k: next(responses))
    remote = old.Ranges(old.URL, old.MiB, retries=0)
    with pytest.raises(RuntimeError): remote.prefetch(0, 8)
    assert second.data.tell() == 0
    assert remote._cache_data is None


def test_prefetch_size_rejected_before_network(monkeypatch):
    monkeypatch.setattr(old.urllib.request, 'urlopen', lambda *a, **k: range_response())
    remote = old.Ranges(old.URL, old.MiB, retries=0)
    monkeypatch.setattr(old.urllib.request, 'urlopen', lambda *a, **k: pytest.fail('Unexpected network'))
    for start, size in [(0, 0), (-1, 8), (0, 8 * old.MiB + 1), (8, 9)]:
        with pytest.raises(ValueError, match='at most 8 MiB'): remote.prefetch(start, size)


@pytest.mark.parametrize('case', ['gap', 'too_large', 'overlap', 'footer'])
def test_physical_group_span_refuses_unsafe_metadata(case):
    from types import SimpleNamespace
    columns = [SimpleNamespace(data_page_offset=4, dictionary_page_offset=None, total_compressed_size=100),
               SimpleNamespace(data_page_offset=104, dictionary_page_offset=None, total_compressed_size=100)]
    end = 16 * old.MiB
    if case == 'gap': columns[1].data_page_offset = 100000
    if case == 'too_large': columns[1].total_compressed_size = 9 * old.MiB
    if case == 'overlap': columns[1].data_page_offset = 50
    if case == 'footer': end = 200
    row = SimpleNamespace(num_columns=2, column=lambda index: columns[index])
    with pytest.raises(RuntimeError): pipeline._physical_group_range(row, end)


@pytest.mark.parametrize('case', ['explicit_null_crs', 'missing_height', 'missing_geo_metadata'])
def test_missing_or_unexplained_schema_is_rejected(monkeypatch, case):
    import pyarrow.parquet as pq
    from types import SimpleNamespace

    spec = {'encoding': 'WKB', 'geometry_types': ['Polygon'], 'bbox': [126.9, 37.5, 126.91, 37.51]}
    if case == 'explicit_null_crs': spec['crs'] = None
    geo = {'version': '1.1.0', 'columns': {'geometry': spec}}
    attrs = {} if case == 'missing_geo_metadata' else {b'geo': json.dumps(geo).encode()}
    columns = ['geometry', 'bbox', 'source', 'id', 'var']
    if case != 'missing_height': columns.append('height')
    fake = SimpleNamespace(metadata=SimpleNamespace(metadata=attrs), schema_arrow=SimpleNamespace(names=columns))
    monkeypatch.setattr(pq, 'ParquetFile', lambda *a, **k: fake)
    with pytest.raises(RuntimeError, match='schema_changed'):
        pipeline._inspect(None, [126.8, 37.4, 127.1, 37.6])


@pytest.fixture
def synthetic_remote(monkeypatch):
    import pyarrow as pa
    import pyarrow.parquet as pq
    from shapely.geometry import box

    rows = []
    for i in range(4):
        geometry = box(126.9 + i * .001, 37.5, 126.9005 + i * .001, 37.5005)
        rows.append({'geometry': geometry.wkb, 'bbox': dict(zip(['xmin', 'ymin', 'xmax', 'ymax'], geometry.bounds)),
                     'id': f'fixture-{i}', 'source': 'synthetic', 'height': None if i == 0 else 10., 'var': 2.})
    metadata = {'version': '1.1.0', 'primary_column': 'geometry', 'columns': {'geometry': {
        'encoding': 'WKB', 'geometry_types': ['Polygon'], 'bbox': [126.9, 37.5, 126.9035, 37.5005]}}}
    table = pa.Table.from_pylist(rows).replace_schema_metadata({b'geo': json.dumps(metadata).encode()})
    buffer = io.BytesIO()
    pq.write_table(table, buffer, row_group_size=2)
    payload = buffer.getvalue()
    calls = []
    class FixtureRanges(io.BytesIO):
        def __init__(self, url, cap, expected_identity=None):
            super().__init__(payload)
            self.cap = cap
            self.bytes_read = 0
            self.reads = []
            self.tail = payload[-8:]
            self.cached = None
            self.prefetch_calls = []
            self.identity = {'url': url, 'size': len(payload), 'etag': '"fixture"', 'version': '1', 'last_modified': None}
            if expected_identity and expected_identity != self.identity:
                raise RuntimeError('identity mismatch')
            calls.append(self)
        def read(self, n=-1):
            start = self.tell()
            if self.cached and self.cached[0] <= start and start + n <= self.cached[0] + self.cached[1]:
                data = payload[start:start + n]
                self.seek(start + len(data))
                return data
            data = super().read(n)
            self.bytes_read += len(data)
            self.reads.append([start, len(data), 'fixture-fingerprint'])
            return data
        def prefetch(self, start, n):
            import hashlib
            self.cached = (start, n)
            self.prefetch_calls.append([start, n])
            self.bytes_read += n
            digest = hashlib.sha256(payload[start:start + n]).hexdigest()
            self.reads.append([start, n, digest])
            return {'start': start, 'bytes': n, 'sha256': digest}
        def clearcache(self): self.cached = None
    monkeypatch.setattr(pipeline, 'Ranges', FixtureRanges)
    monkeypatch.setattr(old.urllib.request, 'urlopen', lambda *a, **k: pytest.fail('Synthetic test attempted network'))
    return calls


def test_metadata_plan_no_feature_reads(synthetic_remote, monkeypatch):
    import pyarrow.parquet as pq
    monkeypatch.setattr(pq.ParquetFile, 'iter_batches', lambda *a, **k: pytest.fail('Plan decoded features'))
    plan = pipeline.plan_buildings([126.8, 37.4, 127.1, 37.6])
    assert plan['no_bulk_downloads']
    assert len(plan['selected_groups']) == 2
    assert plan['source_crs'] == 'OGC:CRS84'
    assert plan['planned_transfer_bytes'] == sum(g['range_bytes'] for g in plan['selected_groups'])
    assert plan['decode_batch_rows'] == 1024


def test_checkpoint_resume_and_corrupt_final(synthetic_remote, tmp_path):
    budget = Budget(tmp_path, stage_root=tmp_path)
    bbox = [126.8, 37.4, 127.1, 37.6]
    plan = pipeline.plan_buildings(bbox)
    output = tmp_path / 'buildings.gpkg'
    def interrupt(progress):
        raise KeyboardInterrupt('Synthetic interruption after committed group')
    with pytest.raises(KeyboardInterrupt):
        pipeline.acquire_buildings(bbox, output, budget, plan, progress=interrupt)
    assert not output.exists()
    assert output.with_suffix('.part.gpkg').exists()
    result = pipeline.acquire_buildings(bbox, output, budget, plan)
    assert len(synthetic_remote[-1].prefetch_calls) == 1  # Committed first group was not reacquired.
    assert result['features'] == 4
    assert result['unresolved_height'] == 1
    assert result['duplicate_source_id_groups'] == 0
    assert pipeline.acquire_buildings(bbox, output, budget, plan)['reused']
    with output.open('r+b') as file:
        file.seek(0)
        file.write(b'CORRUPT!')
    with pytest.raises(RuntimeError, match='checksum mismatch'):
        pipeline.acquire_buildings(bbox, output, budget, plan)


def test_changed_recipe_preserves_partial(synthetic_remote, tmp_path):
    budget = Budget(tmp_path, stage_root=tmp_path)
    bbox = [126.8, 37.4, 127.1, 37.6]
    plan = pipeline.plan_buildings(bbox)
    output = tmp_path / 'buildings.gpkg'
    def interrupt(progress): raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        pipeline.acquire_buildings(bbox, output, budget, plan, progress=interrupt)
    from shapely.geometry import box
    with pytest.raises(RuntimeError, match='identity/recipe mismatch'):
        pipeline.acquire_buildings(bbox, output, budget, plan, support_wgs84=box(126.9, 37.5, 127.0, 37.51))
    assert output.with_suffix('.part.gpkg').exists()


def test_corrupt_committed_partial_is_not_skipped(synthetic_remote, tmp_path):
    from osgeo import gdal

    budget = Budget(tmp_path, stage_root=tmp_path)
    bbox = [126.8, 37.4, 127.1, 37.6]
    plan = pipeline.plan_buildings(bbox)
    output = tmp_path / 'buildings.gpkg'
    def interrupt(progress): raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        pipeline.acquire_buildings(bbox, output, budget, plan, progress=interrupt)
    partial = output.with_suffix('.part.gpkg')
    # Use the native driver so valid GeoPackage spatial-index triggers remain
    # available while deliberately changing just one committed source value.
    database = gdal.OpenEx(str(partial), gdal.OF_VECTOR | gdal.OF_UPDATE)
    layer = database.GetLayerByName('buildings')
    feature = layer.GetFeature(1)
    feature.SetField('source_id', 'synthetically-corrupted-id')
    layer.SetFeature(feature)
    database.FlushCache()
    feature = layer = database = None
    with pytest.raises(RuntimeError, match='checkpoint rows changed'):
        pipeline.acquire_buildings(bbox, output, budget, plan)
    assert partial.exists()
    assert not output.exists()
