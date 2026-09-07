"""Independent small-workload line of sight over closed raster cell columns.

This is deliberately not the dense backend.  Each raster sample is a constant
height column covering the *closed* pixel footprint.  A ray touching a tall
column at a cell corner is obstructed.  Terrain is therefore not interpolated
between pixel centers, unlike GDAL's viewshed approximation.  Endpoint columns
are retained, and exact surface contact is allowed within ``tolerance_m``.

Coordinates are projected metres.  The transform maps pixel *edges* to world
coordinates, in GDAL order, or is an object exposing ``to_gdal()``.  Grid-edge
classification uses a 1e-9 pixel tolerance for floating-point coordinate error.
No coordinate is snapped to a pixel center by this module.
"""

from __future__ import annotations

import math
from typing import Any, Iterator, Sequence

import numpy as np

EARTH_DIAMETER_M = 12_756_274.0
GRID_TOLERANCE = 1e-9


def _inverse_coordinates(
    transform: Any, x: float, y: float
) -> tuple[float, float]:
    """Invert a general affine GDAL geotransform without a GIS dependency."""
    values = transform.to_gdal() if hasattr(transform, "to_gdal") else transform
    if len(values) != 6:
        raise ValueError("transform must have six GDAL coefficients or to_gdal()")
    x0, a, b, y0, d, e = (float(v) for v in values)
    determinant = a * e - b * d
    if not all(math.isfinite(v) for v in (x0, a, b, y0, d, e)) or determinant == 0:
        raise ValueError("transform must be finite and invertible")
    dx, dy = x - x0, y - y0
    return (e * dx - b * dy) / determinant, (-d * dx + a * dy) / determinant


def _touching_indices(value: float, length: int) -> tuple[int, ...]:
    nearest = round(value)
    if abs(value - nearest) <= GRID_TOLERANCE:
        return tuple(i for i in (nearest - 1, nearest) if 0 <= i < length)
    index = math.floor(value)
    return (index,) if 0 <= index < length else ()


def traversed_cells(
    surface_shape: tuple[int, int],
    transform: Any,
    start_xy: Sequence[float],
    end_xy: Sequence[float],
) -> Iterator[tuple[int, int, float, float]]:
    """Yield (row, column, enter_t, exit_t), including singleton corner touches.

    The segment is ``start + t * (end-start)``, for ``0 <= t <= 1``.
    Endpoints must lie in the half-open raster extent in pixel coordinates.
    Complexity is proportional to the number of crossed grid lines, not the
    area of the segment's bounding rectangle.  This readable implementation
    uses Python dictionaries and is intended for reference and short batches.
    """
    rows, cols = surface_shape
    if rows <= 0 or cols <= 0:
        raise ValueError("surface must be a nonempty two-dimensional raster")
    points = [tuple(float(v) for v in p) for p in (start_xy, end_xy)]
    if any(len(p) != 2 or not all(math.isfinite(v) for v in p) for p in points):
        raise ValueError("endpoint coordinates must be finite XY pairs")
    c0, r0 = _inverse_coordinates(transform, *points[0])
    c1, r1 = _inverse_coordinates(transform, *points[1])
    if any(not (0 <= c < cols and 0 <= r < rows) for c, r in ((c0, r0), (c1, r1))):
        raise ValueError("line-of-sight endpoint lies outside the raster extent")

    cuts = {0.0, 1.0}
    for first, last in ((c0, c1), (r0, r1)):
        delta = last - first
        if delta:
            for boundary in range(math.ceil(min(first, last)), math.floor(max(first, last)) + 1):
                t = (boundary - first) / delta
                if 0 < t < 1:
                    cuts.add(t)
    ts = sorted(cuts)
    spans: dict[tuple[int, int], tuple[float, float]] = {}

    def include(at: float, enter: float, leave: float) -> None:
        c, r = c0 + at * (c1 - c0), r0 + at * (r1 - r0)
        for row in _touching_indices(r, rows):
            for col in _touching_indices(c, cols):
                key = row, col
                if key in spans:
                    old_enter, old_leave = spans[key]
                    spans[key] = min(old_enter, enter), max(old_leave, leave)
                else:
                    spans[key] = enter, leave

    for t in ts:
        include(t, t, t)
    for left, right in zip(ts[:-1], ts[1:]):
        include((left + right) / 2, left, right)
    for (row, col), (enter, leave) in sorted(spans.items(), key=lambda item: (item[1][0], item[0])):
        yield row, col, enter, leave


def reference_los(
    surface: np.ndarray,
    transform: Any,
    start_xyz: Sequence[float],
    end_xyz: Sequence[float],
    curvature_coefficient: float = 6 / 7,
    earth_diameter_m: float = EARTH_DIAMETER_M,
    tolerance_m: float = 1e-6,
) -> bool | None:
    """Return clear=True, obstructed=False, or unknown=None for one sightline.

    Unknown wins if *any* touched cell is nonfinite or masked, even if another
    cell is known to obstruct.  Numeric NoData sentinels must be converted to
    NaN by the caller.  Invalid arguments/outside endpoints raise ValueError.

    At segment fraction t, clearance above a cell surface is

      z_start + t*(z_end-z_start) - S - k*distance**2*t*(1-t)/diameter.

    This symmetric curvature/refraction correction preserves LOS reciprocity.
    ``diameter`` defaults to twice the EPSG:5186 ellipsoid semimajor axis.  The
    default k=6/7 is an assumed light-refraction correction, not a measurement.
    We minimize this quadratic on the *entire* traversed cell interval,
    including the interior minimum and zero-length corner touches.  A ray is
    blocked only when this clearance is below ``-tolerance_m``.
    """
    array = np.asanyarray(surface)
    if array.ndim != 2 or 0 in array.shape:
        raise ValueError("surface must be a nonempty two-dimensional raster")
    endpoints = [tuple(float(v) for v in p) for p in (start_xyz, end_xyz)]
    if any(len(p) != 3 or not all(math.isfinite(v) for v in p) for p in endpoints):
        raise ValueError("endpoints must be finite XYZ triples")
    if not math.isfinite(curvature_coefficient) or curvature_coefficient < 0:
        raise ValueError("curvature_coefficient must be finite and nonnegative")
    if not math.isfinite(earth_diameter_m) or earth_diameter_m <= 0:
        raise ValueError("earth_diameter_m must be finite and positive")
    if not math.isfinite(tolerance_m) or tolerance_m < 0:
        raise ValueError("tolerance_m must be finite and nonnegative")

    start, end = endpoints
    distance = math.hypot(end[0] - start[0], end[1] - start[1])
    q = curvature_coefficient * distance * distance / earth_diameter_m
    dz = end[2] - start[2]
    stationary = (q - dz) / (2 * q) if q else None
    blocked = False
    for row, col, enter, leave in traversed_cells(array.shape, transform, start[:2], end[:2]):
        elevation = array[row, col]
        if np.ma.is_masked(elevation) or not math.isfinite(float(elevation)):
            return None
        ts = [enter, leave]
        if stationary is not None and enter < stationary < leave:
            ts.append(stationary)
        minimum = min(start[2] + t * dz - float(elevation) - q * t * (1 - t) for t in ts)
        if minimum < -tolerance_m:
            blocked = True
    return not blocked
