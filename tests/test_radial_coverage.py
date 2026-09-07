"""Unknown unused corners cannot hide required terrain or building gaps."""
import numpy as np
import pytest
from osgeo import gdal

from seoul_visibility import VisibilityEngine, IncompleteCoverageError, State
from seoul_visibility import backend
from test_engine import product, target_at

pytestmark = pytest.mark.skipif(backend.VERSION != '3.8.4', reason='Audited native dependency implementation is GDAL 3.8.4')


@pytest.mark.parametrize('kind',['terrain','height','coverage'])
def test_unused_unknown_corners_preserved_and_larger_radius_rejected(tmp_path,kind):
    dtm=np.zeros((81,81),np.float32)
    surface=dtm.copy();surface[:,45]=30
    occupancy=(surface>dtm).astype(np.uint8)
    quality=np.full((81,81),3,np.uint8)
    known=tmp_path/'known';known.mkdir()
    missing=tmp_path/'missing';missing.mkdir()
    with VisibilityEngine(product(known,surface=surface,dtm=dtm,occupancy=occupancy)) as e:
        expected=e.visible_from_target(target_at(e),100,curvature_coefficient=0).states
    if kind=='terrain':dtm[20,20]=surface[20,20]=np.nan
    if kind=='height':quality[20,20]|=8
    if kind=='coverage':quality[20,20]=1
    path=product(missing,surface=surface,dtm=dtm,occupancy=occupancy,quality=quality)
    with VisibilityEngine(path) as e:
        result=e.visible_from_target(target_at(e),100,curvature_coefficient=0)
        np.testing.assert_array_equal(result.states,expected)
        assert result.metadata['coverage']['unknown_unused_padding_cells']==1
        assert result.metadata['coverage']['strategy']=='radius_plus_one_cell_v1'
        assert result.metadata['quality']['unknown_cells']==0
        assert np.any(result.states==State.BLOCKED)
        with pytest.raises(IncompleteCoverageError):
            e.visible_from_target(target_at(e),150,candidate_mask=np.zeros((81,81),bool))
    # Numerical padding is confined to the local in-memory adapter input.
    ds=gdal.Open(str(missing/'dtm.tif'))
    if kind=='terrain':assert np.isnan(ds.ReadAsArray()[20,20])
    ds=None
    ds=gdal.Open(str(missing/'quality.tif'))
    assert ds.ReadAsArray()[20,20]==quality[20,20]


@pytest.mark.parametrize('col',[59,60,61])
def test_unknown_inside_radius_or_conservative_halo_always_rejected(tmp_path,col):
    q=np.full((81,81),3,np.uint8);q[40,col]|=8
    with VisibilityEngine(product(tmp_path,quality=q)) as e:
        with pytest.raises(IncompleteCoverageError):
            e.visible_from_target(target_at(e),100,candidate_mask=np.zeros((81,81),bool))


def test_unreviewed_backend_falls_back_to_square_and_invalidates_cache(tmp_path,monkeypatch):
    q=np.full((81,81),3,np.uint8);q[20,20]|=8
    with VisibilityEngine(product(tmp_path,quality=q)) as e:
        e.visible_from_target(target_at(e),100)
        monkeypatch.setattr(backend,'radial_coverage_supported',lambda transform:False)
        with pytest.raises(IncompleteCoverageError):e.visible_from_target(target_at(e),100)


def test_radial_proof_does_not_apply_to_different_grids_or_modes(monkeypatch):
    assert backend.radial_coverage_supported((0,5,0,0,0,-5))
    assert not backend.radial_coverage_supported((0,5,.1,0,0,-5))
    assert not backend.radial_coverage_supported((0,5,0,0,0,-2))
    monkeypatch.setattr(backend,'MODE',gdal.GVM_Max)
    assert not backend.radial_coverage_supported((0,5,0,0,0,-5))
