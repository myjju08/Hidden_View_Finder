"""Synthetic dependency mirror tests; no native package downloads/installations."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.data.bootstrap import acquire_native
from seoul_visibility import acquisition_safety as safety


@pytest.fixture
def package():
    return {'name': 'synthetic_1_amd64.deb', 'bytes': 10, 'publisher_sha256': 'a' * 64,
            'publisher_md5': 'b' * 32, 'snapshot_id': '20260824T000000Z',
            'url': 'https://archive.ubuntu.com/ubuntu/pool/synthetic_1_amd64.deb',
            'snapshot_url': 'https://snapshot.ubuntu.com/ubuntu/20260824T000000Z/pool/synthetic_1_amd64.deb'}


def test_missing_live_package_uses_exact_immutable_bytes(package, monkeypatch, tmp_path):
    calls = []
    def download(url, path, budget, **options):
        calls.append((url, Path(path), options))
        if len(calls) == 1: raise safety.AcquisitionError('missing_source', 'Synthetic404')
        return {'status': 'fixture-validated', 'sha256': options['expected_sha256']}
    monkeypatch.setattr(safety, 'guarded_download', download)
    path, record = acquire_native(package, tmp_path, None)
    assert len(calls) == 2
    assert calls[0][1] != calls[1][1]
    assert calls[1][0] == package['snapshot_url']
    assert calls[0][2]['expected_sha256'] == calls[1][2]['expected_sha256'] == package['publisher_sha256']
    assert calls[0][2]['expected_size'] == calls[1][2]['expected_size'] == 10
    assert record['pinned_version_unchanged']
    assert 'snapshot-20260824T000000Z' in str(path)


@pytest.mark.parametrize('status', ['authentication_blocked', 'source_changed', 'network_error', 'corrupt_content'])
def test_barrier_or_changed_bytes_never_triggers_mirror(package, monkeypatch, tmp_path, status):
    calls = []
    def download(*args, **kwargs):
        calls.append(args[0])
        raise safety.AcquisitionError(status, 'Synthetic barrier')
    monkeypatch.setattr(safety, 'guarded_download', download)
    with pytest.raises(safety.AcquisitionError): acquire_native(package, tmp_path, None)
    assert len(calls) == 1


@pytest.mark.parametrize('tail,accepted', [(b'\0' * 65536, True),
    (b'\0' * 65536 + b'not-padding', False), (b'\0' * (1200 * 1024), False)],
    ids=['valid-padding', 'nonzero-tail', 'oversized-tail'])
def test_native_tar_padding_is_bounded_and_drained_before_wait(monkeypatch, tmp_path, tail, accepted):
    import io
    import tarfile
    from scripts.data import bootstrap

    encoded = io.BytesIO()
    with tarfile.open(fileobj=encoded, mode='w') as archive:
        member = tarfile.TarInfo('usr/share/synthetic-fixture.txt')
        member.size = 7
        archive.addfile(member, io.BytesIO(b'fixture'))
    payload = encoded.getvalue() + tail
    class Process:
        def __init__(self):
            self.stdout = io.BytesIO(payload)
            self.returncode = None
            self.killed = False
        def poll(self): return self.returncode
        def kill(self): self.killed = True; self.returncode = -9
        def wait(self, timeout=None):
            if not self.killed:
                assert self.stdout.tell() == len(payload), 'Waiting before draining stdout can deadlock dpkg-deb'
                self.returncode = 0
            return self.returncode
    child = Process()
    monkeypatch.setattr(bootstrap.subprocess, 'Popen', lambda *args, **kwargs: child)
    destination = tmp_path / 'native'
    budget = safety.Budget(tmp_path, stage_root=tmp_path / 'staging')
    if accepted:
        assert bootstrap.extract_deb(tmp_path / 'synthetic.deb', destination, budget) == 7
        assert (destination / 'usr/share/synthetic-fixture.txt').read_bytes() == b'fixture'
        assert not child.killed
    else:
        with pytest.raises(RuntimeError, match='trailing data'):
            bootstrap.extract_deb(tmp_path / 'synthetic.deb', destination, budget)
        assert child.killed
