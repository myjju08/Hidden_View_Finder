"""Reusable radius-windowed inverse viewsheds; no vector work in query paths."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import asdict, replace
from pathlib import Path
import copy
import hashlib
import json
import math
import threading
import time
import numpy as np
from osgeo import gdal
from pyproj import CRS, Transformer

from . import backend
from .errors import ConfigurationError, IncompleteCoverageError, UnsupportedTargetError
from .resources import StoragePolicy, memory_preflight
from .types import CandidateMask, SparseVisibilityResult, State, TargetPoint, VisibilityResult

TOLERANCE_M = 1e-6
LIMITATIONS = [
    'Point visibility only; raster-model screening, not exact footprint geometry.',
    'Observer output is open ground; public access is unverified.',
    'Pixel-center target quantization; projected planar distances in metres.',
    'GDAL GVM_Edge interpolates a raster horizon; it differs from closed column reference LOS.',
    '5 m data cannot resolve every alley or foreground obstacle; all_touched may close narrow gaps.',
    'No trees, balconies, signs, temporary objects, atmosphere, overhangs, or indoor viewpoints unless represented.',
    'Curvature/refraction coefficient is an assumed model setting, not a weather measurement.',
]

def _bounds(gt: tuple, width: int, height: int) -> tuple:
    return (gt[0], gt[3] + height * gt[5], gt[0] + width * gt[1], gt[3])

class VisibilityEngine:
    """Open prepared datasets once. One engine belongs to its creating thread.

    Result cache is a byte-bounded in-memory LRU. No disk result cache is written
    in this release (zero bytes, below the configured 1 GiB disk policy cap).
    Use separate processes/engines for concurrency; native jobs are serialized
    in-process, and the GDAL block cache is capped at 64 MiB.
    """
    def __init__(self, manifest_path: str | Path, *, memory_cache_bytes: int = 128 * 1024**2,
                 interactive_limit_m: float = 10_000):
        started = time.perf_counter()
        self.path = Path(manifest_path).resolve()
        self.manifest = json.loads(self.path.read_text())
        m = self.manifest
        if m.get('schema_version') != 1 or m.get('status') != 'ready':
            raise ConfigurationError('Manifest must be schema_version=1 and status=ready; run prepare to completion')
        if (not isinstance(m.get('vertical_reference'), str) or not m['vertical_reference'].strip()
                or m['vertical_reference'].strip().lower() in ('unknown', 'unspecified')):
            raise ConfigurationError('Prepared data requires a documented vertical_reference')
        self.crs = CRS.from_user_input(m['crs'])
        if not self.crs.is_projected or any(abs(a.unit_conversion_factor - 1) > 1e-12 for a in self.crs.axis_info):
            raise ConfigurationError('Prepared CRS must be projected with metre horizontal units')
        self.to_xy = Transformer.from_crs('EPSG:4326', self.crs, always_xy=True)
        self.to_lonlat = Transformer.from_crs(self.crs, 'EPSG:4326', always_xy=True)
        self.earth_diameter_m = 2 * self.crs.ellipsoid.semi_major_metre
        parameters = m.get('processing', {}).get('parameters', {})
        self.storage = m.get('storage', {
            'data_root': parameters.get('data_root', str(self.path.parent)),
            'policy': asdict(StoragePolicy(**parameters.get('storage_policy', {}))),
            'external_source_bytes': m.get('processing', {}).get('storage_plan', {}).get('estimates', {}).get('external_source_bytes', 0)})
        self.storage = copy.deepcopy(self.storage)
        self.storage['data_root'] = str((self.path.parent / self.storage['data_root']).resolve())
        if memory_cache_bytes < 0 or not math.isfinite(interactive_limit_m) or interactive_limit_m <= 0:
            raise ConfigurationError('Cache byte cap must be nonnegative and interactive radius limit positive')
        self.memory_cache_bytes = int(memory_cache_bytes)
        self.interactive_limit_m = float(interactive_limit_m)
        self._cache: OrderedDict[str, VisibilityResult] = OrderedDict()
        self._cache_bytes = 0
        self._thread = threading.get_ident()
        self._closed = False
        self._products: dict[float, dict] = {}
        self._files = [self.path]
        for key, product in m['products'].items():
            p = dict(product)
            res = float(p['resolution_m'])
            if res <= 0 or not math.isfinite(res) or res in self._products:
                raise ConfigurationError('Product resolutions must be unique, positive and finite')
            gt = tuple(p['transform'])
            if len(gt) != 6 or not np.isfinite(gt).all() or gt[1] != res or gt[5] != -res or gt[2] != 0 or gt[4] != 0:
                raise ConfigurationError('Only aligned, square, north-up rasters are supported')
            if not np.allclose(_bounds(gt, p['width'], p['height']), p['bounds'], rtol=0, atol=1e-6):
                raise ConfigurationError('Manifest bounds disagree with its grid')
            p['transform'] = gt
            p['datasets'] = {}
            for name in ('dtm', 'surface', 'occupancy', 'quality', 'output_mask'):
                if name == 'output_mask' and not p.get(name):
                    continue
                path = (self.path.parent / p[name]).resolve()
                ds = gdal.OpenEx(str(path), gdal.OF_RASTER | gdal.OF_READONLY)
                if ds is None:
                    raise ConfigurationError(f'Cannot open prepared {name}: {path}')
                if ds.RasterCount != 1 or ds.RasterXSize != p['width'] or ds.RasterYSize != p['height'] or not np.allclose(ds.GetGeoTransform(), gt, rtol=0, atol=1e-8):
                    raise ConfigurationError(f'{name} is not aligned with the manifest grid')
                if not self.crs.equals(CRS.from_wkt(ds.GetProjection()), ignore_axis_order=True):
                    raise ConfigurationError(f'{name} CRS differs from manifest')
                expected = gdal.GDT_Float32 if name in ('dtm', 'surface') else gdal.GDT_Byte
                if ds.GetRasterBand(1).DataType != expected:
                    raise ConfigurationError(f'{name} must use Float32 elevations or Byte masks')
                p['datasets'][name] = ds
                self._files.append(path)
            self._products[res] = p
        if not self._products:
            raise ConfigurationError('No prepared resolution products')
        self._signature = self._file_signature()
        self._identity = hashlib.sha256(json.dumps([m, self._signature, backend.BACKEND_ID], sort_keys=True).encode()).hexdigest()
        self.open_seconds = time.perf_counter() - started

    @classmethod
    def from_manifest(cls, path: str | Path, **kwargs) -> 'VisibilityEngine':
        return cls(path, **kwargs)

    def _file_signature(self) -> list:
        return [(str(p), p.stat().st_size, p.stat().st_mtime_ns, p.stat().st_ctime_ns, p.stat().st_ino) for p in self._files]

    def _check(self) -> None:
        if self._closed:
            raise RuntimeError('Engine is closed')
        if threading.get_ident() != self._thread:
            raise RuntimeError('GDAL handles cannot be shared across threads; open a separate engine in a worker process')
        if self._file_signature() != self._signature:
            self._cache.clear(); self._cache_bytes = 0
            raise ConfigurationError('Prepared files/manifest changed while engine was open; reopen from the current manifest')

    def close(self) -> None:
        for p in self._products.values():
            p['datasets'].clear()
        self._cache.clear(); self._cache_bytes = 0
        self._closed = True

    def __enter__(self) -> 'VisibilityEngine':
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def _product(self, resolution_m: float) -> dict:
        if resolution_m not in self._products:
            raise ConfigurationError(f'Resolution {resolution_m!r} m is unavailable; independently prepare it. Available: {sorted(self._products)}')
        return self._products[resolution_m]

    @staticmethod
    def _validate_settings(eye_height_m: float, curvature_coefficient: float) -> None:
        if not math.isfinite(eye_height_m) or eye_height_m < 0:
            raise ValueError('eye_height_m must be finite and nonnegative')
        if not math.isfinite(curvature_coefficient) or not 0 <= curvature_coefficient <= 1:
            raise ValueError('curvature_coefficient must be finite in [0,1] (0 flat, 1 no refraction)')

    @staticmethod
    def _read(p: dict, name: str, window: tuple[int, int, int, int]) -> np.ndarray:
        ds = p['datasets'][name]
        arr = ds.GetRasterBand(1).ReadAsArray(*window)
        if arr is None:
            raise IncompleteCoverageError(f'Failed to read {name} window')
        if name in ('dtm', 'surface'):
            nodata = ds.GetRasterBand(1).GetNoDataValue()
            if nodata is not None and np.isfinite(nodata):
                arr[arr == nodata] = np.nan
        else:
            nodata = ds.GetRasterBand(1).GetNoDataValue()
            if nodata is not None:
                # Unknown occupancy/quality must fail coverage validation. An
                # unknown output boundary is ineligible, never assumed inside.
                arr[arr == nodata] = 255 if name == 'occupancy' else 0
        return arr

    def _target(self, target: TargetPoint, p: dict) -> dict:
        if not all(math.isfinite(v) for v in (target.lon, target.lat, target.height_m)):
            raise UnsupportedTargetError('Target longitude, latitude and height must be finite')
        if not -180 <= target.lon <= 180 or not -90 <= target.lat <= 90:
            raise UnsupportedTargetError('Public coordinates are WGS84 longitude, latitude in that order')
        if target.height_reference not in ('agl', 'absolute'):
            raise UnsupportedTargetError('Specify height_reference="agl" or "absolute"')
        if target.height_reference == 'agl' and target.height_m < 0:
            raise UnsupportedTargetError('AGL height must be nonnegative and is above bare earth, not a roof')
        if target.height_reference == 'absolute' and target.vertical_reference != self.manifest['vertical_reference']:
            raise UnsupportedTargetError('Absolute heights require TargetPoint.vertical_reference exactly matching manifest vertical_reference; transform incompatible GPS/ellipsoidal heights separately')
        x, y = self.to_xy.transform(target.lon, target.lat)
        gt = p['transform']; res = p['resolution_m']
        if not np.isfinite([x, y]).all():
            raise UnsupportedTargetError('Target cannot be transformed to the prepared CRS')
        col, row = math.floor((x - gt[0]) / res), math.floor((gt[3] - y) / res)
        if not (0 <= col < p['width'] and 0 <= row < p['height']):
            raise UnsupportedTargetError('Target is outside prepared raster coverage')
        win = (col, row, 1, 1)
        dtm = float(self._read(p, 'dtm', win)[0, 0])
        surface = float(self._read(p, 'surface', win)[0, 0])
        quality = int(self._read(p, 'quality', win)[0, 0])
        if not np.isfinite([dtm, surface]).all() or quality & 3 != 3 or quality & 248:
            raise IncompleteCoverageError('Target cell has unknown terrain/building coverage or unresolved roof height')
        z = target.height_m + dtm if target.height_reference == 'agl' else target.height_m
        if not math.isfinite(z) or z < surface - TOLERANCE_M:
            raise UnsupportedTargetError(f'Target elevation {z:g} m is below modeled obstruction {surface:g} m; a below-roof facade/indoor point is unsupported in 2.5D')
        effective = (gt[0] + (col + .5) * res, gt[3] - (row + .5) * res)
        lon, lat = self.to_lonlat.transform(*effective)
        return {'requested': {**asdict(target), 'x': x, 'y': y},
                'effective': {'x': effective[0], 'y': effective[1], 'lon': lon, 'lat': lat, 'row': row, 'col': col},
                'absolute_elevation_m': z, 'source_surface_m': surface, 'source_dtm_m': dtm,
                'vertical_reference': self.manifest['vertical_reference'],
                'quantization': 'containing half-open cell, then cell center; no subpixel dense precision'}

    def visible_from_target(self, target: TargetPoint, radius_m: float = 5_000,
                            eye_height_m: float = 1.7, resolution_m: float = 5,
                            curvature_coefficient: float = 6/7,
                            candidate_mask: CandidateMask | np.ndarray | None = None,
                            use_cache: bool = True, *, offline: bool = False,
                            quality_policy: str = 'strict') -> VisibilityResult:
        started = time.perf_counter()
        self._check()
        self._validate_settings(eye_height_m, curvature_coefficient)
        if not math.isfinite(radius_m) or radius_m <= 0:
            raise ValueError('radius_m must be finite and strictly positive; unlimited GDAL ranges are disabled')
        if radius_m > self.interactive_limit_m and not offline:
            raise ValueError(f'Radius exceeds interactive limit {self.interactive_limit_m:g} m; offline=True requires RAM preflight')
        if quality_policy != 'strict':
            raise ConfigurationError('Only strict coverage policy is implemented; unresolved inputs cannot be imputed at query time')
        p = self._product(resolution_m)
        radial_coverage = backend.radial_coverage_supported(p['transform'])
        coverage_strategy = 'radius_plus_one_cell_v1' if radial_coverage else 'full_rectangle_v1'
        target_info = self._target(target, p)
        eff = target_info['effective']; col, row = eff['col'], eff['row']
        half = math.ceil(radius_m / resolution_m) + 1  # interpolation support halo
        xoff, yoff, size = col - half, row - half, 2 * half + 1
        if xoff < 0 or yoff < 0 or xoff + size > p['width'] or yoff + size > p['height']:
            raise IncompleteCoverageError('Full radius square plus one-cell interpolation halo exceeds coverage; prepare surrounding data or reduce radius')
        resources = memory_preflight(size * size * 48 + self._cache_bytes + 64 * 1024**2)
        candidate = None
        digest = None
        if candidate_mask is not None:
            if isinstance(candidate_mask, CandidateMask):
                if tuple(candidate_mask.transform) != p['transform'] or not self.crs.equals(CRS.from_user_input(candidate_mask.crs), ignore_axis_order=True):
                    raise ConfigurationError('Candidate mask transform and CRS must exactly match the prepared grid')
                candidate = candidate_mask.values
            else:
                candidate = candidate_mask
            if not isinstance(candidate, np.ndarray) or candidate.dtype != np.bool_ or candidate.shape != (p['height'], p['width']):
                raise ConfigurationError('Candidate mask must be boolean and match the entire prepared grid shape')
            # Hash only the relevant window; all other cells are outside this computation.
            candidate = np.ascontiguousarray(candidate[yoff:yoff+size, xoff:xoff+size])
            digest = hashlib.sha256(candidate.tobytes()).hexdigest()
        key = hashlib.sha256(json.dumps([self._identity, target_info, radius_m, eye_height_m,
            resolution_m, curvature_coefficient, digest, quality_policy, offline,
            p['transform'], backend.BACKEND_ID, coverage_strategy], sort_keys=True).encode()).hexdigest()
        lookup_done = time.perf_counter()
        if use_cache and key in self._cache:
            result = self._cache.pop(key); self._cache[key] = result
            metadata = copy.deepcopy(result.metadata)
            metadata['cache_status'] = 'memory_hit'
            return replace(result, metadata=metadata, timings={'cache_lookup_s': lookup_done-started, 'total_s': time.perf_counter()-started})

        win = (xoff, yoff, size, size)
        dtm = self._read(p, 'dtm', win)
        surface = self._read(p, 'surface', win)
        occupancy = self._read(p, 'occupancy', win)
        quality = self._read(p, 'quality', win)
        valid = np.isfinite(dtm) & np.isfinite(surface) & ((quality & 3) == 3) & ((quality & 248) == 0) & (occupancy <= 1)
        if radial_coverage:
            offsets = (np.arange(size, dtype=np.float64)-half)*resolution_m
            required = offsets[:, None]**2 + offsets[None, :]**2 <= (radius_m+resolution_m)**2 + 1e-9
        else:
            required = np.ones(valid.shape, dtype=bool)
        missing_required = int(np.count_nonzero(required & ~valid))
        if missing_required:
            raise IncompleteCoverageError(f'{missing_required:,} required computational-window cells have missing terrain/building coverage, unresolved height, or roof conflict; unknown occluders could hide other cells')
        open_ground = (occupancy == 0) & valid
        if np.any(required & (surface < dtm - TOLERANCE_M)) or np.any(required & open_ground & (np.abs(surface-dtm) > TOLERANCE_M)):
            raise ConfigurationError('Surface contract violated: S>=DTM and S=DTM for every unoccupied cell; vegetation/elevated endpoints require a different model')
        if np.any(required & (occupancy > 1)):
            raise ConfigurationError('Occupancy raster must contain only 0 and 1')
        unused_unknown = int(np.count_nonzero(~required & ~valid))
        if unused_unknown:
            # Finite MEM padding ONLY beyond the proven native dependency disk
            # and its conservative cell halo. It is not terrain imputation.
            # Source NoData/quality remain untouched, and these outputs are
            # excluded. Perturbations from -1e9 to +1e12 were measured invariant.
            surface[~required & ~valid] = np.float32(1e9)
        boundary = self._read(p, 'output_mask', win) if 'output_mask' in p['datasets'] else None
        read_done = time.perf_counter()
        gt = p['transform']
        local_gt = (gt[0]+xoff*resolution_m, resolution_m, 0., gt[3]-yoff*resolution_m, 0., -resolution_m)
        states, output_gt = backend.viewshed(surface, local_gt, self.crs.to_wkt(),
            (eff['x'], eff['y']), target_info['absolute_elevation_m']-target_info['source_surface_m'],
            eye_height_m, radius_m, curvature_coefficient)
        dense_done = time.perf_counter()
        dx_float = (output_gt[0]-local_gt[0])/resolution_m
        dy_float = (local_gt[3]-output_gt[3])/resolution_m
        dx, dy = round(dx_float), round(dy_float)
        if abs(dx-dx_float) > 1e-7 or abs(dy-dy_float) > 1e-7 or dx < 0 or dy < 0:
            raise RuntimeError('GDAL returned an unexpected output-grid alignment')
        h, w = states.shape
        if dx+w > size or dy+h > size:
            raise RuntimeError('GDAL output exceeds validated input window')
        sl = np.s_[dy:dy+h, dx:dx+w]
        # 1D coordinate vectors only; broadcasting creates a local distance mask.
        xs = output_gt[0] + (np.arange(w, dtype=np.float64)+.5)*resolution_m - eff['x']
        ys = output_gt[3] - (np.arange(h, dtype=np.float64)+.5)*resolution_m - eff['y']
        eligible = open_ground[sl] & ((ys[:, None]**2 + xs[None, :]**2) <= radius_m**2 + 1e-9)
        if boundary is not None:
            eligible &= boundary[sl] != 0
        if candidate is not None:
            eligible &= candidate[sl]
        states[~eligible] = State.EXCLUDED
        estimated = int(np.count_nonzero(((quality & 4) != 0) & required))
        metadata = {'target': target_info, 'eye_height_m': eye_height_m, 'radius_m': radius_m,
            'distance_origin': 'effective target cell center', 'curvature_coefficient': curvature_coefficient,
            'earth_diameter_m': self.earth_diameter_m, 'backend': backend.BACKEND_ID,
            'backend_version': backend.VERSION, 'backend_mode': 'GVM_Edge/GVOT_NORMAL',
            'source_data_version': self.manifest['data_version'], 'source_kind': self.manifest.get('source_kind','local'),
            'cache_status': 'miss' if use_cache else 'disabled', 'candidate_mask_sha256': digest,
            'quality_policy': quality_policy, 'quality': {'classification': 'APPROXIMATE' if estimated else 'RASTER_MODEL_SCREENING',
                'estimated_height_cells_in_computational_window': estimated, 'unknown_cells': 0},
            'coverage': {'strategy': coverage_strategy,
                'required_radius_m': radius_m+resolution_m if radial_coverage else None,
                'required_cells': int(np.count_nonzero(required)),
                'unknown_unused_padding_cells': unused_unknown,
                'padding_policy': '1e9 finite MEM padding only outside validated native dependencies; source NoData preserved',
                'dependency_evidence': 'GDAL v3.8.4 alg/viewshed.cpp plus reports/gdal-radial-dependencies/probe.json' if radial_coverage else 'unreviewed backend uses complete rectangular coverage'},
            'limitations': LIMITATIONS.copy() + self.manifest.get('limitations', []),
            'source_provenance': copy.deepcopy(self.manifest.get('provenance', {})),
            'output_boundary_applied': boundary is not None,
            'public_access': 'unverified; caller candidate mask supplied' if candidate is not None else 'unverified',
            'computational_window': list(win), 'numerical_tolerance_m_for_source_validation': TOLERANCE_M,
            'native_endpoint_contact': 'native GDAL comparison; reference allows contact within 1e-6 m',
            'resources': resources, 'storage': copy.deepcopy(self.storage)}
        # Immutable bytes backing prevents callers from re-enabling NumPy writes
        # and corrupting the cached array shared by future identical queries.
        states = np.frombuffer(states.tobytes(), dtype=np.uint8).reshape(states.shape)
        result = VisibilityResult(states, self.crs.to_string(), tuple(output_gt), _bounds(output_gt,w,h),
            float(resolution_m), metadata, {'cache_lookup_s': lookup_done-started,
                'read_s': read_done-lookup_done, 'dense_s': dense_done-read_done,
                'mask_s': time.perf_counter()-dense_done, 'total_s': time.perf_counter()-started})
        # Charge arrays and serialized metadata. Eviction never touches source data.
        charge = states.nbytes + len(json.dumps(metadata)) + 1024
        if use_cache and charge <= self.memory_cache_bytes:
            while self._cache and self._cache_bytes + charge > self.memory_cache_bytes:
                _, old = self._cache.popitem(last=False)
                self._cache_bytes -= old.states.nbytes + len(json.dumps(old.metadata)) + 1024
            self._cache[key] = replace(result, metadata=copy.deepcopy(metadata))
            self._cache_bytes += charge
        return result

    def check_observers(self, target: TargetPoint, observers_lonlat: np.ndarray,
                        eye_height_m: float = 1.7, resolution_m: float = 5,
                        curvature_coefficient: float = 6/7) -> SparseVisibilityResult:
        """Small batches, exact observer XY and dense-compatible snapped target.

        Independent Python column LOS is deliberately simple, not a dense engine.
        Invalid/occupied observer endpoints are excluded with reasons; unknown
        cells touched by a ray make that ray unknown. Caller order is preserved.
        """
        from .reference import reference_los
        started = time.perf_counter()
        self._check(); self._validate_settings(eye_height_m, curvature_coefficient)
        coords = np.asarray(observers_lonlat, dtype=np.float64)
        if coords.ndim != 2 or coords.shape[1] != 2 or len(coords) > 10_000:
            raise ValueError('observers_lonlat must have shape (N,2), N<=10,000; stream larger workloads')
        p = self._product(resolution_m); gt=p['transform']
        ti = self._target(target, p); eff=ti['effective']
        tx,ty,tz=eff['x'],eff['y'],ti['absolute_elevation_m']
        states=np.full(len(coords), State.EXCLUDED, dtype=np.uint8)
        reasons: list[str | None]=[]
        estimated_present = False
        for i,(lon,lat) in enumerate(coords):
            reason = None
            if not np.isfinite([lon,lat]).all() or not (-180<=lon<=180 and -90<=lat<=90):
                reasons.append('invalid longitude/latitude'); continue
            x,y=self.to_xy.transform(lon,lat)
            if not np.isfinite([x,y]).all():
                reasons.append('coordinate transform failed'); continue
            c,r=math.floor((x-gt[0])/resolution_m), math.floor((gt[3]-y)/resolution_m)
            if not (0<=c<p['width'] and 0<=r<p['height']):
                reasons.append('outside prepared coverage'); continue
            if math.hypot(x-tx,y-ty)>self.interactive_limit_m:
                reasons.append('beyond sparse interactive distance limit'); continue
            x0=max(0,min(c,eff['col'])-1); y0=max(0,min(r,eff['row'])-1)
            x1=min(p['width'],max(c,eff['col'])+2); y1=min(p['height'],max(r,eff['row'])+2)
            win=(x0,y0,x1-x0,y1-y0)
            memory_preflight((x1-x0)*(y1-y0)*32+64*1024**2)
            dtm=self._read(p,'dtm',win); s=self._read(p,'surface',win)
            q=self._read(p,'quality',win); occ=self._read(p,'occupancy',win)
            estimated_present |= bool(np.any(q & 4))
            rr,cc=r-y0,c-x0
            bad=~np.isfinite(dtm)|~np.isfinite(s)|((q&3)!=3)|((q&248)!=0)|(occ>1)
            if bad[rr,cc]:
                states[i]=State.UNKNOWN; reasons.append('unknown observer cell'); continue
            if occ[rr,cc]:
                reasons.append('building-occupied observer cell'); continue
            if 'output_mask' in p['datasets'] and self._read(p,'output_mask',(c,r,1,1))[0,0]==0:
                reasons.append('outside output boundary'); continue
            if np.any((occ==0)&~bad&(np.abs(s-dtm)>TOLERANCE_M)) or np.any(~bad&(s<dtm-TOLERANCE_M)):
                raise ConfigurationError('Surface contract violated: unoccupied S must equal DTM and S>=DTM')
            s[bad]=np.nan
            local=(gt[0]+x0*resolution_m,resolution_m,0.,gt[3]-y0*resolution_m,0.,-resolution_m)
            visible=reference_los(s,local,(tx,ty,tz),(x,y,float(dtm[rr,cc])+eye_height_m),
                curvature_coefficient=curvature_coefficient,earth_diameter_m=self.earth_diameter_m)
            states[i]=State.UNKNOWN if visible is None else State.VISIBLE if visible else State.BLOCKED
            if visible is None:
                reason='ray touches unknown coverage/height'
            reasons.append(reason)
        return SparseVisibilityResult(states,tuple(reasons),{'target':ti,'backend':'independent Python closed-column reference',
            'observer_coordinates':'requested XY, DTM from containing cell', 'curvature_coefficient':curvature_coefficient,
            'eye_height_m':eye_height_m,'source_data_version':self.manifest['data_version'],
            'crs':self.crs.to_string(),'resolution_m':resolution_m,
            'quality':{'classification':'APPROXIMATE' if estimated_present else 'COLUMN_REFERENCE',
                'estimated_heights_in_any_read_window':estimated_present,
                'unknown_rays':int(np.count_nonzero(states==State.UNKNOWN))},
            'source_provenance':copy.deepcopy(self.manifest.get('provenance',{})),
            'timings':{'total_s':time.perf_counter()-started},'limitations':LIMITATIONS.copy()+self.manifest.get('limitations',[])})
