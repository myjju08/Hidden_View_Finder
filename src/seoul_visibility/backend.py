"""In-process GDAL adapter; the engine validates the required dependency region."""
from __future__ import annotations

import threading
import numpy as np
from osgeo import gdal, osr

gdal.UseExceptions()
osr.UseExceptions()
NATIVE_LOCK = threading.RLock()
MODE = gdal.GVM_Edge
VERSION = gdal.VersionInfo('RELEASE_NAME')
BACKEND_ID = f'GDAL/{VERSION}/GVM_Edge/GVOT_NORMAL/pixel-center-v2'
# GDAL's shared native block cache is globally bounded, with one native job.
gdal.SetCacheMax(64 * 1024**2)


def radial_coverage_supported(transform: tuple[float, ...]) -> bool:
    """Limit this dependency proof to the inspected implementation and grid.

    GDAL 3.8.4 Edge reads only predecessors with no larger absolute column or
    row offsets, and checks radius before updating the horizon. Consequently
    outside-disk heights cannot reach an inside-disk output. See the source
    proof and finite-padding perturbation measurements in
    reports/gdal-radial-dependencies/probe.md. This is a dependency statement,
    not a physical visibility bound. Unreviewed versions use full rectangles.
    """
    return (VERSION == '3.8.4' and MODE == gdal.GVM_Edge and
            transform[2] == 0 and transform[4] == 0 and
            transform[1] > 0 and transform[5] == -transform[1])


def viewshed(surface: np.ndarray, transform: tuple[float, ...], crs: str,
             target_xy: tuple[float, float], source_height_above_surface: float,
             eye_height_m: float, radius_m: float, curvature_coefficient: float
             ) -> tuple[np.ndarray, tuple[float, ...]]:
    """Surface stays alive for the entire ViewshedGenerate call.

    Input is S; reversed observerHeight is z_target-S(source), targetHeight is
    human eye height. This only has correct endpoint semantics on open ground.
    Values: 0 blocked, 1 visible, 2 outside radius, 3 unknown.
    """
    if not np.isfinite(radius_m) or radius_m <= 0:
        raise ValueError('A finite positive maximum distance is mandatory')
    if not np.isfinite(surface).all():
        raise ValueError('GDAL adapter requires a finite, coverage-validated surface')
    with NATIVE_LOCK:
        source = gdal.GetDriverByName('MEM').Create('', surface.shape[1], surface.shape[0], 1, gdal.GDT_Float32)
        source.SetGeoTransform(transform)
        srs = osr.SpatialReference(); srs.SetFromUserInput(crs)
        source.SetProjection(srs.ExportToWkt())
        source.GetRasterBand(1).WriteArray(surface)
        output = gdal.ViewshedGenerate(source.GetRasterBand(1), 'MEM', '', [],
            float(target_xy[0]), float(target_xy[1]), float(source_height_above_surface),
            float(eye_height_m), 1, 0, 2, 3, float(curvature_coefficient), MODE,
            float(radius_m), heightMode=gdal.GVOT_NORMAL, options=[])
        if output is None:
            raise RuntimeError('GDAL ViewshedGenerate returned no dataset')
        result = output.ReadAsArray().astype(np.uint8, copy=False)
        result_transform = output.GetGeoTransform()
        output = None
        source = None
    return result, result_transform
