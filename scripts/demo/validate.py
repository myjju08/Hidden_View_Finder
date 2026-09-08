#!/usr/bin/env python3
"""Record aggregate demo timings; no source datasets or candidate exports.

python scripts/demo/validate.py --output data/demo/validation.json
.venv/bin/python scripts/demo/validate.py --seoul --output data/demo/validation.json
Warm runs do not flush OS caches. Seoul reruns may hit the engine's viewshed cache;
this script never labels them uncached native visibility benchmarks.
"""
from __future__ import annotations
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import resource
import statistics
import sys
import time
from datetime import datetime, timezone

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
start_import = time.perf_counter()
from hidden_view_finder.service import DemoService
from hidden_view_finder.scenarios import default_request
IMPORT_SECONDS = time.perf_counter() - start_import


def compact(result):
    return {"counts": {key: len(result[key]) for key in ("recommendations", "unverified", "excluded", "duplicates")},
            "top_k": [{"id": c["id"], "score": c["score"], "evidence_coverage": c["evidence_coverage"],
                       "image_status": c["image"]["status"]} for c in result["recommendations"]],
            "service_seconds": result["timing_s"],
            "visibility_summary": result.get("visibility_summary")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=REPO / "data/demo/validation.json")
    parser.add_argument("--seoul", action="store_true")
    args = parser.parse_args()
    init = time.perf_counter()
    service = DemoService(REPO / "data", online_weather=False)
    initialization = time.perf_counter() - init
    report = {"created_at": datetime.now(timezone.utc).isoformat(),
              "environment": {"os": platform.platform(), "python": platform.python_version(),
                              "cpu": platform.processor(), "logical_cpus": os.cpu_count(),
                              "storage_type": "unknown container overlay"},
              "packages": {}, "module_import_seconds": IMPORT_SECONDS,
              "service_initialization_seconds": initialization,
              "timing_policy": "In-process service including evidence ranking/presentation. No HTTP, browser, export or network forecast. No OS cache flushing. No recommendation result cache exists."}
    for name in ("numpy", "GDAL", "pyproj", "pytest"):
        try:
            report["packages"][name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            report["packages"][name] = None
    try:
        report["scenario_first"] = compact(service.recommend(default_request()))
        runs = []
        for i in range(30):
            request = default_request()
            request["max_travel_minutes"] = 45 + i / 100
            started = time.perf_counter()
            result = service.recommend(request)
            runs.append(time.perf_counter() - started)
            assert len(result["recommendations"]) == 3
        report["scenario_warm"] = {"run_count": len(runs), "seconds": runs,
            "median_s": statistics.median(runs), "p95_s": sorted(runs)[28],
            "sampling": "Fixed fictional scene; deterministic travel-limit perturbation by index/100 min. Not spatial target sampling."}
        if args.seoul:
            if not service.seoul.available:
                raise SystemExit("Seoul preparation or GIS environment unavailable; no synthetic substitution")
            request = dict(default_request(), mode="seoul", start=service.bootstrap()["seoul_start"])
            report["seoul_first"] = compact(service.recommend(request))
            report["seoul_repeated"] = compact(service.recommend(request))
            report["seoul_timing_policy"] = "Two integration smoke runs, not a median/p95 benchmark. Fixed approximate N Seoul Tower point, 1.8 km radius / 5 m; repeated run may reuse the native result cache. Weather disabled."
        report["peak_rss_mib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 if sys.platform != "darwin" else 1024**2)
        report["generated_asset_bytes"] = sum(p.stat().st_size for p in (REPO / "src/hidden_view_finder/static/images").glob("*.png"))
    finally:
        service.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(args.output)
    print(json.dumps({"output": str(args.output), "scenario_median_s": report["scenario_warm"]["median_s"],
                      "scenario_p95_s": report["scenario_warm"]["p95_s"], "peak_rss_mib": report["peak_rss_mib"]}))


if __name__ == "__main__":
    main()
