"""Small synthetic raster fixtures, never downloaded or labelled real Seoul."""
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time

import numpy as np
from osgeo import gdal, osr
import pytest

from hidden_view_finder.prototype.tiles import (Tiles, TileContractError, WorkBudget,
    QUALITY_BITS, VERTICAL_REFERENCE, inspect_uncertainty)
from seoul_visibility.reference import reference_los


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path, data):
    path.write_text(json.dumps(data, sort_keys=True))


def collection(tmp_path, *, surface=None, quality=None, occupancy=None,
               terrain=None, missing=(), side=4, tiles_x=2, tiles_y=1):
    """Two or more disjoint synthetic EPSG:5186 5m tiles; global flags false."""
    shape = (side*tiles_y, side*tiles_x)
    surface = np.zeros(shape, dtype=np.float32) if surface is None else np.array(surface,dtype=np.float32)
    terrain = np.zeros(shape,dtype=np.float32) if terrain is None else np.array(terrain,dtype=np.float32)
    occupancy = (surface>terrain).astype(np.uint8) if occupancy is None else np.array(occupancy,dtype=np.uint8)
    quality = np.full(shape,3,dtype=np.uint8) if quality is None else np.array(quality,dtype=np.uint8)
    recipe={"processing_version":"citywide-existing-tin-and-surfaces-v4-window-policy",
            "tile_metres":side*5,"resolution_m":5,"source_sha256":"a"*64,"buildings_sha256":"b"*64}
    recipe_hash=hashlib.sha256(json.dumps(recipe,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    manifest={"recipe":recipe,"recipe_sha256":recipe_hash,"resolution_m":5,
              "visibility_ready":False,"status":"terrain_and_surface_tiles_ready_support_incomplete",
              "tiles":[],"completed_tiles":tiles_x*tiles_y-len(missing)}
    srs=osr.SpatialReference();srs.ImportFromEPSG(5186)
    for ty in range(tiles_y):
        for tx in range(tiles_x):
            if (tx,ty) in missing:
                continue
            tile_id=f"x{tx}_y{ty}";folder=tmp_path/tile_id;folder.mkdir(parents=True)
            size=side*5
            bounds=[tx*size,ty*size,(tx+1)*size,(ty+1)*size]
            gt=[bounds[0],5,0,bounds[3],0,-5]
            tile={"grid":{"id":tile_id,"crs":"EPSG:5186","width":side,"height":side,
                    "bounds":bounds,"transform":gt},"recipe_sha256":recipe_hash,
                    "quality_bits":QUALITY_BITS,"vertical_reference":VERTICAL_REFERENCE,
                    "vertical_conversion":"none","visibility_ready":False,
                    "surface_processing":{"source_buildings_sha256":"b"*64},"products":{}}
            rows=slice((tiles_y-ty-1)*side,(tiles_y-ty)*side);cols=slice(tx*side,(tx+1)*side)
            layers={"dtm":terrain[rows,cols],"surface":surface[rows,cols],
                "occupancy":occupancy[rows,cols],"quality":quality[rows,cols],
                "terrain_quality":np.ones((side,side),dtype=np.uint8),
                "contract_mask":np.full((side,side),3,dtype=np.uint8)}
            for name,array in layers.items():
                path=folder/(name+".tif")
                floating=name in ("dtm","surface")
                ds=gdal.GetDriverByName("GTiff").Create(str(path),side,side,1,
                    gdal.GDT_Float32 if floating else gdal.GDT_Byte)
                ds.SetGeoTransform(gt);ds.SetProjection(srs.ExportToWkt())
                if floating:ds.GetRasterBand(1).SetNoDataValue(-9999)
                ds.GetRasterBand(1).WriteArray(array);ds=None
                tile["products"][name]={"path":path.name,"bytes":path.stat().st_size,"sha256":_sha(path)}
            _write(folder/"tile.json",tile)
            manifest["tiles"].append({"id":tile_id,"path":f"{tile_id}/tile.json"})
    path=tmp_path/"manifest.json";_write(path,manifest)
    return path


def test_real_collection_shape_adapter_supports_cross_tile_with_global_false(tmp_path):
    path=collection(tmp_path)
    with Tiles(path) as tiles:
        result=tiles.ray((2.5,12.5,1.7),(37.5,12.5,1.7))
        assert result["state"]=="visible"
        assert result["tiles_touched"]==["x0_y0","x1_y0"]
        assert result["cells_checked"]==8
        assert tiles.metadata["source_visibility_ready"] is False
        assert tiles.metadata["local_exploration_available"] is True
        assert tiles.metadata["whole_panorama_supported"] is False


def test_thin_obstacle_and_unknown_after_block_dominate(tmp_path):
    surface=np.zeros((4,8));surface[:,3]=10
    quality=np.full((4,8),3);quality[:,5]=11
    with Tiles(collection(tmp_path,surface=surface,quality=quality)) as tiles:
        result=tiles.ray((2.5,12.5,1.7),(37.5,12.5,1.7),curvature_coefficient=0)
        assert result["state"]=="unknown"
        assert result["reason"]=="obstruction_height_unknown"
    quality[:,5]=3
    other=tmp_path/"known";other.mkdir()
    with Tiles(collection(other,surface=surface,quality=quality)) as tiles:
        assert tiles.ray((2.5,12.5,1.7),(37.5,12.5,1.7))["state"]=="blocked"


def test_missing_internal_tile_is_unknown_not_empty(tmp_path):
    with Tiles(collection(tmp_path,tiles_x=3,missing=((1,0),))) as tiles:
        assert tiles.ray((2.5,12.5,1.7),(57.5,12.5,1.7))["reason"]=="missing_tile"
        assert tiles.sample(25,10)["state"]=="unknown"


@pytest.mark.parametrize("mask",[0,1,2,8,16,32,64,128,255])
def test_all_missing_quality_bits_propagate(tmp_path,mask):
    quality=np.full((4,8),3);quality[1,4]=mask
    with Tiles(collection(tmp_path,quality=quality)) as tiles:
        assert tiles.ray((2.5,12.5,1.7),(37.5,12.5,1.7))["state"]=="unknown"


def test_closed_corner_contact_cannot_jump_thin_blocker(tmp_path):
    surface=np.zeros((8,8));surface[3,4]=10
    with Tiles(collection(tmp_path,surface=surface,tiles_y=2)) as tiles:
        result=tiles.ray((2.5,37.5,2),(37.5,2.5,2),curvature_coefficient=0)
        assert result["state"]=="blocked"
        assert len(result["tiles_touched"])==4


def test_exact_requested_observer_and_eye_height_distinct(tmp_path):
    with Tiles(collection(tmp_path)) as tiles:
        obs=tiles.observer(2,12)
        assert obs["xyz"]==[2,12,1.7]
        assert obs["effective_xy"]==[2,12]
        assert obs["snapping_displacement_m"]==0
        assert obs["cell_center_xy"]==[2.5,12.5]
        assert obs["cell_center_displacement_m"]==pytest.approx(math.sqrt(.5))


def test_building_endpoint_excluded_roof_contact_not_lifted(tmp_path):
    surface=np.zeros((4,8));surface[1,:2]=10
    with Tiles(collection(tmp_path,surface=surface)) as tiles:
        assert tiles.observer(2.5,12.5)["state"]=="excluded"
        roof=tiles.target(2.1,12.1)
        assert roof["xyz"]==[2.5,12.5,10]
        assert tiles.target(2.5,12.5,absolute_elevation_m=9)["state"]=="excluded"
        assert tiles.ray((37.5,12.5,1.7),roof["xyz"],curvature_coefficient=0)["state"]=="blocked"
        assert tiles.ray((37.5,12.5,15),roof["xyz"],curvature_coefficient=0)["state"]=="visible"


def test_invalid_building_remains_unknown_obstruction(tmp_path):
    with Tiles(collection(tmp_path),[{"bounds":[18,10,22,15],"reason":"source_flagged_invalid_building"}]) as tiles:
        assert tiles.ray((2.5,12.5,1.7),(37.5,12.5,1.7))["reason"]=="source_flagged_invalid_building"


def test_roof_column_near_top_edge_uses_exact_contact_without_height_lift(tmp_path):
    surface=np.zeros((4,8));surface[1,4]=10
    with Tiles(collection(tmp_path,surface=surface)) as tiles:
        roof=tiles.roof_boundary_target((2.5,12.5),(22.5,12.5))
        assert roof["xyz"]==[20,12.5,10]
        assert tiles.ray((2.5,12.5,1.7),roof["xyz"],curvature_coefficient=0)["state"]=="visible"
        centre=tiles.target(22.5,12.5)
        assert centre["xyz"][2]==roof["xyz"][2]
        assert tiles.ray((2.5,12.5,1.7),centre["xyz"],curvature_coefficient=0)["state"]=="blocked"


def test_roof_column_edge_retains_nearby_self_occlusion_and_unknown(tmp_path):
    surface=np.zeros((4,8));surface[1,3:5]=10
    with Tiles(collection(tmp_path,surface=surface)) as tiles:
        roof=tiles.roof_boundary_target((2.5,12.5),(22.5,12.5))
        assert tiles.ray((2.5,12.5,1.7),roof["xyz"],curvature_coefficient=0)["state"]=="blocked"
        assert tiles.roof_boundary_target((22.5,12.5),(22.5,12.5))["state"]=="excluded"
        assert tiles.roof_boundary_target((2.5,12.5),(7.5,12.5))["state"]=="excluded"


def test_roof_diagonal_intersection_does_not_cross_corner_blocker(tmp_path):
    surface=np.zeros((8,8));surface[4,4]=10;surface[3,4]=15
    with Tiles(collection(tmp_path,surface=surface,tiles_y=2)) as tiles:
        roof=tiles.roof_boundary_target((2.5,37.5),(22.5,17.5))
        assert roof["xyz"]==[20,20,10]
        # Corner contact includes the taller adjacent column; cannot erase it.
        assert tiles.ray((2.5,37.5,1.7),roof["xyz"],curvature_coefficient=0)["state"]=="blocked"


def test_uphill_terrain_edge_is_not_lifted_or_relabelled_canopy(tmp_path):
    surface=np.zeros((4,8));surface[1,4]=10
    with Tiles(collection(tmp_path,surface=surface,terrain=surface)) as tiles:
        target=tiles.surface_boundary_target((2.5,12.5),(22.5,12.5))
        assert target["xyz"]==[20,12.5,10]
        assert tiles.ray((2.5,12.5,1.7),target["xyz"],curvature_coefficient=0)["state"]=="visible"
        assert "not measured canopy" in target["claim_scope"]
        assert tiles.roof_boundary_target((2.5,12.5),(22.5,12.5))["state"]=="excluded"


def test_unknown_seam_anomalies_version_checked(tmp_path):
    path=collection(tmp_path)
    anomaly=tmp_path/"anomalies.json"
    report={"recipe_sha256":json.loads(path.read_text())["recipe_sha256"],"coordinate_crs":"EPSG:5186",
            "anomalies":[{"coordinate_epsg5186":[22.5,12.5]}]}
    _write(anomaly,report)
    with Tiles(path,overlap_anomalies_path=anomaly) as tiles:
        assert tiles.ray((2.5,12.5,1.7),(37.5,12.5,1.7))["reason"]=="terrain_overlap_disagreement"
    report["recipe_sha256"]="x";_write(anomaly,report)
    with pytest.raises(TileContractError,match="different geometry version"):
        Tiles(path,overlap_anomalies_path=anomaly)


def test_grid_and_duplicate_tile_rejected(tmp_path):
    path=collection(tmp_path)
    metadata=tmp_path/"x0_y0/tile.json";m=json.loads(metadata.read_text());m["grid"]["transform"][0]=.1;_write(metadata,m)
    with pytest.raises(TileContractError,match="misaligned"):
        Tiles(path)
    m["grid"]["transform"][0]=0;_write(metadata,m)
    m=json.loads(path.read_text());m["tiles"].append(m["tiles"][0]);m["completed_tiles"]+=1;_write(path,m)
    with pytest.raises(TileContractError,match="overlap"):
        Tiles(path)


def test_changed_raster_and_cached_changed_raster_are_rejected(tmp_path):
    path=collection(tmp_path)
    with Tiles(path) as tiles:
        tiles.sample(2.5,12.5)
        raster=tmp_path/"x0_y0/surface.tif"
        with raster.open("ab") as stream:stream.write(b"changed")
        with pytest.raises(TileContractError,match="changed"):
            tiles.sample(2.5,12.5)
        # close detects manifest ownership, not modified source validation.
    with pytest.raises(TileContractError,match="size changed"):
        Tiles(path)


def test_raster_crs_and_missing_vertical_reference_rejected(tmp_path):
    path=collection(tmp_path)
    metadata=tmp_path/"x0_y0/tile.json";m=json.loads(metadata.read_text());m["vertical_reference"]="ellipsoid";_write(metadata,m)
    with pytest.raises(TileContractError,match="vertical"):
        Tiles(path)


def test_symlink_escape_rejected(tmp_path):
    path=collection(tmp_path/"input")
    outside=tmp_path/"foreign.json";outside.write_text("{}")
    link=path.parent/"foreign.json";link.symlink_to(outside)
    m=json.loads(path.read_text());m["tiles"][0]["path"]="foreign.json";_write(path,m)
    with pytest.raises(TileContractError,match="escaped"):
        Tiles(path)


def test_deadline_and_shared_ray_and_cell_work_caps(tmp_path):
    with Tiles(collection(tmp_path)) as tiles:
        a,b=(2.5,12.5,1.7),(37.5,12.5,1.7)
        work=WorkBudget(max_rays=1)
        assert tiles.ray(a,b,work=work)["state"]=="visible"
        assert tiles.ray(a,b,work=work)["reason"]=="geometry_ray_limit"
        assert work.rays==1
        assert tiles.ray(a,b,work=WorkBudget(max_cells=2))["reason"]=="geometry_cell_limit"
        assert tiles.ray(a,b,deadline=time.monotonic()-1)["reason"]=="geometry_deadline"
        with pytest.raises(ValueError):WorkBudget(max_rays=10001)


def test_wider_inventory_does_not_raise_independent_ray_distance_limit(tmp_path):
    with Tiles(collection(tmp_path)) as tiles:
        work=WorkBudget()
        result=tiles.ray((2.5,12.5,1.7),(15002.5,12.5,1.7),work=work)
        assert result['state']=='unknown'
        assert result['reason']=='maximum_modeled_distance_exceeded'
        assert result['maximum_modeled_distance_m']==10000
        assert work.rays==0 and work.cells==0


def test_matches_existing_reference_for_random_columns_and_subcells(tmp_path):
    rng=np.random.default_rng(20260912)
    surface=rng.uniform(0,10,(8,8)).astype(np.float32)
    path=collection(tmp_path,surface=surface,tiles_y=2)
    with Tiles(path) as tiles:
        for _ in range(30):
            a=(*rng.uniform(.1,39.9,2),float(rng.uniform(11,25)))
            b=(*rng.uniform(.1,39.9,2),float(rng.uniform(11,25)))
            expected=reference_los(surface,(0,5,0,40,0,-5),a,b)
            assert tiles.ray(a,b)["state"]==("visible" if expected else "blocked")


def test_surface_contract_mismatch_never_treated_as_visible(tmp_path):
    surface=np.zeros((4,8));surface[1,4]=2
    with Tiles(collection(tmp_path,surface=surface,occupancy=np.zeros((4,8)))) as tiles:
        assert tiles.ray((2.5,12.5,3),(37.5,12.5,3))["reason"]=="surface_contract_violation"


def test_no_data_sentinel_and_mask_are_not_altitude(tmp_path):
    terrain=np.zeros((4,8));terrain[1,4]=-9999
    with Tiles(collection(tmp_path,terrain=terrain)) as tiles:
        assert tiles.ray((2.5,12.5,3),(37.5,12.5,3))["reason"]=="terrain_unsupported"


def test_uncertainty_preserves_invalid_and_whole_base_dependency(tmp_path):
    database=tmp_path/"synthetic.sqlite"
    db=sqlite3.connect(database)
    db.execute("create table buildings(fid integer,geom blob,invalid_geometry integer,source text,source_id text)")
    db.execute("create table gpkg_geometry_columns(table_name text,column_name text,srs_id integer)")
    db.execute("insert into gpkg_geometry_columns values('buildings','geom',5186)")
    db.execute("create virtual table rtree_buildings_geom using rtree(id,minx,maxx,miny,maxy)")
    db.executemany("insert into buildings values(?,?,?,?,?)",[(1,b"fixture",1,"synthetic","A"),(2,b"fixture",0,"synthetic","B")])
    db.executemany("insert into rtree_buildings_geom values(?,?,?,?,?)",[(1,0,3,0,3),(2,100,130,0,30)])
    db.commit();db.close()
    anomaly=tmp_path/"overlap.json"
    _write(anomaly,{"schema_version":1,"coordinate_crs":"EPSG:5186","recipe_sha256":"a"*64,
                    "anomalies":[{"coordinate_epsg5186":[102.5,12.5]}]})
    result=inspect_uncertainty(database,anomaly)
    assert result["report"]["flagged_building_count"]==1
    assert result["report"]["overlap_dependent_building_count"]==1
    assert any(r.get("fid")==2 and r["bounds"]==[95,-5,135,35] for r in result["regions"])
    assert result["report"]["repairs_written"]==0


def test_directional_horizon_evaluates_full_column_near_edge(tmp_path):
    surface=np.zeros((4,8));surface[1,4]=10
    with Tiles(collection(tmp_path,surface=surface)) as tiles:
        horizon=tiles.horizon((2.5,12.5,1.7),90,35,curvature_coefficient=0)
        assert horizon["state"]=="supported"
        assert horizon["maximum_angle_deg"]==pytest.approx(math.degrees(math.atan2(8.3,17.5)))
        assert horizon["distant_horizon_verified"] is False
        assert horizon["supported_until_m"]==35


def test_directional_horizon_unknown_stays_partial_even_after_known_high_wall(tmp_path):
    surface=np.zeros((4,8));surface[1,3]=10
    quality=np.full((4,8),3);quality[1,6]=11
    with Tiles(collection(tmp_path,surface=surface,quality=quality)) as tiles:
        horizon=tiles.horizon((2.5,12.5,1.7),90,35,curvature_coefficient=0)
        assert horizon["state"]=="unknown"
        assert horizon["maximum_angle_deg"] is None
        assert horizon["partial_maximum_angle_deg"]>0
        assert horizon["supported_until_m"]==pytest.approx(27.5)


def test_horizon_outside_collection_and_deadline_do_not_invent_horizon(tmp_path):
    with Tiles(collection(tmp_path)) as tiles:
        horizon=tiles.horizon((2.5,12.5,1.7),90,100)
        assert horizon["reason"]=="missing_tile"
        assert horizon["supported_until_m"]==pytest.approx(37.5)
        assert tiles.horizon((2.5,12.5,1.7),90,35,deadline=time.monotonic()-1)["reason"]=="geometry_deadline"
