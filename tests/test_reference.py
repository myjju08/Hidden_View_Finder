"""Analytical regression cases for the independent closed-column LOS model."""

import numpy as np
import pytest

from seoul_visibility.reference import reference_los, traversed_cells


def grid(rows=7, cols=7, resolution=1.0):
    return np.zeros((rows, cols), dtype=np.float32), (0, resolution, 0, rows * resolution, 0, -resolution)


def xyz(transform, row, col, z):
    return (transform[0] + (col + 0.5) * transform[1], transform[3] + (row + 0.5) * transform[5], z)


def test_flat_and_different_endpoint_heights():
    surface, transform = grid()
    a, b = xyz(transform, 3, 0, 1.7), xyz(transform, 3, 6, 20)
    assert reference_los(surface, transform, a, b, 0) is True
    assert reference_los(surface, transform, b, a, 0) is True


@pytest.mark.parametrize("wall_height, visible", [(0.0, True), (8.0, False), (1.7, True)])
def test_ridge_or_wall_known_shadow(wall_height, visible):
    surface, transform = grid()
    surface[:, 3] = wall_height
    assert reference_los(surface, transform, xyz(transform, 3, 0, 1.7), xyz(transform, 3, 6, 1.7), 0) is visible


def test_cell_interval_catches_blocker_that_center_only_sampling_misses():
    surface, transform = grid()
    surface[3, 3] = 3
    # The ray has z=3 at the obstacle center, but drops below the top before
    # leaving that cell.  Contact at a single center is insufficient.
    assert reference_los(surface, transform, xyz(transform, 3, 0, 4), xyz(transform, 3, 6, 2), 0) is False


def test_surface_contact_and_explicit_vertical_tolerance():
    surface, transform = grid()
    surface[:, 3] = 3
    a, b = xyz(transform, 3, 0, 3), xyz(transform, 3, 6, 3)
    assert reference_los(surface, transform, a, b, 0) is True
    surface[:, 3] = 3 + 0.5e-6
    assert reference_los(surface, transform, a, b, 0) is True
    surface[:, 3] = 3 + 2e-6
    assert reference_los(surface, transform, a, b, 0) is False


def test_target_roof_and_adjacent_building_cells_retained():
    surface, transform = grid()
    surface[3, :2] = 10
    a, b = xyz(transform, 3, 0, 10), xyz(transform, 3, 6, 10)
    assert reference_los(surface, transform, a, b, 0) is True
    assert reference_los(surface, transform, a, xyz(transform, 3, 6, 1.7), 0) is False
    assert reference_los(surface, transform, xyz(transform, 3, 0, 14), xyz(transform, 3, 6, 1.7), 0) is True
    assert reference_los(surface, transform, xyz(transform, 3, 0, 9), b, 0) is False


def test_eye_height_is_only_an_endpoint_offset():
    surface, transform = grid()
    surface[3, 3] = 1
    a, b = xyz(transform, 3, 0, 1.7), xyz(transform, 3, 6, 1.7)
    assert reference_los(surface, transform, a, b, 0) is True
    # Demonstrates the semantic error if eye height is added to obstacles.
    assert reference_los(surface + 1.7, transform, a, b, 0) is False


def test_corner_supercover_and_thin_blocker():
    surface, transform = grid(5, 5)
    a, b = xyz(transform, 0, 0, 2), xyz(transform, 4, 4, 2)
    surface[1, 2] = 4  # touches the ray at one grid vertex only
    spans = list(traversed_cells(surface.shape, transform, a[:2], b[:2]))
    assert (1, 2, 0.375, 0.375) in spans
    assert reference_los(surface, transform, a, b, 0) is False
    surface[1, 2] = 0
    surface[2, 2] = 4
    assert reference_los(surface, transform, a, b, 0) is False


def test_ray_along_cell_boundary_checks_both_sides():
    surface, transform = grid(5, 5)
    surface[1, 2] = 4
    assert reference_los(surface, transform, (0.5, 3.0, 2), (4.5, 3.0, 2), 0) is False


def test_subcell_positions_and_nonzero_window_offset():
    surface, _ = grid(5, 5)
    transform = (200000, 5, 0, 550000, 0, -5)
    surface[2, 2] = 10
    assert reference_los(surface, transform, (200001, 549987.5, 11), (200024, 549987.5, 11), 0) is True
    assert reference_los(surface, transform, (200001, 549987.5, 9), (200024, 549987.5, 9), 0) is False


def test_unknown_including_corner_wins_over_known_block():
    surface, transform = grid(5, 5)
    a, b = xyz(transform, 0, 0, 2), xyz(transform, 4, 4, 2)
    surface[1, 1] = 4
    surface[1, 2] = np.nan
    assert reference_los(surface, transform, a, b, 0) is None
    masked = np.ma.array(np.zeros((5, 5)), mask=np.eye(5, dtype=bool))
    assert reference_los(masked, transform, a, b, 0) is None


def test_curvature_and_interior_quadratic_minimum():
    # One huge cell makes the minimum occur in its interior.  An endpoint-only
    # check of the column would incorrectly classify this as visible.
    surface, transform = grid(1, 1, 120000)
    a, b = (10000, 60000, 170), (110000, 60000, 170)
    assert reference_los(surface, transform, a, b, 0) is True
    assert reference_los(surface, transform, a, b, 6 / 7) is True
    assert reference_los(surface, transform, a, b, 1) is False
    assert reference_los(surface, transform, b, a, 1) is False


def test_physical_reciprocity_deterministically():
    rng = np.random.default_rng(20260907)
    surface, transform = grid(15, 19, 200)
    surface[:] = rng.uniform(0, 20, surface.shape)
    for _ in range(100):
        a = (float(rng.uniform(0.1, 3799.9)), float(rng.uniform(0.1, 2999.9)), float(rng.uniform(20, 40)))
        b = (float(rng.uniform(0.1, 3799.9)), float(rng.uniform(0.1, 2999.9)), float(rng.uniform(20, 40)))
        for k in (0, 6 / 7, 1):
            assert reference_los(surface, transform, a, b, k) == reference_los(surface, transform, b, a, k)


@pytest.mark.parametrize("endpoint", [(7, 3, 1), (-0.1, 3, 1), (1, 7.1, 1), (1, 2, float("nan"))])
def test_invalid_or_outside_endpoint(endpoint):
    surface, transform = grid()
    with pytest.raises(ValueError):
        reference_los(surface, transform, (1.5, 3.5, 1.7), endpoint)


@pytest.mark.parametrize("kwargs", [{"curvature_coefficient": -1}, {"curvature_coefficient": float("nan")}, {"earth_diameter_m": 0}, {"tolerance_m": -1}])
def test_invalid_parameters(kwargs):
    surface, transform = grid()
    with pytest.raises(ValueError):
        reference_los(surface, transform, (1.5, 3.5, 1.7), (5.5, 3.5, 1.7), **kwargs)


def test_vertical_segment_and_rotated_affine():
    surface, transform = grid()
    assert reference_los(surface, transform, (1.5, 3.5, 1.7), (1.5, 3.5, 10), 0) is True
    rotated = (0, 0, 1, 0, 1, 0)
    assert reference_los(surface, rotated, (1.5, 3.5, 1.7), (1.5, 5.5, 1.7), 0) is True


@pytest.mark.parametrize("wall_height", [0, 1, 30])
def test_native_backend_unambiguous_flat_and_wall_regressions(wall_height):
    from seoul_visibility.backend import viewshed

    surface, transform = grid(31, 31, 5)
    surface[:, 15] = wall_height
    source = xyz(transform, 15, 6, 1.7)
    result, output_transform = viewshed(surface, transform, "EPSG:5186", source[:2], 1.7, 1.7, 300, 0)
    assert result.shape == surface.shape
    assert tuple(output_transform) == tuple(transform)
    assert (result[:, :15] == 1).all()
    assert (result[:, 16:] == (0 if wall_height == 30 else 1)).all()


def test_installed_source_position_is_quantized_within_cell():
    from seoul_visibility.backend import viewshed

    rng = np.random.default_rng(20260907)
    surface, transform = grid(31, 31, 5)
    surface[:] = np.where(rng.random(surface.shape) < 0.2, rng.uniform(2, 35, surface.shape), 0)
    arrays = []
    for offset in (0.01, 0.5, 0.99):
        point = ((15 + offset) * 5, (31 - 15 - offset) * 5)
        arrays.append(viewshed(surface, transform, "EPSG:5186", point, 20, 1.7, 300, 0)[0])
    assert all(np.array_equal(arrays[0], array) for array in arrays[1:])


@pytest.mark.parametrize("curvature,visible", [(0, True), (6 / 7, True), (1, False)])
def test_native_curvature_analytical_horizon(curvature, visible):
    from seoul_visibility.backend import viewshed

    surface, transform = grid(101, 101, 200)
    start, end = xyz(transform, 50, 50, 1.7), xyz(transform, 50, 100, 1.7)
    result, _ = viewshed(surface, transform, "EPSG:5186", start[:2], 1.7, 1.7, 21000, curvature)
    assert bool(result[50, 100] == 1) is visible
    assert reference_los(surface, transform, start, end, curvature) is visible
