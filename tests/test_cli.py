from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np
from osgeo import gdal
from pyproj import Transformer
import pytest

from seoul_visibility.benchmark import run_benchmark
from seoul_visibility.cli import main
from seoul_visibility.errors import ResourceBudgetError
from seoul_visibility.resources import StoragePolicy
from seoul_visibility.synthetic import create_synthetic


def _center(manifest: Path) -> tuple[float, float]:
    product = json.loads(manifest.read_text())["products"]["5"]
    xmin, ymin, xmax, ymax = product["bounds"]
    return Transformer.from_crs(5186, 4326, always_xy=True).transform((xmin + xmax) / 2, (ymin + ymax) / 2)


def test_synthetic_is_deterministic_reusable_and_aligned(tmp_path):
    first = create_synthetic(tmp_path / "first", size_m=120)
    second = create_synthetic(tmp_path / "second", size_m=120)
    a = json.loads(first.read_text())
    b = json.loads(second.read_text())
    assert a["data_version"] == b["data_version"]
    assert a["source_kind"] == "synthetic"
    assert "not real Seoul" in a["vertical_reference"]
    product = a["products"]["5"]
    for key in ("dtm", "surface", "occupancy", "quality"):
        ad = gdal.Open(str(first.parent / product[key]))
        bd = gdal.Open(str(second.parent / product[key]))
        np.testing.assert_array_equal(ad.ReadAsArray(), bd.ReadAsArray())
        assert list(ad.GetGeoTransform()) == product["transform"]
        assert ad.RasterXSize == product["width"]
        assert ad.GetRasterBand(1).GetBlockSize() == [256, 256]
    before = first.stat().st_mtime_ns
    assert create_synthetic(first.parent, size_m=120) == first
    assert first.stat().st_mtime_ns == before
    with pytest.raises(FileExistsError):
        create_synthetic(first.parent, size_m=120, seed=3)


def test_synthetic_refuses_budget_and_cleans_own_failed_staging(tmp_path, monkeypatch):
    with pytest.raises(ResourceBudgetError):
        create_synthetic(tmp_path / "too_large", size_m=120, policy=StoragePolicy(total_budget_bytes=1, minimum_free_bytes=0))
    assert not (tmp_path / "too_large").exists()
    unrelated = tmp_path / "keep.txt"
    unrelated.write_text("preserve me")

    def failure(*args):
        raise RuntimeError("injected terrain failure")

    monkeypatch.setattr("seoul_visibility.synthetic._terrain", failure)
    with pytest.raises(RuntimeError, match="injected"):
        create_synthetic(tmp_path / "failure", size_m=120)
    assert not (tmp_path / "failure").exists()
    assert not list(tmp_path.glob(".synthetic-*"))
    assert unrelated.read_text() == "preserve me"


@pytest.mark.parametrize("size", [0, -5, float("nan"), float("inf")])
def test_synthetic_invalid_size(tmp_path, size):
    with pytest.raises(ValueError, match="size_m"):
        create_synthetic(tmp_path / "bad", size_m=size)


def test_cli_synthetic_inspect_query_and_errors(tmp_path, capsys):
    output = tmp_path / "fixture"
    assert main(["synthetic", "--output", str(output), "--size-m", "240"]) == 0
    assert json.loads(capsys.readouterr().out)["source_kind"] == "synthetic"
    manifest = output / "manifest.json"
    lon, lat = _center(manifest)
    assert main(["inspect", str(output / "dtm.tif")]) == 0
    inspection = json.loads(capsys.readouterr().out)
    assert inspection
    exported = output / "visibility.tif"
    query = ["query", str(manifest), "--lon", str(lon), "--lat", str(lat), "--height", "120", "--height-reference", "agl", "--radius", "20"]
    assert main([*query, "--output", str(exported)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert sum(result["state_counts"].values()) == np.prod(result["shape"])
    assert exported.is_file()
    ds = gdal.Open(str(exported))
    assert ds.GetRasterBand(1).DataType == gdal.GDT_Byte
    assert ds.GetRasterBand(1).GetNoDataValue() == 3
    assert main([*query, "--radius", "0"]) == 2
    error = json.loads(capsys.readouterr().err)
    assert "positive" in error["message"]


def test_benchmark_smoke_streams_real_timings_and_cleans_export(tmp_path):
    manifest = create_synthetic(tmp_path / "fixture", size_m=240)
    summary = run_benchmark(manifest, tmp_path / "report", runs=2, radii_m=[20, 5000], resolutions_m=[5, 2])
    report = json.loads(Path(summary["report"]).read_text())
    assert report["source_kind"] == "synthetic"
    assert len(report["groups"]) == 1
    group = report["groups"][0]
    assert group["warm_uncached"]["elapsed_s"]["count"] == 2
    assert group["exact_cache_hit"]["count"] == 2
    assert group["useful_sample_count"] is False
    assert report["skipped"]
    assert report["startup"]["process_launch_and_import_s"] > 0
    assert report["one_time"]["preprocessing_recorded_s"] > 0
    assert report["prepared_data_disk_bytes"] > 0
    assert not list(manifest.parent.glob(".visibility-benchmark-*.tif"))
    with Path(summary["runs_csv"]).open() as stream:
        rows = list(csv.DictReader(stream))
    assert [row["phase"] for row in rows] == ["warm_uncached", "warm_uncached", "exact_cache_hit", "exact_cache_hit"]
    assert all(row["cache_status"] == "disabled" for row in rows[:2])
    assert all(row["cache_status"] == "memory_hit" for row in rows[2:])
    with pytest.raises(FileExistsError):
        run_benchmark(manifest, tmp_path / "report", runs=1, radii_m=[20])


def test_cli_plan_prepare_and_query_from_explicit_config(tmp_path, capsys):
    source = create_synthetic(tmp_path / "source", size_m=240)
    manifest = json.loads(source.read_text())
    product = manifest["products"]["5"]
    raw = source.parent / product["dtm"]
    original = raw.read_bytes()
    configuration = {
        "data_root": str(tmp_path), "output_dir": str(tmp_path / "prepared"),
        "crs": manifest["crs"], "vertical_reference": manifest["vertical_reference"],
        "resolution_m": 5, "bounds": product["bounds"], "source_kind": "synthetic",
        "terrain": {"kind": "raster", "path": str(raw), "units": "m", "bare_earth_verified": True,
                    "vertical_reference": manifest["vertical_reference"]},
        "buildings": {"assume_empty": True, "justification": "Explicit synthetic terrain-only CLI fixture",
                      "coverage_bounds": product["bounds"]},
    }
    config = tmp_path / "config.json"
    config.write_text(json.dumps(configuration))
    assert main(["plan", str(config)]) == 0
    planned = json.loads(capsys.readouterr().out)
    assert planned["grid"]["transform"] == product["transform"]
    assert main(["prepare", str(config)]) == 0
    prepared = Path(json.loads(capsys.readouterr().out)["manifest"])
    assert prepared.exists()
    assert raw.read_bytes() == original
    lon, lat = _center(prepared)
    assert main(["query", str(prepared), "--lon", str(lon), "--lat", str(lat), "--height", "120", "--height-reference", "agl", "--radius", "20"]) == 0
    assert json.loads(capsys.readouterr().out)["state_counts"]["visible"] > 0


def test_cli_inspection_report_never_overwrites_source(tmp_path, capsys):
    manifest = create_synthetic(tmp_path / "source", size_m=120)
    source = manifest.parent / "dtm.tif"
    original = source.read_bytes()
    assert main(["inspect", str(source), "--output", str(source)]) == 2
    error = json.loads(capsys.readouterr().err)
    assert error["error"] == "FileExistsError"
    assert source.read_bytes() == original
    report = tmp_path / "inspection.json"
    assert main(["inspect", str(source), "--output", str(report)]) == 0
    capsys.readouterr()
    assert json.loads(report.read_text())["sources"]
