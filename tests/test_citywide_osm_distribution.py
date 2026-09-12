"""Synthetic .poly fixtures; no network or country downloads."""
import pytest
from shapely.geometry import Point
from scripts.data.osm_distribution import parse_poly


def test_extraction_polygon_keeps_holes():
    geometry=parse_poly('synthetic\n1\n0 0\n4 0\n4 4\n0 4\n0 0\nEND\n!1\n1 1\n2 1\n2 2\n1 2\n1 1\nEND\nEND\n')
    assert geometry.area==15 and geometry.covers(Point(.5,.5))
    assert not geometry.covers(Point(1.5,1.5))


@pytest.mark.parametrize('text',['<html>error</html>','x\n1\n0 0\n1 0\n1 1\nEND\nEND','x\n1\n200 0\n0 0\n0 1\n200 0\nEND\nEND'])
def test_extraction_polygon_rejects_html_truncation_and_unexplained_crs(text):
    with pytest.raises(ValueError):parse_poly(text)
