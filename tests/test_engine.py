import json
import math
import os
from pathlib import Path
import threading

import numpy as np
import pytest
from osgeo import gdal, osr
from pyproj import Transformer

from seoul_visibility import (CandidateMask, ConfigurationError, IncompleteCoverageError,
    State, TargetPoint, UnsupportedTargetError, VisibilityEngine)
from seoul_visibility.resources import StoragePolicy, ResourceBudgetError, preflight

def product(tmp_path, *, surface=None, dtm=None, occupancy=None, quality=None, boundary=None, res=5., size=81):
    gt=(199800.,res,0.,550200.,0.,-res)
    dtm=np.zeros((size,size),np.float32) if dtm is None else dtm.astype(np.float32)
    arrays={'dtm':dtm,'surface':dtm.copy() if surface is None else surface.astype(np.float32),
        'occupancy':np.zeros_like(dtm,dtype=np.uint8) if occupancy is None else occupancy,
        'quality':np.full_like(dtm,3,dtype=np.uint8) if quality is None else quality}
    if boundary is not None: arrays['output_mask']=boundary
    p={'resolution_m':res,'transform':gt,'width':size,'height':size,
       'bounds':[gt[0],gt[3]-size*res,gt[0]+size*res,gt[3]]}
    srs=osr.SpatialReference();srs.ImportFromEPSG(5186)
    for name,a in arrays.items():
        path=tmp_path/f'{name}.tif'
        ds=gdal.GetDriverByName('GTiff').Create(str(path),size,size,1,
            gdal.GDT_Float32 if name in ('dtm','surface') else gdal.GDT_Byte,
            options=['TILED=YES','COMPRESS=DEFLATE'])
        ds.SetGeoTransform(gt);ds.SetProjection(srs.ExportToWkt());ds.GetRasterBand(1).WriteArray(a);ds=None
        p[name]=path.name
    m={'schema_version':1,'status':'ready','data_version':'fixture-v1','crs':'EPSG:5186',
       'vertical_reference':'test datum','source_kind':'synthetic','products':{str(res):p}}
    path=tmp_path/'manifest.json';path.write_text(json.dumps(m))
    return path

def target_at(engine, row=40, col=40, height=5., reference='agl', dx=0., dy=0.):
    p=next(iter(engine._products.values()));gt=p['transform'];res=p['resolution_m']
    lon,lat=engine.to_lonlat.transform(gt[0]+(col+.5)*res+dx,gt[3]-(row+.5)*res+dy)
    return TargetPoint(lon,lat,height,reference,'test datum' if reference=='absolute' else None)

def state_at(result, x, y):
    c=round((x-result.transform[0])/result.resolution_m-.5)
    r=round((result.transform[3]-y)/result.resolution_m-.5)
    return result.states[r,c]

def test_flat_axis_pixel_centers_radius_offsets_export(tmp_path):
    path=product(tmp_path)
    with VisibilityEngine.from_manifest(path) as e:
        t=target_at(e,34,46,dx=1.,dy=.8)
        r=e.visible_from_target(t,radius_m=50,curvature_coefficient=0)
        ti=r.metadata['target'];eff=ti['effective']
        assert eff['row']==34 and eff['col']==46
        assert ti['requested']['x']==pytest.approx(eff['x']+1,abs=1e-6)
        assert ti['requested']['y']==pytest.approx(eff['y']+.8,abs=1e-6)
        assert 126<t.lon<128 and 36<t.lat<39
        assert np.count_nonzero(r.states==State.VISIBLE)==317
        assert not np.any(r.states==State.BLOCKED)
        assert state_at(r,eff['x']+50,eff['y'])==State.VISIBLE
        assert state_at(r,eff['x']+50,eff['y']+5)==State.EXCLUDED
        assert r.transform[0]==eff['x']-52.5
        sampled=r.sample_coordinates(max_points=10,wgs84=False)
        assert sampled.shape==(10,2)
        dest=r.export_geotiff(tmp_path/'result.tif');ds=gdal.Open(str(dest))
        np.testing.assert_array_equal(ds.ReadAsArray(),r.states)
        assert ds.GetGeoTransform()==r.transform
        assert ds.GetRasterBand(1).GetNoDataValue()==3
        assert not list(tmp_path.glob('.visibility-export-*'))

def test_wall_retained_candidate_boundary_applied_after_viewshed(tmp_path):
    s=np.zeros((81,81),np.float32);s[:,45]=30
    occ=(s>0).astype(np.uint8)
    boundary=np.ones_like(occ);boundary[:20]=0
    with VisibilityEngine(product(tmp_path,surface=s,occupancy=occ,boundary=boundary)) as e:
        t=target_at(e);base=e.visible_from_target(t,150,curvature_coefficient=0)
        xy=base.metadata['target']['effective'];x,y=xy['x'],xy['y']
        assert state_at(base,x+25,y)==State.EXCLUDED
        assert state_at(base,x+50,y)==State.BLOCKED
        assert state_at(base,x-50,y)==State.VISIBLE
        mask=np.zeros((81,81),bool);mask[:,46:]=True
        r=e.visible_from_target(t,150,curvature_coefficient=0,candidate_mask=mask)
        assert state_at(r,x+50,y)==State.BLOCKED  # masked-out wall still occludes
        assert state_at(r,x-50,y)==State.EXCLUDED
        assert r.metadata['output_boundary_applied']

def test_roof_agl_absolute_and_below_roof(tmp_path):
    dtm=np.full((81,81),100,np.float32);s=dtm.copy();s[39:42,39:42]=120
    with VisibilityEngine(product(tmp_path,dtm=dtm,surface=s,occupancy=(s>dtm).astype(np.uint8))) as e:
        with pytest.raises(UnsupportedTargetError,match='below modeled'):e.visible_from_target(target_at(e,height=19),50)
        r=e.visible_from_target(target_at(e,height=20),50)
        assert r.metadata['target']['absolute_elevation_m']==120
        assert state_at(r,200002.5,549997.5)==State.EXCLUDED
        r2=e.visible_from_target(target_at(e,height=130,reference='absolute'),50)
        assert r2.metadata['target']['absolute_elevation_m']==130
        with pytest.raises(UnsupportedTargetError,match='vertical_reference'):
            e.visible_from_target(TargetPoint(127,37.55,130,'absolute'),50)

@pytest.mark.parametrize('kind',['terrain','surface','coverage','height','conflict'])
def test_unknown_window_rejected_even_off_candidate(tmp_path,kind):
    dtm=np.zeros((81,81),np.float32);s=dtm.copy();q=np.full((81,81),3,np.uint8)
    if kind=='terrain':dtm[35,35]=np.nan
    if kind=='surface':s[35,35]=np.nan
    if kind=='coverage':q[35,35]=1
    if kind=='height':q[35,35]|=8
    if kind=='conflict':q[35,35]|=16
    with VisibilityEngine(product(tmp_path,dtm=dtm,surface=s,quality=q)) as e:
        with pytest.raises(IncompleteCoverageError):e.visible_from_target(target_at(e),50,candidate_mask=np.zeros((81,81),bool))

def test_open_surface_height_semantics_assertion(tmp_path):
    s=np.zeros((81,81),np.float32);s[35,35]=1.7
    with VisibilityEngine(product(tmp_path,surface=s)) as e:
        with pytest.raises(ConfigurationError,match='Surface contract'):e.visible_from_target(target_at(e),50)

def test_estimated_flag_and_strict_policy(tmp_path):
    q=np.full((81,81),3,np.uint8);q[35,35]|=4
    with VisibilityEngine(product(tmp_path,quality=q)) as e:
        r=e.visible_from_target(target_at(e),50)
        assert r.metadata['quality']['classification']=='APPROXIMATE'
        with pytest.raises(ConfigurationError,match='strict'):e.visible_from_target(target_at(e),50,quality_policy='fill_zero')

@pytest.mark.parametrize('radius',[0,-1,float('nan'),float('inf')])
def test_invalid_radius(tmp_path,radius):
    with VisibilityEngine(product(tmp_path)) as e:
        with pytest.raises(ValueError,match='radius_m'):e.visible_from_target(target_at(e),radius)

@pytest.mark.parametrize('height',[float('nan'),float('inf'),-1])
def test_invalid_agl(tmp_path,height):
    with VisibilityEngine(product(tmp_path)) as e:
        with pytest.raises(UnsupportedTargetError):e.visible_from_target(target_at(e,height=height),50)

def test_invalid_coverage_resolution_coordinates_settings(tmp_path):
    with VisibilityEngine(product(tmp_path)) as e:
        t=target_at(e)
        with pytest.raises(IncompleteCoverageError,match='surrounding'):e.visible_from_target(t,200)
        with pytest.raises(ValueError,match='interactive'):e.visible_from_target(t,10001)
        with pytest.raises(ConfigurationError,match='independently'):e.visible_from_target(t,50,resolution_m=2)
        with pytest.raises(UnsupportedTargetError,match='outside'):e.visible_from_target(TargetPoint(0,0,10,'agl'),50)
        with pytest.raises(UnsupportedTargetError):e.visible_from_target(TargetPoint(t.lat,t.lon,10,'agl'),50)
        for kwargs in ({'eye_height_m':-1},{'eye_height_m':float('nan')},{'curvature_coefficient':1.1}):
            with pytest.raises(ValueError):e.visible_from_target(t,50,**kwargs)
        for mask in (np.ones((81,81),np.uint8),np.ones((5,5),bool),CandidateMask(np.ones((81,81),bool),(0,5,0,0,0,-5),'EPSG:5186')):
            with pytest.raises(ConfigurationError):e.visible_from_target(t,50,candidate_mask=mask)

def test_determinism_cache_all_parameters_and_eviction(tmp_path):
    with VisibilityEngine(product(tmp_path),memory_cache_bytes=100_000) as e:
        t=target_at(e);r=e.visible_from_target(t,50)
        assert r.metadata['cache_status']=='miss'
        h=e.visible_from_target(t,50);assert h.metadata['cache_status']=='memory_hit'
        np.testing.assert_array_equal(h.states,r.states)
        h.metadata['quality']['classification']='corrupted'
        assert e.visible_from_target(t,50).metadata['quality']['classification']!='corrupted'
        assert not h.states.flags.writeable
        variations=[{'target':target_at(e,height=6)},{'target':target_at(e,dx=.1)},
            {'radius_m':51},{'eye_height_m':2},{'curvature_coefficient':0},
            {'candidate_mask':np.ones((81,81),bool)}]
        for variation in variations:
            kw={'target':t,'radius_m':50};kw.update(variation)
            assert e.visible_from_target(**kw).metadata['cache_status']=='miss'
        mask=np.ones((81,81),bool);e.visible_from_target(t,50,candidate_mask=mask)
        mask[40,40]=False
        assert e.visible_from_target(t,50,candidate_mask=mask).metadata['cache_status']=='miss'
        assert e._cache_bytes<=e.memory_cache_bytes
    with VisibilityEngine(tmp_path/'manifest.json',memory_cache_bytes=1) as e:
        e.visible_from_target(target_at(e),50)
        assert e.visible_from_target(target_at(e),50).metadata['cache_status']=='miss'
        assert e._cache_bytes==0

def test_prepared_changes_require_reopen(tmp_path):
    path=product(tmp_path)
    with VisibilityEngine(path) as e:
        e.visible_from_target(target_at(e),50)
        m=json.loads(path.read_text());m['data_version']='v2';path.write_text(json.dumps(m))
        with pytest.raises(ConfigurationError,match='reopen'):e.visible_from_target(target_at(e),50)
    with VisibilityEngine(path) as e:assert e.visible_from_target(target_at(e),50).metadata['source_data_version']=='v2'

def test_thread_rejected_closed_engine(tmp_path):
    e=VisibilityEngine(product(tmp_path));errors=[]
    def run():
        try:e.visible_from_target(target_at(e),50)
        except RuntimeError as exc:errors.append(str(exc))
    th=threading.Thread(target=run);th.start();th.join()
    assert 'threads' in errors[0]
    e.close()
    with pytest.raises(RuntimeError,match='closed'):e.visible_from_target(TargetPoint(127,37.5,5,'agl'),50)

def test_sparse_order_and_unknown(tmp_path):
    s=np.zeros((81,81),np.float32);s[:,45]=30;q=np.full((81,81),3,np.uint8);q[40,30]=1
    with VisibilityEngine(product(tmp_path,surface=s,occupancy=(s>0).astype(np.uint8),quality=q)) as e:
        t=target_at(e)
        coords=[(target_at(e,col=c).lon,target_at(e,col=c).lat) for c in (50,45,35,25)]
        coords += [(float('nan'),0),(0,0)]
        r=e.check_observers(t,np.array(coords),curvature_coefficient=0)
        assert r.states.tolist()==[State.BLOCKED,State.EXCLUDED,State.VISIBLE,State.UNKNOWN,State.EXCLUDED,State.EXCLUDED]
        assert len(r.reasons)==6

def test_storage_failures_preserve_sources(tmp_path):
    raw=tmp_path/'source.shp';raw.write_bytes(b'preserve')
    with pytest.raises(ResourceBudgetError):preflight(tmp_path,additional_bytes=100,policy=StoragePolicy(total_budget_bytes=10))
    with pytest.raises(ResourceBudgetError):preflight(tmp_path,temporary_bytes=10,policy=StoragePolicy(temporary_budget_bytes=1))
    with pytest.raises(ResourceBudgetError):preflight(tmp_path,policy=StoragePolicy(minimum_free_bytes=10**18))
    assert raw.read_bytes()==b'preserve'

def test_memory_failure_before_dense(tmp_path,monkeypatch):
    monkeypatch.setattr('seoul_visibility.resources.available_memory_bytes',lambda:1024)
    with VisibilityEngine(product(tmp_path)) as e:
        with pytest.raises(ResourceBudgetError):e.visible_from_target(target_at(e),50)

@pytest.mark.parametrize('name,nodata',[('occupancy',0),('quality',3)])
def test_mask_nodata_rejected(tmp_path,name,nodata):
    path=product(tmp_path)
    ds=gdal.Open(str(tmp_path/f'{name}.tif'),gdal.GA_Update)
    ds.GetRasterBand(1).SetNoDataValue(nodata);ds=None
    with VisibilityEngine(path) as e:
        with pytest.raises(IncompleteCoverageError):e.visible_from_target(target_at(e),50)


def test_unknown_boundary_is_excluded(tmp_path):
    mask=np.ones((81,81),np.uint8);mask[40,41]=255
    path=product(tmp_path,boundary=mask)
    ds=gdal.Open(str(tmp_path/'output_mask.tif'),gdal.GA_Update)
    ds.GetRasterBand(1).SetNoDataValue(255);ds=None
    with VisibilityEngine(path) as e:
        r=e.visible_from_target(target_at(e),50)
        eff=r.metadata['target']['effective']
        assert state_at(r,eff['x']+5,eff['y'])==State.EXCLUDED


def test_whitespace_unknown_datum_rejected(tmp_path):
    path=product(tmp_path);m=json.loads(path.read_text());m['vertical_reference']=' unknown '
    path.write_text(json.dumps(m))
    with pytest.raises(ConfigurationError,match='vertical_reference'):VisibilityEngine(path)


def test_independently_generated_two_metre_product(tmp_path):
    from seoul_visibility.synthetic import create_synthetic
    manifest=create_synthetic(tmp_path/'two-metre',size_m=240,resolution_m=2)
    with VisibilityEngine(manifest) as e:
        t=target_at(e,row=60,col=60,height=120)
        r=e.visible_from_target(t,radius_m=50,resolution_m=2)
        assert r.resolution_m==2
        assert r.transform[1]==2 and r.states.shape==(51,51)
        with pytest.raises(ConfigurationError,match='unavailable'):
            e.visible_from_target(t,radius_m=50,resolution_m=5)
