"""Hard regressions for the audited GDAL 3.8.4 native dependency geometry."""
import importlib.util
from pathlib import Path

from osgeo import gdal
import pytest


_path = Path(__file__).parents[1] / "scripts" / "probe_gdal_radial_dependencies.py"
_spec = importlib.util.spec_from_file_location("gdal_radial_probe", _path)
_probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_probe)
pytestmark = pytest.mark.skipif(gdal.VersionInfo("RELEASE_NAME") != "3.8.4",
                                reason="This proof is restricted to the inspected 3.8.4 source; other versions use square validation")


@pytest.mark.parametrize("radius,resolution", [(1., 5.), (5., 5.), (25., 5.), (99.9, 5.), (100., 5.), (103.75, 5.), (40., 2.)])
@pytest.mark.parametrize("curvature", [0., 6 / 7, 1.])
def test_outside_disk_fills_never_change_in_radius_cells(radius, resolution, curvature):
    result = _probe.perturbation_case(radius, resolution, curvature, (2, -1), "rough")
    assert all(item["inside_differences"] == 0 for item in result["comparisons"])


def test_near_boundary_obstacles_respect_dependency_direction():
    result = _probe.boundary_blocker_case()
    assert result["outside_ring_inside_differences"] == 0
    assert len(result["cardinal_boundary_receivers"]) == len(result["diagonal_boundary_receivers"]) == 4
