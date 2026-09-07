"""The example generator must preserve input files and declare its fiction."""

import hashlib
from pathlib import Path
import runpy

from osgeo import gdal
import pytest


def test_fixture_generator_schema_encoding_and_no_clobber(tmp_path):
    script = Path(__file__).resolve().parents[1] / 'examples' / 'make_fixture_inputs.py'
    generate = runpy.run_path(str(script))['generate']
    root = tmp_path / 'data'
    report = generate(root)
    assert report['source_kind'] == 'synthetic'
    source = gdal.OpenEx(str(root / 'raw-example' / 'fictional-buildings.shp'),
                         gdal.OF_VECTOR | gdal.OF_READONLY, open_options=['ENCODING=CP949'])
    layer = source.GetLayer(0)
    assert layer.GetFeatureCount() == 6
    first = next(iter(layer))
    assert first.GetField('SYN_NAME') == '가상건물 1'
    assert first.GetField('SYN_H_M') == 40
    source = None
    source_paths = sorted(p for p in root.rglob('*') if p.is_file())
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths}
    with pytest.raises(FileExistsError, match='Refusing to overwrite'):
        generate(root)
    assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths} == before
    assert not list(root.glob('.fixture-inputs-*'))
    assert not (root / '.fixture-inputs.lock').exists()
