"""No-network regressions for acquisition budgets and owned temporary cleanup."""
from __future__ import annotations
import importlib
from pathlib import Path
import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'


@pytest.fixture
def common(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    module = importlib.import_module('building_acquisition_common')
    monkeypatch.setattr(module, 'preflight', lambda root, **kwargs: {'estimates': kwargs})
    return module


@pytest.fixture(params=['acquire_buildings', 'acquire_official_buildings'])
def downloader(request, monkeypatch, common):
    pytest.importorskip('requests', reason='Optional acquisition dependencies required')
    if request.param == 'acquire_buildings':
        pytest.importorskip('pyarrow', reason='Optional acquisition dependencies required')
    module = importlib.import_module(request.param)
    import requests
    import urllib.request
    def unexpected_network(*args, **kwargs):
        pytest.fail('Validation must not reach the network')
    monkeypatch.setattr(requests, 'post', unexpected_network)
    monkeypatch.setattr(urllib.request, 'urlopen', unexpected_network)
    return module


def test_output_outside_budget_root_rejected(downloader, tmp_path):
    root = tmp_path / 'accounted'
    outside = tmp_path / 'outside.gpkg'
    with pytest.raises(ValueError, match='inside --data-root'):
        downloader.main(['--data-root', str(root), '--output', str(outside)])
    assert not outside.exists()
    assert not root.exists()


def test_output_symlink_parent_escape_rejected(downloader, tmp_path):
    root = tmp_path / 'accounted'; root.mkdir()
    outside = tmp_path / 'outside'; outside.mkdir()
    (root / 'link').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='inside --data-root'):
        downloader.main(['--data-root', str(root), '--output', str(root / 'link/out.gpkg')])
    assert not list(outside.iterdir())


def test_metadata_symlink_escape_rejected(downloader, tmp_path):
    root = tmp_path / 'accounted'; root.mkdir()
    original = tmp_path / 'unrelated.json'; original.write_text('preserve')
    (root / 'new.source.json').symlink_to(original)
    with pytest.raises(ValueError, match='metadata must be inside'):
        downloader.main(['--data-root', str(root), '--output', str(root / 'new.gpkg')])
    assert original.read_text() == 'preserve'


@pytest.mark.parametrize('bounds', [
    ['nan', '37.4', '127.2', '37.8'],
    ['126.7', '37.4', 'inf', '37.8'],
    ['127.2', '37.4', '126.7', '37.8'],
    ['126.7', '37.4', '127.2', '37.4'],
])
def test_invalid_bounds_rejected_before_network(downloader, tmp_path, bounds):
    option = '--bbox' if downloader.__name__ == 'acquire_buildings' else '--bounds'
    with pytest.raises(ValueError, match='finite.*positive area'):
        downloader.main(['--data-root', str(tmp_path / 'data'), option, *bounds])


@pytest.mark.parametrize('cap', ['0', '-1'])
def test_nonpositive_download_caps_rejected(downloader, tmp_path, cap):
    with pytest.raises(ValueError, match='positive integer'):
        downloader.main(['--data-root', str(tmp_path / 'data'), '--max-download-mib', cap])


def test_custom_root_is_passed_to_preflight(downloader, common, monkeypatch, tmp_path):
    root = tmp_path / 'different-accounted-root'; calls = []
    def measured(actual_root, **kwargs):
        calls.append(Path(actual_root)); return {'current_bytes': 0}
    monkeypatch.setattr(common, 'preflight', measured)
    class StopBeforeNetwork(Exception): pass
    def stop(*args, **kwargs): raise StopBeforeNetwork
    if downloader.__name__ == 'acquire_buildings':
        monkeypatch.setattr(downloader, 'Ranges', stop)
    else:
        monkeypatch.setattr(downloader.requests, 'post', stop)
    with pytest.raises(StopBeforeNetwork):
        downloader.main(['--data-root', str(root)])
    assert calls == [root.resolve()]
    assert common.budget_check(root)['data_root'] == str(root.resolve())
    assert not list(root.rglob('*.part'))


def test_archive_outside_budget_root_rejected(monkeypatch, common, tmp_path):
    pytest.importorskip('requests')
    module = importlib.import_module('acquire_official_buildings')
    archive = tmp_path / 'outside.zip'; archive.write_bytes(b'existing source')
    with pytest.raises(ValueError, match='archive must be inside'):
        module.main(['--data-root', str(tmp_path / 'data'), '--archive', str(archive)])
    assert archive.read_bytes() == b'existing source'


@pytest.mark.parametrize('fail_by_cap', [False, True])
def test_failed_download_cleans_only_its_own_partial(common, monkeypatch, tmp_path, fail_by_cap):
    pytest.importorskip('requests')
    module = importlib.import_module('acquire_official_buildings')
    root = tmp_path / 'data'; root.mkdir()
    unrelated = root / 'unrelated.part'; unrelated.write_bytes(b'keep')
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def raise_for_status(self): pass
        def iter_content(self, chunk_size):
            yield b'first bytes written'
            if fail_by_cap:
                yield b'x' * (1024**2)
            else:
                raise OSError('simulated interrupted download')
    monkeypatch.setattr(module.requests, 'post', lambda *a, **k: Response())
    error = RuntimeError if fail_by_cap else OSError
    with pytest.raises(error, match='download'):
        module.main(['--data-root', str(root), '--max-download-mib', '1'])
    assert unrelated.read_bytes() == b'keep'
    assert list(root.rglob('*.part')) == [unrelated]
    assert not list(root.rglob('*.zip'))
    assert not list(root.rglob('*.gpkg'))


def test_owned_gpkg_cleanup_preserves_unrelated_and_previous_files(common, tmp_path):
    temp = tmp_path / 'output.partial.gpkg'
    unrelated = tmp_path / 'keep.gpkg-journal'; unrelated.write_bytes(b'keep')
    with pytest.raises(RuntimeError, match='simulated'):
        with common.owned_temporary(temp, gpkg=True):
            temp.write_bytes(b'partial')
            for suffix in ('-journal', '-wal', '-shm'):
                Path(str(temp) + suffix).write_bytes(b'partial')
            raise RuntimeError('simulated native write failure')
    assert list(tmp_path.iterdir()) == [unrelated]
    temp.write_bytes(b'previous work')
    with pytest.raises(FileExistsError, match='preserved'):
        with common.owned_temporary(temp, gpkg=True):
            pytest.fail('Must not own a pre-existing temporary')
    assert temp.read_bytes() == b'previous work'
