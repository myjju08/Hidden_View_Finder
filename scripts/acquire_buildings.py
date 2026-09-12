#!/usr/bin/env python3
"""Fetch a local Seoul GBA 2D footprint/height subset via bounded HTTP ranges.

No full regional/national tile or 3D geometry is downloaded. GeoParquet row-group
statistics select candidate groups; exact footprint/bbox intersection is then
applied. GBA heights are estimates in metres AGL, never surveyed roof elevations.
The Source Cooperative conversion is WGS84 (GeoParquet CRS84 default), unlike
original GBA GeoJSON EPSG:3857. This is verified against footer metadata/bounds.

.venv/bin/python scripts/acquire_buildings.py --bbox 126.82 37.42 127.19 37.72
"""
from __future__ import annotations
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import http.client
import io
import json
import math
from pathlib import Path
import random
import re
import socket
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime

import numpy as np
import pyarrow.parquet as pq
from shapely import from_wkb
from shapely.geometry import box
from seoul_visibility.acquisition_safety import AcquisitionError
if __package__:
    from .building_acquisition_common import budget_check, owned_temporary, scoped_paths, validate_bounds, validate_download_cap
else:
    from building_acquisition_common import budget_check, owned_temporary, scoped_paths, validate_bounds, validate_download_cap

URL = 'https://s3.us-west-2.amazonaws.com/us-west-2.opendata.source.coop/tge-labs/globalbuildingatlas-lod1/e125_n40_e130_n35.parquet'
MiB = 1024**2


class _IncompleteRange(IOError):
    """Retryable short/oversized payload, distinguished from network failures."""


def _range_http_category(status):
    if status in {401, 403, 407}: return 'authentication_blocked'
    if status in {404, 410}: return 'missing_source'
    if status == 429: return 'rate_limited'
    if status == 412: return 'source_changed'
    if status == 451: return 'permission_blocked'
    if status in {408, 500, 502, 503, 504}: return 'network_error'
    return 'range_protocol_error'


class Ranges(io.RawIOBase):
    """Bounded in-memory ranges pinned to one upstream object.

    Transfer accounting includes unsuccessful partial reads. Retries never write
    to disk or concatenate responses, and never accept a full HTTP 200 payload.
    """
    def __init__(self, url: str, cap: int, *, retries: int = 5,
                 timeout: float = 30, expected_identity: dict | None = None):
        super().__init__()
        if cap < MiB:
            raise ValueError('Download cap must allow at least 1 MiB for metadata and local ranges')
        if not isinstance(retries, int) or not 0 <= retries <= 5:
            raise ValueError('Transient retry limit must be between zero and five')
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.query:
            raise ValueError('Range source requires a public HTTPS URL without credentials or query secrets')
        self.url, self.cap, self.pos, self.bytes_read = url, cap, 0, 0
        self.host, self.retries, self.timeout = parsed.hostname, retries, timeout
        self.reads = []
        self._cache_start = 0
        self._cache_data = None
        self.size = None
        self.etag = self.version = self.modified = None
        self.tail = self._fetch(None, 8)
        if self.tail[-4:] != b'PAR1':
            raise AcquisitionError('corrupt_content', 'Not a Parquet file')
        if expected_identity and self.identity != expected_identity:
            raise AcquisitionError('source_changed', 'Upstream identity mismatch; preserve prior subset and inspect new version')
    @property
    def identity(self):
        return {'url': self.url, 'size': self.size, 'etag': self.etag,
                'version': self.version, 'last_modified': self.modified}
    def _fetch(self, start, n):
        for attempt in range(self.retries + 1):
            if self.bytes_read + n + 1 > self.cap:
                raise AcquisitionError('transfer_limit', 'Bounded range total transfer cap exceeded')
            headers = {'Range': 'bytes=-8' if start is None else f'bytes={start}-{start+n-1}',
                       'Accept-Encoding': 'identity',
                       'User-Agent': 'HiddenViewFinder/0.2 (+https://github.com/myjju08/Hidden_View_Finder)'}
            if self.etag:
                headers['If-Match'] = self.etag
            request = urllib.request.Request(self.url, headers=headers)
            retry_after = None
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    final = urllib.parse.urlsplit(response.geturl())
                    if final.scheme != 'https' or final.hostname != self.host:
                        raise AcquisitionError('redirect_blocked', f'Range redirect left the approved HTTPS source host: {final.hostname}')
                    if response.status != 206:
                        raise AcquisitionError(_range_http_category(response.status), 'Refusing non206 response/full tile')
                    match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('Content-Range', ''))
                    if not match:
                        raise AcquisitionError('range_protocol_error', 'Missing or invalid Content-Range')
                    first, last, size = map(int, match.groups())
                    expected_first = size - 8 if start is None else start
                    if size < 8 or first != expected_first or last != first + n - 1 or last >= size:
                        raise AcquisitionError('range_protocol_error', 'Unexpected response range')
                    if self.size is not None and size != self.size:
                        raise AcquisitionError('source_changed', 'Upstream object size changed')
                    length = response.headers.get('Content-Length')
                    if length is not None and (not length.isdecimal() or int(length) != n):
                        raise AcquisitionError('range_protocol_error', 'Content-Length disagrees with bounded range')
                    if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
                        raise AcquisitionError('range_protocol_error', 'Encoded ranges are not permitted')
                    etag, version = response.headers.get('ETag'), response.headers.get('x-amz-version-id')
                    if not etag or etag.startswith('W/') or not (etag.startswith('"') and etag.endswith('"')):
                        raise AcquisitionError('range_protocol_error', 'A strong upstream ETag is required')
                    if self.etag and (etag != self.etag or version != self.version):
                        raise AcquisitionError('source_changed', 'Upstream identity mismatch during range acquisition')
                    if self.size is None:
                        self.size, self.etag, self.version = size, etag, version
                        self.modified = response.headers.get('Last-Modified')
                    chunks, received = [], 0
                    while received < n + 1:
                        block = response.read(min(64 * 1024, n + 1 - received))
                        if not block:
                            break
                        received += len(block)
                        self.bytes_read += len(block)
                        chunks.append(block)
                    if received != n:
                        raise _IncompleteRange('Incomplete or oversized HTTP range')
                    data = b''.join(chunks)
                    self.reads.append([first, n, hashlib.sha256(data).hexdigest()])
                    return data
            except urllib.error.HTTPError as error:
                category = _range_http_category(error.code)
                retry_after = (error.headers or {}).get('Retry-After')
                error.close()
                if error.code not in {408, 429, 500, 502, 503, 504}:
                    raise AcquisitionError(category, f'Range HTTP failure {error.code} from {self.host}; no transient retry') from error
                if attempt == self.retries:
                    raise AcquisitionError(category, f'Range HTTP failure {error.code} from {self.host}; bounded retries exhausted') from error
            except (urllib.error.URLError, TimeoutError, socket.timeout, IOError, http.client.HTTPException) as error:
                if isinstance(error, http.client.IncompleteRead) and isinstance(error.partial, bytes):
                    self.bytes_read += len(error.partial)
                if attempt == self.retries:
                    category = ('corrupt_content' if isinstance(error, (_IncompleteRange, http.client.IncompleteRead))
                                else 'range_protocol_error' if isinstance(error, http.client.BadStatusLine)
                                and not isinstance(error, http.client.RemoteDisconnected)
                                else 'network_error')
                    message = ('Incomplete or oversized HTTP range' if category == 'corrupt_content'
                               else f'Bounded range retries exhausted for {self.host}: {type(error).__name__}')
                    raise AcquisitionError(category, message) from error
            delay = min(30.0, 2 ** attempt + random.uniform(0, 0.5))
            if retry_after:
                try:
                    try:
                        pause = float(retry_after)
                    except ValueError:
                        pause = (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds()
                    if not math.isfinite(pause): raise ValueError('Non-finite retry delay')
                except (TypeError, ValueError, OverflowError):
                    raise AcquisitionError('range_protocol_error', 'Invalid Retry-After header; checkpoint and inspect provider response') from None
                if pause > 60:
                    raise AcquisitionError('rate_limited', 'Rate limit requests a longer pause; checkpoint and resume later')
                delay = max(delay, max(0, pause))
            time.sleep(delay)
    def readable(self): return True
    def seekable(self): return True
    def tell(self): return self.pos
    def seek(self, offset, whence=0):
        p = offset if whence == 0 else self.pos + offset if whence == 1 else self.size + offset
        if not 0 <= p <= self.size:
            raise ValueError('Seek outside source')
        self.pos = p
        return p
    def clearcache(self):
        """Release the sole row-group buffer; cached bytes never reach disk."""
        self._cache_start, self._cache_data = 0, None
    def prefetch(self, start: int, n: int):
        """Fetch one inspected physical row-group span with normal safeguards."""
        if (not isinstance(start, int) or not isinstance(n, int) or start < 0
                or n <= 0 or n > 8 * MiB or start + n > self.size):
            raise ValueError('Prefetch must be one in-object range of at most 8 MiB')
        self.clearcache()  # Never retain two row-group payloads simultaneously.
        data = self._fetch(start, n)
        self._cache_start, self._cache_data = start, data
        return {'start': start, 'bytes': n, 'sha256': hashlib.sha256(data).hexdigest()}
    def read(self, n=-1):
        n = self.size - self.pos if n < 0 else min(n, self.size-self.pos)
        if n == 0: return b''
        if n > 8*MiB:
            raise AcquisitionError('transfer_limit', f'Bounded range/read cap exceeded: request={n}, total={self.bytes_read}')
        start = self.pos
        if (self._cache_data is not None and self._cache_start <= start
                and start + n <= self._cache_start + len(self._cache_data)):
            data = self._cache_data[start - self._cache_start:start - self._cache_start + n]
        else:
            data = self._fetch(start, n)
        self.pos += n
        return data


def main(argv=None):
    from osgeo import ogr, osr

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bbox', nargs=4, type=float, default=[126.82,37.42,127.19,37.72])
    parser.add_argument('--data-root', type=Path, default=Path('data'), help='Account all acquisition outputs under this storage budget root')
    parser.add_argument('--output', type=Path, default=None)
    parser.add_argument('--max-download-mib', type=int, default=150)
    args = parser.parse_args(argv)
    validate_bounds(args.bbox, '--bbox')
    validate_download_cap(args.max_download_mib)
    output = args.output or args.data_root / 'acquisition/buildings/gba_seoul_2025.gpkg'
    data_root, paths = scoped_paths(args.data_root, output=output,
                                   metadata=output.with_suffix('.source.json'),
                                   temporary=output.with_suffix('.partial.gpkg'))
    args.output = paths['output']
    if paths['metadata'].exists():
        raise FileExistsError(f'Metadata already exists; preserved: {paths["metadata"]}')
    if args.output.exists():
        raise SystemExit(f'Output already exists; source preserved: {args.output}')
    west,south,east,north = args.bbox
    if not (126.5 <= west < east <= 127.6 and 37.2 <= south < north <= 38.0):
        raise SystemExit('This acquisition is limited to Seoul and its immediate surroundings')
    initial_preflight = budget_check(data_root, additional_bytes=200*MiB, temporary_bytes=200*MiB)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    remote = Ranges(URL, args.max_download_mib*MiB)
    file = pq.ParquetFile(remote, pre_buffer=False)
    metadata = file.metadata
    geo = json.loads(metadata.metadata[b'geo'])
    spec = geo['columns']['geometry']
    if ('crs' in spec or spec['geometry_types'] != ['Polygon']
            or spec.get('encoding') != 'WKB' or geo.get('version') != '1.1.0'):
        raise RuntimeError('Unexpected CRS/geometry; review source metadata')
    if spec['bbox'][0] < 120 or spec['bbox'][2] > 135:
        raise RuntimeError('Declared geographic CRS does not match coordinate magnitudes')
    groups = []
    for i in range(metadata.num_row_groups):
        row = metadata.row_group(i)
        stats = {row.column(j).path_in_schema:row.column(j).statistics for j in range(row.num_columns)}
        if (stats['bbox.xmin'].min <= east and stats['bbox.ymin'].min <= north
                and stats['bbox.xmax'].max >= west and stats['bbox.ymax'].max >= south):
            groups.append(i)
    planned = sum(metadata.row_group(i).column(j).total_compressed_size for i in groups for j in range(metadata.num_columns))
    if planned+MiB > remote.cap:
        raise RuntimeError(f'Planned download {planned} exceeds cap')
    print(json.dumps({'row_groups':groups,'planned_bytes':planned}), flush=True)
    ogr.UseExceptions(); osr.UseExceptions()
    temp = paths['temporary']
    with owned_temporary(temp, gpkg=True):
        ds = None
        try:
            ds = ogr.GetDriverByName('GPKG').CreateDataSource(str(temp))
            sr = osr.SpatialReference();sr.ImportFromEPSG(4326)
            sr.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            layer = ds.CreateLayer('buildings',sr,ogr.wkbPolygon,options=['SPATIAL_INDEX=YES'])
            for name,typ in [('source',ogr.OFTString),('source_id',ogr.OFTString),('height_m',ogr.OFTReal),
                             ('height_var',ogr.OFTReal),('estimated',ogr.OFTInteger),('unresolved',ogr.OFTInteger)]:
                layer.CreateField(ogr.FieldDefn(name,typ))
            count=invalid=missing=holes=low_one=low_two=0; sources=Counter(); heights=[]; region=box(*args.bbox)
            ds.StartTransaction()
            try:
                for rg in groups:
                    table = file.read_row_group(rg, use_threads=False)
                    rows = table.to_pylist()
                    for row in rows:
                        b=row['bbox']
                        if b['xmin'] > east or b['ymin'] > north or b['xmax'] < west or b['ymax'] < south: continue
                        shape=from_wkb(row['geometry'])
                        if not shape.intersects(region):continue
                        if shape.has_z: raise RuntimeError('Unexpected 3D source geometry; only 2D footprints are supported')
                        holes += bool(shape.interiors)
                        invalid += not shape.is_valid
                        height=row['height']; unresolved=height is None or not np.isfinite(height) or height<=0
                        missing += unresolved
                        feature=ogr.Feature(layer.GetLayerDefn())
                        feature.SetGeometry(ogr.CreateGeometryFromWkb(row['geometry']))
                        feature.SetField('source',row['source']);feature.SetField('source_id',row['id'])
                        if not unresolved:
                            feature.SetField('height_m',height);heights.append(height)
                            low_one += height < 1; low_two += height < 2
                        if row['var'] is not None:feature.SetField('height_var',row['var'])
                        feature.SetField('estimated',1);feature.SetField('unresolved',int(unresolved))
                        layer.CreateFeature(feature);sources[row['source']]+=1;count+=1
                    ds.CommitTransaction();ds.StartTransaction()
                    budget_check(data_root,additional_bytes=100*MiB,temporary_bytes=100*MiB)
                    print(json.dumps({'row_group':rg,'features_retained':count,'downloaded_bytes':remote.bytes_read}),flush=True)
                ds.CommitTransaction();ds=None
                temp.replace(args.output)
            except BaseException:
                ds=None
                raise
        finally:
            ds = None
    report={'data_root':str(data_root),'preflight':initial_preflight,'source_url':URL,'source_catalog':'https://source.coop/tge-labs/globalbuildingatlas-lod1',
            'original_dataset':'https://github.com/zhu-xlab/GlobalBuildingAtlas',
            'publication':'https://doi.org/10.5194/essd-17-6647-2025',
            'accessed_utc':datetime.now(timezone.utc).isoformat(),'http_etag':remote.etag,
            'object_version':remote.version,'object_last_modified':remote.modified,
            'source_object_bytes_not_fully_downloaded':remote.size,'http_bytes_downloaded':remote.bytes_read,
            'ranges':remote.reads,'bbox_lon_lat':args.bbox,'crs':'OGC:CRS84 / EPSG:4326 always_xy',
            'crs_evidence':'Absent crs key in GeoParquet1.1.0 means CRS84 (explicit null would be rejected)',
            'crs_specification':'https://geoparquet.org/releases/v1.1.0/',
            'geometry':'ordinary 2D Polygon footprint; no mesh/3D geometry','height_field':'height_m',
            'height_reference':'AGL','height_units':'m','heights_estimated':True,
            'height_method':'Existing GBA ML-derived building heights; no ML trained or executed by this engine',
            'licences':['ODbL for OSM/Microsoft footprints','CC BY-NC 4.0 for other footprints and height estimates'],
            'source_observation_dates':'Mixed source dates; individual temporal accuracy not certified; release hosted September2025',
            'features':count,'invalid_geometry':invalid,'unresolved_height':missing,'sources':dict(sources),
            'geometry_z_count':0,'geometry_with_holes_count':holes,
            'height_below_1m':low_one,'height_below_2m':low_two,
            'height_quantiles_m':np.quantile(heights,[0,.25,.5,.75,.95,1]).tolist() if heights else [],
            'row_groups_selected':groups,'row_groups_total':metadata.num_row_groups,
            'elapsed_s':time.perf_counter()-started,'file_bytes':args.output.stat().st_size,
            'sha256':hashlib.sha256(args.output.read_bytes()).hexdigest(),
            'limitations':['Dataset-domain coverage is not a guarantee all real buildings were detected.',
                'Height estimates and footprint omission/commission can change visibility.',
                'The original release licence split applies despite the older combined conversion.',
                'Retained source footprints are not repaired; engine preparation must repair/reject explicitly.']}
    paths['metadata'].write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k!='ranges'},indent=2))

if __name__ == '__main__': main()
