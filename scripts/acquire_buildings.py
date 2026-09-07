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
import io
import json
from pathlib import Path
import struct
import time
import urllib.request

import numpy as np
from osgeo import ogr, osr
import pyarrow.parquet as pq
from shapely import from_wkb
from shapely.geometry import box
if __package__:
    from .building_acquisition_common import budget_check, owned_temporary, scoped_paths, validate_bounds, validate_download_cap
else:
    from building_acquisition_common import budget_check, owned_temporary, scoped_paths, validate_bounds, validate_download_cap

URL = 'https://s3.us-west-2.amazonaws.com/us-west-2.opendata.source.coop/tge-labs/globalbuildingatlas-lod1/e125_n40_e130_n35.parquet'
MiB = 1024**2


class Ranges(io.RawIOBase):
    """Seekable HTTP source; strict206 and byte cap avoid silent full downloads."""
    def __init__(self, url: str, cap: int):
        super().__init__()
        if cap < MiB:
            raise ValueError('Download cap must allow at least 1 MiB for metadata and local ranges')
        self.url, self.cap, self.pos, self.bytes_read = url, cap, 0, 0
        self.reads = []
        request = urllib.request.Request(url, headers={'Range': 'bytes=-8'})
        with urllib.request.urlopen(request, timeout=60) as response:
            if response.status != 206:
                raise RuntimeError('Server must support HTTP206 ranges')
            self.size = int(response.headers['Content-Range'].split('/')[-1])
            self.etag = response.headers['ETag']
            self.version = response.headers.get('x-amz-version-id')
            self.modified = response.headers.get('Last-Modified')
            self.tail = response.read(9)
        if len(self.tail) != 8 or self.tail[-4:] != b'PAR1':
            raise RuntimeError('Not a Parquet file')
        self.bytes_read = 8
    def readable(self): return True
    def seekable(self): return True
    def tell(self): return self.pos
    def seek(self, offset, whence=0):
        p = offset if whence == 0 else self.pos + offset if whence == 1 else self.size + offset
        if not 0 <= p <= self.size:
            raise ValueError('Seek outside source')
        self.pos = p
        return p
    def read(self, n=-1):
        n = self.size - self.pos if n < 0 else min(n, self.size-self.pos)
        if n == 0: return b''
        if n > 8*MiB or self.bytes_read+n > self.cap:
            raise RuntimeError(f'Bounded range/read cap exceeded: request={n}, total={self.bytes_read}')
        start = self.pos
        request = urllib.request.Request(self.url, headers={'Range': f'bytes={start}-{start+n-1}', 'If-Match': self.etag})
        with urllib.request.urlopen(request, timeout=60) as response:
            if response.status != 206:
                raise RuntimeError('Refusing non206 response/full tile')
            expected = f'bytes {start}-{start+n-1}/{self.size}'
            if response.headers['Content-Range'] != expected:
                raise RuntimeError('Unexpected response range')
            data = response.read(n+1)
        if len(data) != n: raise RuntimeError('Incomplete HTTP range')
        self.pos += n
        self.bytes_read += n
        self.reads.append([start, n, hashlib.sha256(data).hexdigest()])
        return data


def main(argv=None):
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
