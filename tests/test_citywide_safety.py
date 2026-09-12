"""Synthetic, no-network/no-large-write acquisition safety regressions."""
from __future__ import annotations

import hashlib
import io
import json
import multiprocessing
import os
from pathlib import Path
import stat
from types import SimpleNamespace
from zipfile import ZipFile, ZipInfo

import pytest

from seoul_visibility.acquisition_safety import (
    AcquisitionError, Budget, atomic_json, guarded_download, redact_url,
    safe_extract_zip, sha256,
)
from seoul_visibility.errors import ResourceBudgetError
from seoul_visibility.resources import HARD_TOTAL_STORAGE_BYTES, StoragePolicy

URL = 'https://fixture.invalid/synthetic.bin'


@pytest.fixture
def budget(tmp_path, monkeypatch):
    import seoul_visibility.acquisition_safety as safety
    monkeypatch.setattr(safety.shutil, 'disk_usage', lambda p: SimpleNamespace(free=100_000_000_000, total=200_000_000_000))
    return Budget(tmp_path, stage_root=tmp_path / 'staging')


class Response(io.BytesIO):
    def __init__(self, data=b'DATAfixture', *, status=200, headers=None, url=URL):
        super().__init__(data)
        self.status = status
        self.headers = {'ETag': '"synthetic-v1"', **(headers or {})}
        self.url = url

    def geturl(self):
        return self.url


def fetch(budget, **kwargs):
    return guarded_download(URL, budget.root / 'fixture.bin', budget,
                            max_bytes=1000, max_retries=0, magic=b'DATA', **kwargs)


def test_decimal_ceiling_rejects_old_default_and_invalid_limits(tmp_path):
    assert StoragePolicy().total_budget_bytes == 20_000_000_000
    assert Budget(tmp_path).limit == HARD_TOTAL_STORAGE_BYTES
    for value in (20 * 1024**3, 20_000_000_001, -1, True):
        with pytest.raises(ResourceBudgetError):
            Budget(tmp_path, limit=value)
    with pytest.raises(ResourceBudgetError):
        StoragePolicy(total_budget_bytes=20 * 1024**3)
    with pytest.raises(ResourceBudgetError):
        Budget(tmp_path, min_free=0)


def test_existing_accounting_includes_hidden_partial_journal_and_hardlinks(budget):
    root = budget.root
    (root / 'artifact.part').write_bytes(b'x' * 10000)
    (root / 'data.gpkg-wal').write_bytes(b'x' * 9000)
    (root / '.cache').mkdir()
    (root / '.cache/item').write_bytes(b'x' * 8000)
    os.link(root / 'artifact.part', root / 'same-hardlink')
    snap = budget.snapshot()
    assert snap['logical_bytes'] < 10000 * 2 + 9000 + 8000 + 10000
    assert snap['accounted_bytes'] >= 27000
    assert snap['temporary_bytes'] >= 27000  # a non-temporary hardlink alias cannot hide staging
    assert snap['allocated_bytes'] > 0


def test_refuses_before_any_payload_when_existing_exceeds_ceiling(budget):
    (budget.root / 'existing').write_bytes(b'keep')
    budget.limit = 1
    with pytest.raises(ResourceBudgetError, match='storage_blocked'):
        with budget.reserve(1_000_000):
            pytest.fail('writer must never be entered')
    assert (budget.root / 'existing').read_bytes() == b'keep'
    assert not budget.lock_path.exists()


def test_reservation_margin_and_stage_cap_before_write(budget):
    budget.limit = budget.snapshot()['accounted_bytes'] + 1_000_000
    with pytest.raises(ResourceBudgetError):
        with budget.reserve(800_000):
            pytest.fail('25% plus checkpoint allowance must be reserved')
    budget.limit = HARD_TOTAL_STORAGE_BYTES
    with pytest.raises(ResourceBudgetError, match='temporary'):
        with budget.reserve(1, temporary_bytes=4 * 1024**3):
            pytest.fail('temporary cap includes safety margin')


def test_reservation_rechecks_before_midstream_write(budget, monkeypatch):
    import seoul_visibility.acquisition_safety as safety
    with budget.reserve(200_000, temporary_bytes=200_000) as reservation:
        target = budget.root / 'stage.part'
        reservation.check_write(10, target)
        target.write_bytes(b'first')
        monkeypatch.setattr(safety.shutil, 'disk_usage', lambda p: SimpleNamespace(free=8 * 1024**3 + 1000, total=200_000_000_000))
        with pytest.raises(ResourceBudgetError, match='free'):
            reservation.check_write(10, target)
        # Restore simulated free space so exiting the reservation can record its final sample.
        monkeypatch.setattr(safety.shutil, 'disk_usage', lambda p: SimpleNamespace(free=100_000_000_000, total=200_000_000_000))
        assert target.read_bytes() == b'first'


def _competing_writer(root, queue):
    try:
        with Budget(root).reserve(10000, temporary_bytes=10000):
            queue.put('unexpected-acquired')
    except ResourceBudgetError as exc:
        queue.put(str(exc))


def test_cross_process_shared_reservation_and_stale_lock_recovery(budget):
    with budget.reserve(10000, temporary_bytes=10000):
        ctx = multiprocessing.get_context('fork')
        queue = ctx.Queue()
        process = ctx.Process(target=_competing_writer, args=(budget.root, queue))
        process.start()
        process.join(5)
        assert process.exitcode == 0
        assert 'shared reservation lock' in queue.get(timeout=1)
    # Stale metadata remains but advisory lock is released; never unlink the inode.
    inode = budget.lock_path.stat().st_ino
    with budget.reserve(10000, temporary_bytes=10000):
        pass
    assert budget.lock_path.stat().st_ino == inode


def test_symlink_output_and_existing_tree_escape_rejected(budget, tmp_path):
    outside = tmp_path.parent / (tmp_path.name + '-outside')
    outside.mkdir()
    (budget.root / 'link').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ResourceBudgetError, match='symlink'):
        budget.safe_path(budget.root / 'link/payload')
    with pytest.raises(ResourceBudgetError, match='symlink'):
        budget.snapshot()
    assert not list(outside.iterdir())


def test_valid_download_atomic_publication_and_reuse_offline(budget):
    payload = b'DATAfixture'
    result = fetch(budget, open_url=lambda *a, **k: Response(payload),
                   expected_sha256=hashlib.sha256(payload).hexdigest(), expected_size=len(payload))
    assert result['status'] == 'acquired_validated'
    assert not (budget.root / 'fixture.bin.part').exists()
    def no_network(*args, **kwargs):
        pytest.fail('validated reuse must not perform network request')
    reused = fetch(budget, open_url=no_network)
    assert reused['status'] == 'reused_validated'
    assert reused['network_bytes_this_run'] == 0


def test_corrupt_existing_is_preserved_and_rejected(budget):
    fetch(budget, open_url=lambda *a, **k: Response())
    target = budget.root / 'fixture.bin'
    target.write_bytes(b'DATAcorrupt')
    with pytest.raises(AcquisitionError, match='SHA-256'):
        fetch(budget, open_url=lambda *a, **k: pytest.fail('must not fetch'))
    assert target.read_bytes() == b'DATAcorrupt'


@pytest.mark.parametrize('response,pattern', [
    (lambda: Response(b'<html>login</html>'), 'HTML'),
    (lambda: Response(b'DATAshort', headers={'Content-Length': '100'}), 'Truncated'),
    (lambda: Response(b'DATApayload', headers={'Content-Length': '2'}), 'declared'),
    (lambda: Response(b'DATApayload', headers={'Content-Length': '1001'}), 'Content-Length'),
    (lambda: Response(b'DATApayload', url='https://undocumented.invalid/file'), 'redirect'),
    (lambda: Response(b'DATApayload', url='http://fixture.invalid/file'), 'unencrypted'),
])
def test_download_rejects_corruption_size_and_redirect(budget, response, pattern):
    with pytest.raises(AcquisitionError, match=pattern):
        fetch(budget, open_url=lambda *a, **k: response())
    assert not (budget.root / 'fixture.bin').exists()


def test_missing_content_length_is_bounded_and_validated(budget):
    result = fetch(budget, open_url=lambda *a, **k: Response())
    assert result['size_bytes'] == len(b'DATAfixture')
    assert 'no independent publisher' in result['integrity']


def test_missing_length_over_cap_never_writes_excess(budget):
    with pytest.raises(AcquisitionError, match='transfer cap'):
        fetch(budget, open_url=lambda *a, **k: Response(b'DATA' + b'x' * 2000))
    assert not (budget.root / 'fixture.bin').exists()
    assert (budget.root / 'fixture.bin.part').stat().st_size <= 1000


def _partial(budget):
    (budget.root / 'fixture.bin.part').write_bytes(b'DATA')
    atomic_json(budget.root / 'fixture.bin.part.json', {'url': URL, 'etag': '"synthetic-v1"', 'object_size': 11,
                                                      'received_bytes': 4}, budget)


@pytest.mark.parametrize('status,headers,pattern', [
    (200, {'Content-Length': '11'}, 'expected 206'),
    (206, {'Content-Range': 'bytes 0-6/11'}, 'Wrong resumed'),
    (206, {'Content-Range': 'bytes 4-10/11', 'ETag': '"changed"'}, 'identity mismatch'),
    (206, {'Content-Range': 'bytes 4-10/11', 'Content-Length': '6'}, 'disagrees'),
    (206, {}, 'identity mismatch'),
])
def test_resume_refuses_ignored_or_wrong_range_and_changed_object(budget, status, headers, pattern):
    _partial(budget)
    with pytest.raises(AcquisitionError, match=pattern):
        fetch(budget, open_url=lambda *a, **k: Response(b'fixture', status=status, headers=headers))
    assert (budget.root / 'fixture.bin.part').read_bytes() == b'DATA'


def test_resume_strong_identity_and_exact_range(budget):
    _partial(budget)
    def opener(request, **kwargs):
        assert request.get_header('Range') == 'bytes=4-'
        assert request.get_header('If-range') == '"synthetic-v1"'
        return Response(b'fixture', status=206, headers={'Content-Range': 'bytes 4-10/11'})
    result = fetch(budget, open_url=opener)
    assert result['network_bytes_this_run'] == 7
    assert (budget.root / 'fixture.bin').read_bytes() == b'DATAfixture'


def test_interruption_preserves_partial_with_identity_and_resumes(budget, monkeypatch):
    import seoul_visibility.acquisition_safety as safety
    monkeypatch.setattr(safety, 'CHUNK', 4)
    class Interrupted(Response):
        def read(self, size=-1):
            if self.tell() >= 4:
                raise ConnectionResetError('synthetic interruption')
            return super().read(size)
    with pytest.raises(AcquisitionError, match='retries exhausted'):
        fetch(budget, open_url=lambda *a, **k: Interrupted(headers={'Content-Length': '11'}))
    assert (budget.root / 'fixture.bin.part').read_bytes() == b'DATA'
    assert json.loads((budget.root / 'fixture.bin.part.json').read_text())['etag'] == '"synthetic-v1"'
    result = fetch(budget, open_url=lambda *a, **k: Response(b'fixture', status=206, headers={'Content-Range': 'bytes 4-10/11'}))
    assert result['size_bytes'] == 11


def test_partial_without_reliable_validator_blocks_append(budget):
    _partial(budget)
    state_path = budget.root / 'fixture.bin.part.json'
    state = json.loads(state_path.read_text()); state['etag'] = 'W/"weak"'
    atomic_json(state_path, state, budget)
    with pytest.raises(AcquisitionError, match='no strong ETag'):
        fetch(budget, open_url=lambda *a, **k: pytest.fail('must not request'))


def test_pinned_hash_change_preserves_completed_partial(budget):
    with pytest.raises(AcquisitionError, match='Pinned SHA-256 mismatch'):
        fetch(budget, expected_sha256='0' * 64, open_url=lambda *a, **k: Response())
    assert not (budget.root / 'fixture.bin').exists()
    assert (budget.root / 'fixture.bin.part').read_bytes() == b'DATAfixture'


@pytest.mark.parametrize('name,mode', [('../escape', None), ('/absolute', None), ('a\\..\\escape', None),
                                      ('link', stat.S_IFLNK | 0o777), ('device', stat.S_IFCHR | 0o600)])
def test_unsafe_zip_rejected_before_extraction(budget, name, mode):
    archive = budget.root / 'source.zip'
    with ZipFile(archive, 'w') as zipped:
        member = ZipInfo(name)
        if mode:
            member.create_system = 3
            member.external_attr = mode << 16
        zipped.writestr(member, b'unsafe')
    with pytest.raises(AcquisitionError, match='unsafe_archive'):
        safe_extract_zip(archive, budget.root / 'extracted', budget, max_expanded_bytes=100)
    assert not (budget.root / 'extracted').exists()


def test_zip_bounded_expansion_valid_reuse_and_corruption(budget):
    archive = budget.root / 'source.zip'
    with ZipFile(archive, 'w') as zipped:
        zipped.writestr('layer.shp', b'synthetic geometry')
        zipped.writestr('layer.prj', b'synthetic CRS')
    with pytest.raises(AcquisitionError, match='expansion'):
        safe_extract_zip(archive, budget.root / 'out', budget, max_expanded_bytes=10)
    assert not (budget.root / 'out').exists()
    result = safe_extract_zip(archive, budget.root / 'out', budget, max_expanded_bytes=100)
    assert result['written_bytes_this_run'] > 0
    reused = safe_extract_zip(archive, budget.root / 'out', budget, max_expanded_bytes=100)
    assert reused['written_bytes_this_run'] == 0
    (budget.root / 'out/layer.prj').write_bytes(b'corrupt')
    with pytest.raises(AcquisitionError, match='differs from archive'):
        safe_extract_zip(archive, budget.root / 'out', budget, max_expanded_bytes=100)


def test_atomic_json_refuses_unowned_interrupted_temporary(budget):
    final = budget.root / 'state.json'
    atomic_json(final, {'old': True}, budget)
    (budget.root / 'state.json.writing').write_text('preserve interruption')
    with pytest.raises(FileExistsError):
        atomic_json(final, {'new': True}, budget)
    assert json.loads(final.read_text()) == {'old': True}
    assert (budget.root / 'state.json.writing').read_text() == 'preserve interruption'


def test_secret_redaction():
    redacted = redact_url('https://user:password@source.invalid/object?token=private&X-Amz-Signature=private&part=3#secret')
    assert 'private' not in redacted
    assert 'password' not in redacted
    assert 'part=3' in redacted
    assert '#secret' not in redacted


def test_actual_zip_expansion_over_metadata_stops_before_write(budget, monkeypatch):
    import seoul_visibility.acquisition_safety as safety
    archive = budget.root / 'synthetic.zip'; archive.write_bytes(b'synthetic container fixture')
    member = ZipInfo('layer.shp'); member.file_size = 4
    class UnexpectedExpansion:
        def __init__(self, *args): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def infolist(self): return [member]
        def open(self, member): return io.BytesIO(b'longer-than-metadata')
    monkeypatch.setattr(safety, 'ZipFile', UnexpectedExpansion)
    with pytest.raises(AcquisitionError, match='Actual expanded bytes'):
        safe_extract_zip(archive, budget.root / 'out', budget, max_expanded_bytes=100)
    assert not (budget.root / 'out/layer.shp').exists()
    assert (budget.root / 'out/layer.shp.part').stat().st_size == 0


def test_interrupted_zip_resumes_only_owned_partial_with_verified_source(budget, monkeypatch):
    import seoul_visibility.acquisition_safety as safety
    archive = budget.root / 'source.zip'
    with ZipFile(archive, 'w') as zipped:
        zipped.writestr('layer.shp', b'complete-synthetic-geometry')
    original = safety.Reservation.check_write
    def interrupt(reservation, n, path):
        path = Path(path)
        if path.suffix == '.part' and path.exists() and path.stat().st_size >= 4:
            raise KeyboardInterrupt('synthetic extraction interruption')
        return original(reservation, n, path)
    monkeypatch.setattr(safety, 'CHUNK', 4)
    monkeypatch.setattr(safety.Reservation, 'check_write', interrupt)
    with pytest.raises(KeyboardInterrupt):
        safe_extract_zip(archive, budget.root / 'out', budget, max_expanded_bytes=100)
    assert (budget.root / 'out/layer.shp.part').read_bytes() == b'comp'
    unrelated = budget.root / 'out/unrelated.part'; unrelated.write_bytes(b'keep')
    monkeypatch.setattr(safety.Reservation, 'check_write', original)
    result = safe_extract_zip(archive, budget.root / 'out', budget, max_expanded_bytes=100)
    assert (budget.root / 'out/layer.shp').read_bytes() == b'complete-synthetic-geometry'
    assert unrelated.read_bytes() == b'keep'
    preserved = result['preserved_interrupted_members'][0]
    assert preserved['status'] == 'preserved'
    assert preserved['deletion_performed'] is False
    assert Path(preserved['preserved_path']).read_bytes() == b'comp'
    assert preserved['recovery_source_sha256'] == sha256(archive)
    assert result['cleanup'] == []
    assert archive.exists()


def test_unowned_zip_partial_is_preserved(budget):
    archive = budget.root / 'source.zip'
    with ZipFile(archive, 'w') as zipped:
        zipped.writestr('layer.shp', b'complete')
    (budget.root / 'out').mkdir()
    partial = budget.root / 'out/layer.shp.part'; partial.write_bytes(b'user file')
    with pytest.raises(AcquisitionError, match='Unowned extraction partial'):
        safe_extract_zip(archive, budget.root / 'out', budget, max_expanded_bytes=100)
    assert partial.read_bytes() == b'user file'


def test_cleanup_refuses_changed_recovery_source(budget):
    from seoul_visibility.acquisition_safety import safe_cleanup
    source = budget.root / 'recoverable'; source.write_bytes(b'complete')
    partial = budget.root / 'owned.part'; partial.write_bytes(b'partial')
    owner = budget.root / 'owner.json'
    atomic_json(owner, {'schema': 'citywide-owned-partial-v1', 'task_owned': True,
                       'disposable': True, 'exclusive_creation': True,
                       'path': str(partial), 'recovery_source': str(source),
                       'recovery_source_sha256': sha256(source)}, budget)
    source.write_bytes(b'changed')
    with pytest.raises(AcquisitionError, match='Recovery source is missing or changed'):
        safe_cleanup(partial, {'ownership_record': str(owner), 'artifact_sha256': sha256(partial)}, budget)
    assert partial.exists()


def test_completed_partial_reconfirmed_by_head_then_published_without_body(budget):
    payload = b'DATAfixture'
    _partial(budget)
    (budget.root / 'fixture.bin.part').write_bytes(payload)
    calls = []
    def opener(request, **kwargs):
        calls.append(request.get_method())
        return Response(b'', headers={'Content-Length': str(len(payload))})
    result = fetch(budget, expected_sha256=hashlib.sha256(payload).hexdigest(), open_url=opener)
    assert calls == ['HEAD']
    assert result['recovered_complete_partial'] is True
    assert result['network_bytes_this_run'] == 0
    assert (budget.root / 'fixture.bin').read_bytes() == payload


def test_completed_partial_changed_remote_identity_is_preserved(budget):
    payload = b'DATAfixture'
    _partial(budget)
    (budget.root / 'fixture.bin.part').write_bytes(payload)
    with pytest.raises(AcquisitionError, match='HEAD identity/size changed'):
        fetch(budget, expected_sha256=hashlib.sha256(payload).hexdigest(),
              open_url=lambda *a, **k: Response(b'', headers={'Content-Length': '11', 'ETag': '"changed"'}))
    assert (budget.root / 'fixture.bin.part').read_bytes() == payload
    assert not (budget.root / 'fixture.bin').exists()


def test_completed_partial_requires_checksum_or_source_validator(budget):
    _partial(budget)
    (budget.root / 'fixture.bin.part').write_bytes(b'DATAfixture')
    with pytest.raises(AcquisitionError, match='pinned checksum or source validator'):
        fetch(budget, open_url=lambda *a, **k: pytest.fail('must not request'))


def test_hidden_staging_component_counts_descendants_and_hardlink_alias(budget):
    original = budget.root / 'normalized.gpkg'; original.write_bytes(b'x' * 10000)
    stage = budget.root / '.package-v1.staging'; stage.mkdir()
    (stage / 'nested').mkdir()
    os.link(original, stage / 'nested/index.gpkg')
    snap = budget.snapshot()
    assert snap['temporary_bytes'] >= max(original.stat().st_size, original.stat().st_blocks * 512)
    assert snap['accounted_bytes'] < 2 * 10000 + 5 * 4096


def test_bounded_small_writes_use_prospective_credit_and_fewer_scans(budget, monkeypatch):
    calls = []
    original_snapshot = budget.snapshot
    def measured():
        calls.append(1)
        return original_snapshot()
    monkeypatch.setattr(budget, 'snapshot', measured)
    with budget.reserve(2_000_000, 2_000_000, 'synthetic small-file batch') as reservation:
        before = len(calls)
        with reservation.bounded_small_writes():
            for i in range(32):
                path = budget.root / f'synthetic-{i}.part'
                reservation.check_write(100, path)
                path.write_bytes(b'x' * 100)
        assert len(calls) - before <= 3
    assert len(list(budget.root.glob('synthetic-*.part'))) == 32


def test_bounded_small_writes_recheck_free_before_each_write(budget, monkeypatch):
    import seoul_visibility.acquisition_safety as safety
    with budget.reserve(2_000_000, 2_000_000, 'synthetic free-space drop') as reservation:
        with reservation.bounded_small_writes():
            first = budget.root / 'first.part'
            reservation.check_write(10, first); first.write_bytes(b'first')
            monkeypatch.setattr(safety.shutil, 'disk_usage', lambda p: SimpleNamespace(free=8 * 1024**3 + 1000, total=200_000_000_000))
            with pytest.raises(ResourceBudgetError, match='free space fell'):
                reservation.check_write(10, budget.root / 'refused.part')
            assert not (budget.root / 'refused.part').exists()
            monkeypatch.setattr(safety.shutil, 'disk_usage', lambda p: SimpleNamespace(free=100_000_000_000, total=200_000_000_000))


def test_speculative_temporary_allocation_refused_before_next_write(budget, monkeypatch):
    original = budget.snapshot
    with budget.reserve(40_000_000, 1_000_000, 'synthetic speculative allocation') as reservation:
        def inflated():
            snap = original()
            for field in ('accounted_bytes', 'allocated_bytes', 'temporary_bytes'):
                snap[field] += 8 * 1024**2
            return snap
        monkeypatch.setattr(budget, 'snapshot', inflated)
        with pytest.raises(ResourceBudgetError, match='temporary reservation before write'):
            reservation.check_write(1024**2, budget.root / 'refused.part')
        assert not (budget.root / 'refused.part').exists()
        assert inflated()['accounted_bytes'] < budget.limit
        monkeypatch.setattr(budget, 'snapshot', original)


def test_zip_reserves_filesystem_allocation_slack_inside_both_caps(budget, monkeypatch):
    archive = budget.root / 'synthetic-preallocation.zip'
    payload = b'x' * (2 * 1024**2)
    with ZipFile(archive, 'w') as zipped:
        zipped.writestr('layer.bin', payload)
    original = budget.snapshot
    partial = budget.root / 'out/layer.bin.part'
    observed = []
    def inflated():
        snap = original()
        if partial.exists() and partial.stat().st_size:
            # Model a filesystem retaining a16MiB speculative extent until close;
            # no actual extra bytes are written or actual disk pressure created.
            for field in ('accounted_bytes', 'allocated_bytes', 'temporary_bytes'):
                snap[field] += 16 * 1024**2
            observed.append(snap['temporary_bytes'])
        return snap
    monkeypatch.setattr(budget, 'snapshot', inflated)
    result = safe_extract_zip(archive, budget.root / 'out', budget, max_expanded_bytes=len(payload))
    assert observed
    assert result['expanded_bytes'] == len(payload)
    assert (budget.root / 'out/layer.bin').read_bytes() == payload


def test_keep_size_preallocation_preserves_logical_partial_length(budget):
    from seoul_visibility.acquisition_safety import preallocate_keep_size
    partial = budget.root / 'preallocated.part'
    with budget.reserve(4 * 1024**2, 4 * 1024**2, 'synthetic exact preallocation') as reservation:
        with partial.open('xb') as output:
            output.write(b'DATA'); output.flush()
            result = preallocate_keep_size(output, 1024**2, partial, reservation)
            assert partial.stat().st_size == 4
            assert partial.stat().st_blocks * 512 >= 1024**2
            assert result['logical_size_before_bytes'] == 4
        assert partial.read_bytes() == b'DATA'


def test_unsupported_preallocation_refuses_before_payload(budget, monkeypatch):
    import ctypes
    monkeypatch.setattr(ctypes, 'CDLL', lambda *a, **k: object())
    with pytest.raises(AcquisitionError, match='preallocation_unavailable'):
        fetch(budget, open_url=lambda *a, **k: Response())
    partial = budget.root / 'fixture.bin.part'
    assert partial.stat().st_size == 0
    assert not (budget.root / 'fixture.bin').exists()


def test_empty_preallocated_partial_resumes_without_exclusive_create_conflict(budget):
    partial = budget.root / 'fixture.bin.part'; partial.touch()
    atomic_json(budget.root / 'fixture.bin.part.json', {'url': URL, 'etag': '"synthetic-v1"',
               'object_size': 11, 'received_bytes': 0}, budget)
    result = fetch(budget, open_url=lambda *a, **k: Response(headers={'Content-Length': '11'}))
    assert result['size_bytes'] == 11
    assert (budget.root / 'fixture.bin').read_bytes() == b'DATAfixture'


def test_download_invokes_exact_keep_size_bound(budget, monkeypatch):
    import seoul_visibility.acquisition_safety as safety
    real = safety.preallocate_keep_size
    seen = []
    def record(handle, size, path, reservation):
        seen.append((size, Path(path).stat().st_size))
        return real(handle, size, path, reservation)
    monkeypatch.setattr(safety, 'preallocate_keep_size', record)
    result = fetch(budget, open_url=lambda *a, **k: Response(headers={'Content-Length': '11'}))
    assert seen == [(11, 0)]
    assert result['preallocation']['method'] == 'Linux fallocate FALLOC_FL_KEEP_SIZE'


def test_additional_accounted_allowance_counts_without_changing_measurements(budget):
    measured = budget.snapshot()
    budget.additional_accounted_bytes = 5 * 1024**2
    snap = budget.snapshot()
    assert snap['accounted_bytes'] == measured['accounted_bytes'] + 5 * 1024**2
    assert snap['logical_bytes'] == measured['logical_bytes']
    assert snap['allocated_bytes'] == measured['allocated_bytes']
    assert snap['measured_accounted_bytes'] == measured['accounted_bytes']
    assert snap['additional_accounted_bytes'] == 5 * 1024**2


def test_additional_accounted_allowance_refuses_before_write(budget):
    current = budget.snapshot()['accounted_bytes']
    budget.limit = current + 1_000_000
    budget.additional_accounted_bytes = 1_000_000
    with pytest.raises(ResourceBudgetError, match='storage_blocked'):
        with budget.reserve(10_000, 10_000, 'synthetic external footprint'):
            pytest.fail('accounted allowance must consume total ceiling before writer entry')
    assert not budget.lock_path.exists()
    with pytest.raises(ResourceBudgetError):
        Budget(budget.root, additional_accounted_bytes=-1)
    with pytest.raises(ResourceBudgetError):
        Budget(budget.root, additional_accounted_bytes=20_000_000_001)


def test_checkpoint_reserve_is_16_mib_and_protects_staging(budget):
    from seoul_visibility.acquisition_safety import REPORT_RESERVE
    assert REPORT_RESERVE == 16 * 1024**2
    assert budget.snapshot()['checkpoint_reserve_bytes'] == REPORT_RESERVE
    budget.stage_limit = REPORT_RESERVE - 1
    with pytest.raises(ResourceBudgetError, match='temporary'):
        budget.check()


def test_checkpoint_can_use_protected_room_when_normal_write_refused(budget, monkeypatch):
    from seoul_visibility.acquisition_safety import REPORT_RESERVE
    original = budget.snapshot
    def pressured():
        snap = original()
        snap['accounted_bytes'] = budget.limit - REPORT_RESERVE // 2
        return snap
    monkeypatch.setattr(budget, 'snapshot', pressured)
    with pytest.raises(ResourceBudgetError, match='storage_blocked'):
        atomic_json(budget.root / 'normal.json', {'status': 'normal'}, budget)
    atomic_json(budget.root / 'checkpoint.json', {'status': 'storage_blocked'}, budget, checkpoint=True)
    assert json.loads((budget.root / 'checkpoint.json').read_text())['status'] == 'storage_blocked'
    assert not (budget.root / 'normal.json').exists()


def test_checkpoint_payload_limit_refuses_before_write(budget):
    from seoul_visibility.acquisition_safety import MAX_METADATA_BYTES
    with pytest.raises(AcquisitionError, match='8 MiB'):
        atomic_json(budget.root / 'oversized.json', {'value': 'x' * MAX_METADATA_BYTES}, budget, checkpoint=True)
    assert not (budget.root / 'oversized.json').exists()
    assert not (budget.root / 'oversized.json.writing').exists()


def _competing_checkpoint(root, queue):
    try:
        atomic_json(Path(root) / 'racing-checkpoint.json', {'status': 'synthetic'}, Budget(root), checkpoint=True)
        queue.put('unexpected-wrote')
    except ResourceBudgetError as exc:
        queue.put(str(exc))


def test_checkpoint_obeys_live_shared_writer_lock(budget):
    with budget.reserve(10000, temporary_bytes=10000):
        ctx = multiprocessing.get_context('fork')
        queue = ctx.Queue()
        process = ctx.Process(target=_competing_checkpoint, args=(budget.root, queue))
        process.start(); process.join(5)
        assert process.exitcode == 0
        assert 'shared reservation lock' in queue.get(timeout=1)
    assert not (budget.root / 'racing-checkpoint.json').exists()


def test_inherited_reservation_requires_explicit_live_descriptor(budget, monkeypatch):
    for name in ('HVF_BUDGET_RESERVATION_FD', 'HVF_BUDGET_PARENT_PID', 'HVF_BUDGET_RESERVATION_ID'):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ResourceBudgetError, match='no valid live inherited writer reservation'):
        Budget(budget.root, inherited_reservation=True)


def test_bounded_child_checkpoint_reuses_verified_parent_reservation(budget):
    import sys
    from seoul_visibility.acquisition_safety import run_bounded
    code = (
        'import sys; from pathlib import Path; '
        'from seoul_visibility.acquisition_safety import Budget,atomic_json; '
        'root=Path(sys.argv[1]); '
        'b=Budget(root,stage_root=root/"staging",inherited_reservation=True); '
        'atomic_json(root/"child-state.json",{"ok":True},b,checkpoint=True)'
    )
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
           'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
    result = run_bounded([sys.executable, '-c', code, str(budget.root)], budget,
                         peak_bytes=1024**2, temporary_bytes=1024**2,
                         cwd=budget.root, env=env, timeout=15,
                         log_path=budget.root / 'child.log')
    assert result['returncode'] == 0
    assert json.loads((budget.root / 'child-state.json').read_text()) == {'ok': True}
    # Child exit must not leave a stale or separately unlocked writer inode.
    with budget.reserve(10000, 10000, 'post-child writer'):
        pass


def test_bounded_child_rejects_changed_delegated_budget(budget):
    import sys
    from seoul_visibility.acquisition_safety import run_bounded
    code = (
        'import sys; from pathlib import Path; '
        'from seoul_visibility.acquisition_safety import Budget; '
        'root=Path(sys.argv[1]); '
        'Budget(root,stage_root=root/"staging",limit=19999999999,inherited_reservation=True)'
    )
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
           'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
    with pytest.raises(AcquisitionError, match='child_failed'):
        run_bounded([sys.executable, '-c', code, str(budget.root)], budget,
                    peak_bytes=1024**2, temporary_bytes=1024**2,
                    cwd=budget.root, env=env, timeout=15,
                    log_path=budget.root / 'changed-child.log')
    assert 'no valid live inherited writer reservation' in (budget.root / 'changed-child.log').read_text()


def test_cleanup_requires_distinct_validated_successor_before_deletion(budget):
    from seoul_visibility.acquisition_safety import safe_cleanup
    source = budget.root / 'source'; source.write_bytes(b'synthetic source')
    partial = budget.root / 'interrupted.part'; partial.write_bytes(b'synthetic partial')
    owner = budget.root / 'owner.json'
    atomic_json(owner, {'schema': 'citywide-owned-partial-v1', 'task_owned': True,
                       'disposable': True, 'exclusive_creation': True,
                       'path': str(partial), 'recovery_source': str(source),
                       'recovery_source_sha256': sha256(source)}, budget)
    proof = {'ownership_record': str(owner), 'artifact_sha256': sha256(partial)}
    with pytest.raises(AcquisitionError, match='validated successor receipt is required'):
        safe_cleanup(partial, proof, budget)
    assert partial.exists()
    successor = budget.root / 'validated-successor'; successor.write_bytes(b'synthetic validated successor')
    receipt = budget.root / 'successor-receipt.json'
    atomic_json(receipt, {'valid': True, 'path': str(successor), 'sha256': sha256(successor),
                         'validation_kind': 'synthetic fixture, not GIS validation'}, budget)
    proof['validated_successor_receipt'] = str(receipt)
    event = safe_cleanup(partial, proof, budget)
    assert event['status'] == 'deleted'
    assert event['validated_successor_sha256'] == sha256(successor)
    assert not partial.exists()
    assert source.exists() and successor.exists()
