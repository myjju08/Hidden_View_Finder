"""Synthetic tiny OSM fixtures for selected-member quarantine investigation."""
import sqlite3

from hidden_view_finder.prototype.osm_recovery import recover_shape, investigate, classify


def test_complete_multiway_outer_with_inner_hole():
    nodes={1:(0,0),2:(10,0),3:(10,10),4:(0,10),5:(2,2),6:(4,2),7:(4,4),8:(2,4)}
    ways={10:[1,2,3],11:[3,4,1],12:[5,6,7,8,5]}
    shape,info=recover_shape(ways,[(10,'outer'),(11,'outer'),(12,'inner')],nodes)
    assert info['status']=='complete_valid_geometry'
    assert info['holes']==1
    assert shape.area==96
    assert info['repairs_written']==0


def test_missing_members_not_guessed_closed_or_imputed():
    nodes={1:(0,0),2:(10,0),3:(10,10)}
    shape,info=recover_shape({10:[1,2,3,4,1]},[(10,'outer')],nodes)
    assert shape is None
    assert info['missing_node_ids']==[4]
    shape,info=recover_shape({},[(10,'outer')],nodes)
    assert shape is None
    assert info['missing_way_ids']==[10]


def test_open_ring_and_hole_outside_not_imported_as_public_space():
    nodes={1:(0,0),2:(10,0),3:(10,10),4:(0,10),5:(20,20),6:(21,20),7:(21,21)}
    shape,info=recover_shape({10:[1,2,3]},[(10,'outer')],nodes)
    assert shape is None
    assert info['status']=='ring_topology_invalid'
    shape,info=recover_shape({10:[1,2,3,4,1],11:[5,6,7,5]},[(10,'outer'),(11,'inner')],nodes)
    assert shape is None
    assert info['status']=='inner_ring_outside_outer'


def test_native_filters_recover_nested_relation_without_whole_country_cache(tmp_path):
    path=tmp_path/'synthetic.osm'
    path.write_text('''<osm version="0.6" generator="synthetic-test">
      <node id="1" lat="37.50" lon="127.0" version="1"/>
      <node id="2" lat="37.50" lon="127.01" version="1"/>
      <node id="3" lat="37.51" lon="127.01" version="1"/>
      <node id="4" lat="37.51" lon="127.0" version="1"/>
      <node id="999" lat="0" lon="0" version="1"/>
      <way id="10" version="1"><nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/></way>
      <way id="999" version="1"><nd ref="999"/><nd ref="1"/></way>
      <relation id="20" version="1"><member type="way" ref="10" role="outer"/><tag k="type" v="multipolygon"/></relation>
      <relation id="30" version="1"><member type="relation" ref="20" role="outer"/><tag k="type" v="multipolygon"/><tag k="natural" v="wood"/></relation>
      <relation id="31" version="1"><tag k="boundary" v="administrative"/><tag k="admin_level" v="8"/></relation>
    </osm>''')
    database=tmp_path/'synthetic.sqlite'
    db=sqlite3.connect(database);db.execute('create table quality(source_id text)')
    db.executemany('insert into quality values(?)',[('relation/30',),('relation/31',)]);db.commit();db.close()
    before=path.read_bytes()
    result=investigate(path,database,maximum_seconds=10)
    assert result['selected_node_count']==4
    assert result['recovered_node_count']==4
    assert result['selected_way_count']==1
    assert result['nested_relations_recovered']==[20]
    assert result['objects'][0]['status']=='complete_valid_geometry'
    assert result['excluded_source_ids']==['relation/30','relation/31']
    assert result['administrative_diagnostic_count']==1
    assert result['global_readiness_changed'] is False
    assert result['source_date_not_download_date'] is None
    assert path.read_bytes()==before
    assert len(result['quarantine_regions'])==1


def test_classification_does_not_make_absent_tags_public():
    assert classify({})=='other'
    assert classify({'natural':'wood'})=='woodland'
    assert classify({'waterway':'river'})=='water'
    assert classify({'boundary':'administrative','natural':'wood'})=='administrative'
