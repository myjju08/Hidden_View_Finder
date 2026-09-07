#!/usr/bin/env python3
"""Acquire the public Seoul 2015 2D building SHP archive, preserving source data.

The dated official archive is historical, not current Seoul coverage. After a
bounded download, read SHP inside ZIP via GDAL and retain only relevant columns
and full footprints intersecting a local study region. Never extract its 1.1 GB DBF.
GRO_FLO_CO is above-ground floors, not measured building height. Missing and zero
floors stay unresolved. No floor-to-height conversion takes place in this step.
"""
from __future__ import annotations
import argparse
from datetime import datetime,timezone
import hashlib,json,math,time,zipfile
from pathlib import Path
import requests
from osgeo import gdal,ogr,osr
if __package__:
    from .building_acquisition_common import budget_check, owned_temporary, scoped_paths, validate_bounds, validate_download_cap
else:
    from building_acquisition_common import budget_check, owned_temporary, scoped_paths, validate_bounds, validate_download_cap

FORM={'infId':'','seq':'2','filePath':'openDATA/data/FILE_13/OA-13224',
      'fileName':'TL_SPBD_BULD_2015_SHP.zip','infSeq':'2','domainId':'seoul'}
URL='https://data.seoul.go.kr/dataList/mapFileDownload.do'
MiB=1024**2


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root',type=Path,default=Path('data'),help='Account archive and output under this storage budget root')
    parser.add_argument('--archive',type=Path,default=None)
    parser.add_argument('--output',type=Path,default=None)
    parser.add_argument('--max-download-mib',type=int,default=250)
    parser.add_argument('--bounds',type=float,nargs=4,default=[191500,546500,205000,560000],help='EPSG5186 bbox; full intersecting footprints retained')
    args=parser.parse_args(argv)
    validate_bounds(args.bounds, '--bounds')
    validate_download_cap(args.max_download_mib)
    archive=args.archive or args.data_root / 'acquisition/buildings/TL_SPBD_BULD_2015_SHP.zip'
    output=args.output or args.data_root / 'acquisition/buildings/official_seoul_2015_core.gpkg'
    data_root, paths=scoped_paths(args.data_root, archive=archive, output=output,
        metadata=output.with_suffix('.source.json'), temporary=output.with_suffix('.partial.gpkg'),
        download_temporary=archive.with_suffix(archive.suffix+'.part'))
    args.archive=paths['archive'];args.output=paths['output']
    if args.output.exists() or paths['metadata'].exists():
        raise FileExistsError('Output or metadata already exists; all existing files are preserved')
    download_cap=args.max_download_mib*MiB
    initial_preflight=budget_check(data_root,additional_bytes=100*MiB+download_cap,temporary_bytes=download_cap)
    start=time.perf_counter();args.archive.parent.mkdir(parents=True,exist_ok=True)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    archive_reused=args.archive.exists()
    if not archive_reused:
        part=paths['download_temporary']
        with owned_temporary(part):
            with requests.post(URL,data=FORM,stream=True,timeout=(30,120)) as response:
                response.raise_for_status();n=0
                with part.open('wb') as output:
                    for block in response.iter_content(MiB):
                        n+=len(block)
                        if n>download_cap:raise RuntimeError('Official archive exceeds configured download cap')
                        output.write(block)
                        if n%(16*MiB)<MiB:
                            budget_check(data_root,additional_bytes=100*MiB,temporary_bytes=download_cap-n)
                            print(json.dumps({'downloaded_bytes':n}),flush=True)
            with zipfile.ZipFile(part) as source:
                if sum(v.file_size for v in source.infolist())>2*1024**3:
                    raise RuntimeError('Unexpected archive expands past 2 GiB')
                if source.testzip() is not None:raise RuntimeError('Corrupt ZIP member')
            part.replace(args.archive)
    gdal.UseExceptions();ogr.UseExceptions();osr.UseExceptions()
    with zipfile.ZipFile(args.archive) as z:
        members=[{'name':i.filename,'compressed_bytes':i.compress_size,'uncompressed_bytes':i.file_size} for i in z.infolist()]
        shp=[i.filename for i in z.infolist() if i.filename.lower().endswith('.shp')]
        if len(shp)!=1:raise RuntimeError('Expected exactly one SHP')
        if z.testzip() is not None:raise RuntimeError('Corrupt ZIP member; source was preserved')
    uri=f'/vsizip/{args.archive.resolve()}/{shp[0]}'
    source=gdal.OpenEx(uri,gdal.OF_VECTOR,open_options=['ENCODING=CP949'])
    layer=source.GetLayer(0);sr=layer.GetSpatialRef()
    if sr is None:raise RuntimeError('Source is missing CRS; do not assign a guessed CRS')
    # The supplied ITRF2000 TM WKT is valid but has no exact EPSG authority.
    # Transform that declared CRS directly; do not assign a guessed code.
    total=layer.GetFeatureCount();extent=layer.GetExtent()
    fields=[layer.GetLayerDefn().GetFieldDefn(i).GetName() for i in range(layer.GetLayerDefn().GetFieldCount())]
    if 'GRO_FLO_CO' not in fields:raise RuntimeError('Verified aboveground floor mapping no longer present')
    missing=zero=positive=negative=0;hist={}
    layer.SetIgnoredFields([f for f in fields if f not in ('GRO_FLO_CO',)])
    for feature in layer:
        value=feature.GetField('GRO_FLO_CO')
        if value is None:missing+=1
        elif value==0:zero+=1
        elif value<0:negative+=1
        else:positive+=1;hist[str(value)]=hist.get(str(value),0)+1
    layer.SetIgnoredFields([]);layer.ResetReading()
    temp=paths['temporary']
    with owned_temporary(temp, gpkg=True):
        out=None
        try:
            out=gdal.VectorTranslate(str(temp),source,format='GPKG',dstSRS='EPSG:5186',
                spatFilter=args.bounds,spatSRS='EPSG:5186',selectFields=['SIG_CD','BUL_MAN_NO','GRO_FLO_CO','UND_FLO_CO','BDTYP_CD'],
                layerName='buildings',geometryType='PROMOTE_TO_MULTI',layerCreationOptions=['SPATIAL_INDEX=YES'])
            if out is None:raise RuntimeError('Failed localfootprint extraction')
            selected=out.GetLayer(0).GetFeatureCount();out=None;temp.replace(args.output)
        finally:
            out=None
    report={'data_root':str(data_root),'preflight':initial_preflight,'catalog':'https://data.seoul.go.kr/dataList/mapView.do?infId=OA-13224&srvType=M',
        'download_url':URL,'download_method':'POST','download_form':FORM,
        'download_accessed_utc':datetime.now(timezone.utc).isoformat(),
        'source_vintage':'2015 as archive name; posted2016-04-15; historical inventory',
        'crs_wkt':sr.ExportToWkt(),'source_epsg':sr.GetAuthorityCode(None),'source_bounds_xy':extent,
        'encoding':'CP949 (SHP DBF language driver0x4E; Korean decoding checked)',
        'feature_count':total,'source_fields':fields,'aboveground_floors_field':'GRO_FLO_CO',
        'belowground_floors_field_not_used':'UND_FLO_CO','measured_height_field':None,
        'floor_field_documentation':'https://business.juso.go.kr/addrlink/qna/qnaDetail.do?bulletinRefSn=112427&noticeMgtSn=112427&noticeType=QNA',
        'floor_missing':missing,'floor_zero':zero,'floor_negative':negative,'floor_positive':positive,'floor_histogram':hist,
        'members':members,'archive_bytes':args.archive.stat().st_size,'archive_sha256':hashlib.sha256(args.archive.read_bytes()).hexdigest(),
        'local_bounds_epsg5186':args.bounds,'local_selected_features':selected,
        'local_geometry_policy':'Retain full geometries intersecting rectangle; no footprint clipping or height imputation',
        'local_file_bytes':args.output.stat().st_size,'local_sha256':hashlib.sha256(args.output.read_bytes()).hexdigest(),
        'elapsed_s':time.perf_counter()-start,'elapsed_scope':'CRC validation, DBF scan, local extraction; includes download only when not reusing an archive',
        'archive_reused':archive_reused,'zip_crc_validated':True,
        'limitations':['Historical 2015 inventory with unverified building changes since 2015.',
            'Floors need explicit metres-per-floor estimate and quality flag in preparation.',
            '0/missing floors are not evidence building is absent.',
            'Archive covers the Seoul administrative area; query extents need actual coverage checks.']}
    paths['metadata'].write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n')
    print(json.dumps(report,indent=2,ensure_ascii=False))

if __name__=='__main__':main()
