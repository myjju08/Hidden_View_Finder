"""Independent review regressions for cache, export and endpoint edge cases."""

import math
import os
from dataclasses import asdict, replace

import numpy as np
import pytest

from seoul_visibility import IncompleteCoverageError, State, VisibilityEngine
from seoul_visibility.resources import ResourceBudgetError, StoragePolicy, tree_bytes
from test_engine import product, target_at


@pytest.mark.parametrize("name", ["dtm.tif", "manifest.json", "source.shp"])
def test_export_never_clobbers_existing_source_or_manifest(tmp_path, name):
    path = product(tmp_path)
    destination = tmp_path / name
    if not destination.exists():
        destination.write_bytes(b"original source data must survive")
    before = destination.read_bytes()
    with VisibilityEngine(path) as engine:
        result = engine.visible_from_target(target_at(engine), 50)
        with pytest.raises(FileExistsError):
            result.export_geotiff(destination)
        assert destination.read_bytes() == before
        assert not list(tmp_path.glob(".visibility-export-*"))


def test_export_does_not_replace_dangling_symlink(tmp_path):
    path = product(tmp_path)
    destination = tmp_path / "existing-link.tif"
    destination.symlink_to(tmp_path / "not-yet-mounted-source.tif")
    with VisibilityEngine(path) as engine:
        result = engine.visible_from_target(target_at(engine), 50)
        with pytest.raises(FileExistsError):
            result.export_geotiff(destination)
    assert destination.is_symlink()
    assert not list(tmp_path.glob(".visibility-export-*"))


def test_export_publication_race_preserves_other_writer_and_cleans_temp(tmp_path, monkeypatch):
    path = product(tmp_path)
    destination = tmp_path / "racing-output.tif"
    original_link = os.link

    def other_writer_wins(source, target, *args, **kwargs):
        destination.write_bytes(b"another writer's data")
        return original_link(source, target, *args, **kwargs)

    monkeypatch.setattr(os, "link", other_writer_wins)
    with VisibilityEngine(path) as engine:
        result = engine.visible_from_target(target_at(engine), 50)
        with pytest.raises(FileExistsError):
            result.export_geotiff(destination)
    assert destination.read_bytes() == b"another writer's data"
    assert not list(tmp_path.glob(".visibility-export-*"))


def test_returned_array_cannot_corrupt_cached_raster(tmp_path):
    with VisibilityEngine(product(tmp_path)) as engine:
        target = target_at(engine)
        result = engine.visible_from_target(target, 50)
        expected = result.states.copy()
        try:
            result.states.setflags(write=True)
        except ValueError:
            pass  # immutable backing is preferred over returning a copy
        else:
            result.states[:] = State.UNKNOWN
        cached = engine.visible_from_target(target, 50)
        assert cached.metadata["cache_status"] == "memory_hit"
        np.testing.assert_array_equal(cached.states, expected)


@pytest.mark.parametrize("radius, expected", [
    (0.01, 1), (5 - 1e-5, 1), (5, 5), (5 + 1e-5, 5),
    (math.sqrt(50) - 1e-5, 5), (math.sqrt(50), 9),
])
def test_small_and_fractional_radius_pixel_center_boundaries(tmp_path, radius, expected):
    with VisibilityEngine(product(tmp_path)) as engine:
        result = engine.visible_from_target(target_at(engine, dx=1.1, dy=-0.7), radius, curvature_coefficient=0)
        assert np.count_nonzero(result.states == State.VISIBLE) == expected
        assert not np.any(result.states == State.BLOCKED)


def test_unknown_in_computational_halo_rejected_even_outside_output_radius(tmp_path):
    quality = np.full((81, 81), 3, np.uint8)
    # 55 m is outside the 50 m output radius but inside its required one-cell
    # radial halo. The formerly tested square corner is now proven unused.
    quality[29, 40] = 0
    with VisibilityEngine(product(tmp_path, quality=quality)) as engine:
        with pytest.raises(IncompleteCoverageError):
            engine.visible_from_target(target_at(engine), 50)


def test_sparse_unknown_off_ray_does_not_poison_valid_ray(tmp_path):
    quality = np.full((81, 81), 3, np.uint8)
    quality[41, 45] = 0  # inside the rectangular read; below the horizontal ray
    with VisibilityEngine(product(tmp_path, quality=quality)) as engine:
        target = target_at(engine)
        observer = target_at(engine, col=50)
        result = engine.check_observers(target, np.array([[observer.lon, observer.lat]]), curvature_coefficient=0)
        assert result.states.tolist() == [State.VISIBLE]
        assert result.reasons == (None,)


def test_sparse_unknown_corner_touch_is_unknown(tmp_path):
    quality = np.full((81, 81), 3, np.uint8)
    quality[41, 42] = 0  # a corner touched by the row=column diagonal
    with VisibilityEngine(product(tmp_path, quality=quality)) as engine:
        target = target_at(engine)
        observer = target_at(engine, row=44, col=44)
        result = engine.check_observers(target, np.array([[observer.lon, observer.lat]]), curvature_coefficient=0)
        assert result.states.tolist() == [State.UNKNOWN]
        assert result.reasons == ("ray touches unknown coverage/height",)


def test_export_budget_includes_sibling_data_and_recorded_policy(tmp_path):
    data = tmp_path / 'data'
    data.mkdir()
    path = product(data)
    raw = data / 'raw-input.shp'
    raw.write_bytes(bytes(2 * 1024**2))
    with VisibilityEngine(path) as engine:
        result = engine.visible_from_target(target_at(engine), 50)
    cap = tree_bytes(data) + 100  # insufficient for an export with its temporary data
    policy = StoragePolicy(total_budget_bytes=cap, minimum_free_bytes=0)
    metadata = dict(result.metadata)
    metadata['storage'] = {'data_root': str(data), 'policy': asdict(policy), 'external_source_bytes': 0}
    result = replace(result, metadata=metadata)
    with pytest.raises(ResourceBudgetError, match='Project peak'):
        result.export_geotiff(data / 'new-export-directory' / 'result.tif')
    assert raw.stat().st_size == 2 * 1024**2
    assert not list(data.rglob('.visibility-export-*'))


def test_export_outside_recorded_root_requires_explicit_override(tmp_path):
    data = tmp_path / 'data'
    data.mkdir()
    with VisibilityEngine(product(data)) as engine:
        result = engine.visible_from_target(target_at(engine), 50)
    metadata = dict(result.metadata)
    metadata['storage'] = {'data_root': str(data), 'policy': asdict(StoragePolicy()), 'external_source_bytes': 0}
    result = replace(result, metadata=metadata)
    destination = tmp_path / 'other-project' / 'result.tif'
    with pytest.raises(ValueError, match='within project data root'):
        result.export_geotiff(destination)
    assert not destination.exists()
    assert result.export_geotiff(destination, project_data_root=tmp_path) == destination


def test_export_counts_recorded_external_source_bytes(tmp_path):
    with VisibilityEngine(product(tmp_path)) as engine:
        result = engine.visible_from_target(target_at(engine), 50)
    policy = StoragePolicy(total_budget_bytes=4 * 1024**2, minimum_free_bytes=0)
    metadata = dict(result.metadata)
    metadata['storage'] = {'data_root': str(tmp_path), 'policy': asdict(policy), 'external_source_bytes': 4 * 1024**2}
    result = replace(result, metadata=metadata)
    with pytest.raises(ResourceBudgetError, match='Project peak'):
        result.export_geotiff(tmp_path / 'result.tif')
