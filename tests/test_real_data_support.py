"""Real-source integration policies keep estimated inputs and survey gaps explicit."""
import json
import numpy as np
import pytest
from osgeo import gdal, ogr
from test_prepare import raster, vectors, rectangle, read_product
from seoul_visibility.prepare import prepare, plan
from seoul_visibility.errors import ConfigurationError


def config(tmp_path):
    ground=raster(tmp_path/'dtm.tif',np.full((20,20),100,np.float32))
    b=vectors(tmp_path/'buildings.gpkg',[(rectangle(200010,549910,200030,549930),12,None)])
    return {'data_root':str(tmp_path),'output_dir':str(tmp_path/'prepared'),'crs':'EPSG:5186',
        'vertical_reference':'test documented datum','resolution_m':5,'bounds':[200000,549900,200100,550000],
        'terrain':{'kind':'raster','path':str(ground),'bare_earth_verified':True,'units':'m','vertical_reference':'test documented datum'},
        'buildings':{'path':str(b),'layer':'footprints','height_field':'measured','height_is_agl':True,
          'units':'m','vertical_reference':'test documented datum','coverage_bounds':[200000,549900,200100,550000]},
        'storage_policy':{'minimum_free_bytes':0}}


def test_existing_estimated_height_keeps_quality_and_method(tmp_path):
    c=config(tmp_path);c['buildings'].update(height_is_estimated=True,height_estimation_method='published estimated AGL heights')
    manifest=prepare(c)
    q=read_product(manifest,'quality');assert np.any(q&4)
    assert json.loads(manifest.read_text())['processing']['buildings']['estimated_heights']==1
    ds=gdal.OpenEx(str(manifest.parent/'buildings.gpkg'),gdal.OF_VECTOR)
    f=ds.GetLayer(0).GetNextFeature();assert f.GetField('height_source')=='published estimated AGL heights'


def test_estimated_source_requires_provenance(tmp_path):
    c=config(tmp_path);c['buildings']['height_is_estimated']=True
    with pytest.raises(ConfigurationError,match='height_estimation_method'):plan(c)


@pytest.mark.parametrize('layer_name',['terrain','buildings'])
def test_full_cell_coverage_polygon_retains_unknown_inside_bbox(tmp_path,layer_name):
    c=config(tmp_path)
    # Narrow missing rightmost strip is inside declared rectangular bounds.
    boundary=vectors(tmp_path/'survey.gpkg',[(rectangle(200000,549900,200097,550000),1,None)])
    c[layer_name]['coverage_boundary']={'path':str(boundary),'layer':'footprints'}
    manifest=prepare(c);q=read_product(manifest,'quality')
    bit=1 if layer_name=='terrain' else 2
    assert np.all(q[:,-1]&bit==0)
    assert np.all(q[:,0]&bit!=0)
    if layer_name=='terrain':
        s=read_product(manifest,'dtm');assert np.all(s[:,-1]<-1e30)


def test_maximum_base_is_explicit_flagged(tmp_path):
    c=config(tmp_path)
    ground=np.tile(np.arange(20,dtype=np.float32)*2,(20,1))+100
    raster(tmp_path/'dtm.tif',ground)
    c['buildings']['slope_conflict_m']=1
    # A wide stepped terrain footprint with a low input height conflicts under median.
    p=prepare(c)
    assert np.any(read_product(p,'quality')&16)
    c['output_dir']=str(tmp_path/'maximum')
    c['buildings'].update(base_estimation_method='maximum',base_estimation_justification='Explicit screening roof overestimate on sloped terrain')
    p=prepare(c);q=read_product(p,'quality')
    assert np.any(q&4) and not np.any(q&16)
    meta=json.loads(p.read_text())['processing']['buildings']
    assert meta['large_terrain_relief_footprints']==1 and meta['maximum_base_estimates']==1
    assert np.all(read_product(p,'surface')>=read_product(p,'dtm'))


def test_maximum_base_requires_justification(tmp_path):
    c=config(tmp_path);c['buildings']['base_estimation_method']='maximum'
    with pytest.raises(ConfigurationError,match='justification'):plan(c)


def test_maximum_policy_never_raises_supplied_absolute_base(tmp_path):
    c=config(tmp_path)
    ds=gdal.OpenEx(c['buildings']['path'],gdal.OF_VECTOR|gdal.OF_UPDATE)
    layer=ds.GetLayerByName('footprints')
    layer.CreateField(ogr.FieldDefn('base_abs',ogr.OFTReal))
    feature=layer.GetNextFeature();feature.SetField('base_abs',70.0)
    layer.SetFeature(feature);feature=None;layer=None;ds=None
    c['buildings'].update(base_elevation_field='base_abs',base_estimation_method='maximum',
        base_estimation_justification='An estimated base is allowed only without a supplied base')
    p=prepare(c)
    assert np.any(read_product(p,'quality')&16)
    metadata=json.loads(p.read_text())['processing']['buildings']
    assert metadata['maximum_base_estimates']==0
    ds=gdal.OpenEx(str(p.parent/'buildings.gpkg'),gdal.OF_VECTOR)
    feature=ds.GetLayer(0).GetNextFeature()
    assert feature['base_m']==70.0 and feature['roof_m']==82.0


@pytest.mark.parametrize('missing',['output_mask.tif','buildings.gpkg'])
def test_resume_requires_boundary_and_retained_footprint_index(tmp_path,missing):
    c=config(tmp_path)
    boundary=vectors(tmp_path/'boundary.gpkg',[(rectangle(200000,549900,200100,550000),1,None)])
    c['output_boundary']={'path':str(boundary),'layer':'footprints'}
    manifest=prepare(c)
    (manifest.parent/missing).unlink()
    with pytest.raises(ConfigurationError,match='missing'):
        prepare(c)
