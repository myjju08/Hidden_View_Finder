#!/usr/bin/env python3
"""Spatially compare two dated building estimates; this is not ground-truth accuracy."""
from __future__ import annotations
import argparse,csv,json,time
from pathlib import Path
import numpy as np
from osgeo import ogr
from pyproj import Transformer
import shapely
from shapely.strtree import STRtree


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--official',type=Path,default=Path('data/acquisition/buildings/official_seoul_2015_core.gpkg'))
    parser.add_argument('--gba',type=Path,default=Path('data/acquisition/buildings/gba_seoul_2025.gpkg'))
    parser.add_argument('--output',type=Path,default=Path('reports/building_source_comparison.json'))
    parser.add_argument('--samples',type=int,default=1000)
    parser.add_argument('--seed',type=int,default=20260907)
    parser.add_argument('--floor-height-m',type=float,default=3.0)
    args=parser.parse_args()
    if not np.isfinite(args.floor_height_m) or args.floor_height_m<=0:raise ValueError('Positive floor height required')
    if not 1<=args.samples<=10000:raise ValueError('Select 1 to 10000 diagnostic samples')
    started=time.perf_counter();ogr.UseExceptions()
    official=ogr.Open(str(args.official));layer=official.GetLayer(0)
    bounds=layer.GetExtent();bounds=(bounds[0],bounds[2],bounds[1],bounds[3])
    transform=Transformer.from_crs(5186,4326,always_xy=True)
    geo_bounds=transform.transform_bounds(*bounds)
    gba=ogr.Open(str(args.gba));gl=gba.GetLayer(0);gl.SetSpatialFilterRect(*geo_bounds)
    geometries=[];heights=[];ids=[]
    for f in gl:
        geometries.append(bytes(f.GetGeometryRef().ExportToWkb()));heights.append(f.GetField('height_m'));ids.append(f.GetField('source_id'))
    shapes=shapely.from_wkb(geometries)
    shapes=shapely.transform(shapes,Transformer.from_crs(4326,5186,always_xy=True).transform,interleaved=False)
    valid=shapely.is_valid(shapes)&~shapely.is_empty(shapes)
    shapes=shapes[valid];heights=np.asarray(heights,float)[valid];ids=np.asarray(ids,dtype=object)[valid]
    tree=STRtree(shapes)
    # Reservoir sample positive-floor geometries without retaining all official rows.
    rng=np.random.default_rng(args.seed);samples=[];seen=0
    for f in layer:
        floors=f.GetField('GRO_FLO_CO')
        if floors is None or floors<=0:continue
        seen+=1
        index=seen-1 if len(samples)<args.samples else int(rng.integers(seen))
        if index<args.samples:
            sample=(f.GetFID(),floors,bytes(f.GetGeometryRef().ExportToWkb()))
            if len(samples)<args.samples:samples.append(sample)
            else:samples[index]=sample
    records=[];differences=[];unmatched=invalid_official=0
    any_intersection=centroid_covered=0; covered_fractions=[]; official_areas=[]; best_area_ratios=[]
    for fid,floors,wkb in samples:
        shape=shapely.from_wkb(wkb)
        if not shape.is_valid or shape.is_empty:
            invalid_official+=1;continue
        official_areas.append(shape.area)
        candidates=tree.query(shape,predicate='intersects')
        if not len(candidates):
            unmatched+=1;covered_fractions.append(0.0);continue
        any_intersection+=1
        others=shapes[candidates]
        centroid_covered += bool(np.any(shapely.covers(others,shape.centroid)))
        intersections=shapely.intersection(others,shape)
        covered_fractions.append(float(np.clip(shapely.area(shapely.union_all(intersections))/shape.area,0.0,1.0)))
        inter=shapely.area(intersections);union=shapely.area(others)+shape.area-inter
        iou=np.divide(inter,union,out=np.zeros_like(inter),where=union>0)
        j=int(np.argmax(iou));idx=int(candidates[j])
        best_area_ratios.append(float(shapely.area(others[j])/shape.area))
        if iou[j]<0.5:unmatched+=1;continue
        proxy=floors*args.floor_height_m;delta=float(heights[idx]-proxy)
        records.append({'official_fid':fid,'gba_source_id':ids[idx],'iou':float(iou[j]),'aboveground_floors_2015':floors,'floor_proxy_m':proxy,'gba_estimate_m':float(heights[idx]),'gba_minus_floor_proxy_m':delta})
        differences.append(delta)
    diffs=np.asarray(differences)
    report={'official_input':str(args.official),'gba_input':str(args.gba),'seed':args.seed,'requested_samples':args.samples,
        'sampled_positive_floor_features':len(samples),'eligible_official_features':seen,'gba_features_in_envelope':len(shapes),
        'minimum_iou':0.5,'strong_polygon_matches':len(records),'unmatched_or_low_iou':unmatched,
        'samples_with_any_gba_intersection':any_intersection,
        'samples_with_centroid_covered_by_gba':centroid_covered,
        'samples_with_zero_area_overlap':int(np.count_nonzero(np.asarray(covered_fractions)<=1e-12)),
        'samples_covered_more_than_50_percent_by_gba':int(np.count_nonzero(np.asarray(covered_fractions)>0.5)),
        'covered_area_fraction_quantiles':dict(zip(['min','p25','p50','p75','p95','max'],np.quantile(covered_fractions,[0,.25,.5,.75,.95,1]).tolist())),
        'samples_covered_80_percent_or_more_by_gba':int(np.count_nonzero(np.asarray(covered_fractions)>=0.8)),
        'median_official_area_m2':float(np.median(official_areas)),
        'median_gba_to_official_area_ratio_for_best_overlap':float(np.median(best_area_ratios)),
        'median_official_area_fraction_covered_by_gba':float(np.median(covered_fractions)),
        'invalid_official_geometry_skipped':invalid_official,'floor_height_m':args.floor_height_m,
        'metrics_are_disagreement_between_estimates_not_accuracy':True,
        'mean_signed_height_difference_m':float(diffs.mean()) if len(diffs) else None,
        'median_absolute_height_difference_m':float(np.median(abs(diffs))) if len(diffs) else None,
        'mean_absolute_height_difference_m':float(abs(diffs).mean()) if len(diffs) else None,
        'p95_absolute_height_difference_m':float(np.quantile(abs(diffs),.95)) if len(diffs) else None,
        'elapsed_s':time.perf_counter()-started,
        'limitations':['2015 official floor counts and mixed-date GBA may represent different buildings.',
            'Neither 3 metres per floor nor GBA height is surveyed height ground truth.',
            'Only strong spatial matches are compared; this does not certify completeness or visibility accuracy.',
            'No heights are imputed and neither source is modified.']}
    args.output.write_text(json.dumps(report,indent=2)+'\n')
    with args.output.with_suffix('.csv').open('w',newline='') as stream:
        if records:
            writer=csv.DictWriter(stream,fieldnames=list(records[0]));writer.writeheader();writer.writerows(records)
    print(json.dumps(report,indent=2))

if __name__=='__main__':main()
