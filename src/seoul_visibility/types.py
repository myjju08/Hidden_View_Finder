from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Literal
import json
import os
import tempfile
import numpy as np
from numpy.typing import NDArray

from .resources import StoragePolicy

class State(IntEnum):
    BLOCKED = 0
    VISIBLE = 1
    EXCLUDED = 2
    UNKNOWN = 3

@dataclass(frozen=True)
class TargetPoint:
    lon: float
    lat: float
    height_m: float
    height_reference: Literal["agl", "absolute"]
    vertical_reference: str | None = None

@dataclass(frozen=True)
class CandidateMask:
    """Boolean mask aligned to the entire prepared grid; CRS must match."""
    values: NDArray[np.bool_]
    transform: tuple[float, ...]
    crs: str

@dataclass(frozen=True)
class VisibilityResult:
    states: NDArray[np.uint8]
    crs: str
    transform: tuple[float, ...]  # GDAL order, pixel-corner origin
    bounds: tuple[float, float, float, float]
    resolution_m: float
    metadata: dict
    timings: dict[str, float]

    def sample_coordinates(self, state: State = State.VISIBLE, max_points: int = 1000,
                           seed: int = 0, wgs84: bool = True) -> NDArray[np.float64]:
        """Sample at most max_points cell centers; this does not polygonize."""
        from pyproj import Transformer
        if max_points < 0:
            raise ValueError("max_points must be nonnegative")
        indices = np.flatnonzero(self.states.ravel() == state)
        if len(indices) > max_points:
            indices = np.random.default_rng(seed).choice(indices, max_points, replace=False)
        rows, cols = np.divmod(indices, self.states.shape[1])
        x = self.transform[0] + (cols + .5) * self.transform[1]
        y = self.transform[3] + (rows + .5) * self.transform[5]
        if wgs84:
            x, y = Transformer.from_crs(self.crs, 'EPSG:4326', always_xy=True).transform(x, y)
        return np.column_stack([x, y]).astype(np.float64)

    def export_geotiff(self, path: str | Path, *,
                       project_data_root: str | Path | None = None,
                       policy: StoragePolicy | None = None) -> Path:
        """Export lossless tiled bytes without overwriting any existing path.

        States 3 and 2 mean unknown/NoData and excluded respectively.  The
        prepared project's recorded data root and storage limits apply to the
        export, including source bytes outside that root.  A destination must
        be inside the selected root; callers can explicitly select another
        project root/policy.  Hand-constructed results without storage metadata
        fall back to the destination directory and default limits, so callers
        should supply their full project root for complete budget accounting.
        """
        from osgeo import gdal, osr
        from .resources import preflight
        path = Path(path)
        if path.exists() or path.is_symlink():
            raise FileExistsError(f'Export destination already exists: {path}; choose a new path')
        storage = self.metadata.get('storage', {})
        budget_root = Path(project_data_root if project_data_root is not None
                           else storage.get('data_root', path.parent)).resolve()
        selected_policy = policy if policy is not None else StoragePolicy(**storage.get('policy', {}))
        if not isinstance(selected_policy, StoragePolicy):
            raise TypeError('policy must be a StoragePolicy instance')
        if not path.resolve().is_relative_to(budget_root):
            raise ValueError(f'Export destination must be within project data root {budget_root}; '
                             'supply project_data_root explicitly to use another project root')
        metadata_json = json.dumps(self.metadata, sort_keys=True)
        temporary_bytes = self.states.nbytes * 2 + len(metadata_json.encode('utf-8')) + 1024**2
        path.parent.mkdir(parents=True, exist_ok=True)
        preflight(budget_root, additional_bytes=storage.get('external_source_bytes', 0),
                  temporary_bytes=temporary_bytes, policy=selected_policy)
        fd, temp = tempfile.mkstemp(prefix='.visibility-export-', suffix='.tif', dir=path.parent)
        os.close(fd)
        ds = None
        try:
            ds = gdal.GetDriverByName('GTiff').Create(temp, self.states.shape[1], self.states.shape[0], 1, gdal.GDT_Byte,
                options=['TILED=YES', 'COMPRESS=DEFLATE', 'NUM_THREADS=1'])
            ds.SetGeoTransform(self.transform)
            srs = osr.SpatialReference(); srs.SetFromUserInput(self.crs)
            ds.SetProjection(srs.ExportToWkt())
            ds.GetRasterBand(1).WriteArray(self.states)
            ds.GetRasterBand(1).SetNoDataValue(int(State.UNKNOWN))
            ds.SetMetadataItem('STATES', '0=blocked,1=visible,2=excluded,3=unknown')
            ds.SetMetadataItem('VISIBILITY_METADATA', metadata_json)
            ds.FlushCache(); ds = None
            # Atomic no-clobber publication, including a destination created
            # concurrently. Never overwrite prepared or unrelated source files.
            os.link(temp, path)
        finally:
            ds = None
            Path(temp).unlink(missing_ok=True)
        return path

@dataclass(frozen=True)
class SparseVisibilityResult:
    states: NDArray[np.uint8]
    reasons: tuple[str | None, ...]
    metadata: dict
