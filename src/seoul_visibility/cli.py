"""Command line entry points; heavy dependencies are imported only as needed."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "tolist"):
        return value.tolist()
    if hasattr(value, "item"):
        return value.item()
    raise TypeError(f"Cannot encode {type(value).__name__}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepared 2.5D point-visibility screening for Seoul")
    commands = parser.add_subparsers(dest="command", required=True)
    inspect_parser = commands.add_parser("inspect", help="Inspect local rasters/vector layers and environment without modifying sources")
    inspect_parser.add_argument("paths", nargs="+")
    inspect_parser.add_argument("--encoding", help="Explicit SHP encoding override, e.g. CP949; otherwise inspect sidecar declarations")
    inspect_parser.add_argument("--output", type=Path, help="Optional JSON inspection report")
    for command in ("plan", "prepare"):
        child = commands.add_parser(command, help=f"{command.title()} explicitly configured preprocessing")
        child.add_argument("config", type=Path)
    synthetic = commands.add_parser("synthetic", help="Generate deterministic FICTIONAL terrain/building raster fixtures")
    synthetic.add_argument("--output", required=True, type=Path)
    synthetic.add_argument("--size-m", type=float, default=2400)
    synthetic.add_argument("--resolution", type=float, choices=(2, 5), default=5)
    synthetic.add_argument("--seed", type=int, default=1729)
    synthetic.add_argument("--project-data-root", type=Path)
    query = commands.add_parser("query", help="Compute one target-centred native viewshed from prepared rasters")
    query.add_argument("manifest", type=Path)
    query.add_argument("--lon", type=float, required=True)
    query.add_argument("--lat", type=float, required=True)
    query.add_argument("--height", type=float, required=True)
    query.add_argument("--height-reference", choices=("agl", "absolute"), required=True)
    query.add_argument("--vertical-reference", help="Required matching datum description for absolute heights")
    query.add_argument("--radius", type=float, default=5000)
    query.add_argument("--eye-height", type=float, default=1.7)
    query.add_argument("--resolution", type=float, choices=(2, 5), default=5)
    query.add_argument("--curvature", type=float, default=6 / 7)
    query.add_argument("--no-cache", action="store_true")
    query.add_argument("--offline", action="store_true", help="Allow radii beyond the interactive limit after resource preflight")
    query.add_argument("--output", type=Path, help="Optional states GeoTIFF; export is outside core query timing")
    query.add_argument("--project-data-root", type=Path, help="Explicit export budget root; include prepared data, raw sources and outputs")
    bench = commands.add_parser("benchmark", help="Measure real native queries on an existing prepared product")
    bench.add_argument("manifest", type=Path)
    bench.add_argument("--output", required=True, type=Path, help="Report directory containing benchmark.json and runs.csv")
    bench.add_argument("--runs", type=int, default=30, help="Timed warm uncached runs PER radius/resolution (minimum 30 for a useful report)")
    bench.add_argument("--radii", type=float, nargs="+", default=[1000, 3000, 5000, 10000])
    bench.add_argument("--seed", type=int, default=1729)
    bench.add_argument("--target-height", type=float, default=120.0, help="Explicit benchmark target height above DTM")
    bench.add_argument("--resolution", type=float, nargs="+", default=[5])
    bench.add_argument("--keep-export", action="store_true", help="Retain this benchmark's optional sample export")
    bench.add_argument("--profile-block-cache", action="store_true", help="Instead profile 64/128/256 MiB GDAL caches at 5 km; --output is a JSON file")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "inspect":
            from .inspect import inspect_paths

            result = inspect_paths(args.paths, **({"encoding": args.encoding} if args.encoding else {}))
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                with args.output.open("x") as report:
                    report.write(json.dumps(result, indent=2, default=_json_default) + "\n")
        elif args.command == "plan":
            from .prepare import plan

            result = plan(args.config)
        elif args.command == "prepare":
            from .prepare import prepare

            result = {"manifest": str(prepare(args.config))}
        elif args.command == "synthetic":
            from .synthetic import create_synthetic

            path = create_synthetic(args.output, size_m=args.size_m, resolution_m=args.resolution, seed=args.seed, project_data_root=args.project_data_root)
            result = {"manifest": str(path), "source_kind": "synthetic", "warning": "Fictional terrain and buildings; no real Seoul accuracy claim."}
        elif args.command == "query":
            import numpy as np

            from . import TargetPoint, VisibilityEngine

            target = TargetPoint(lon=args.lon, lat=args.lat, height_m=args.height, height_reference=args.height_reference, vertical_reference=args.vertical_reference)
            with VisibilityEngine.from_manifest(args.manifest) as engine:
                viewshed = engine.visible_from_target(target, radius_m=args.radius, eye_height_m=args.eye_height, resolution_m=args.resolution, curvature_coefficient=args.curvature, use_cache=not args.no_cache, offline=args.offline)
                counts = np.bincount(viewshed.states.ravel(), minlength=4)
                result = {
                    "metadata": viewshed.metadata,
                    "timings": viewshed.timings,
                    "shape": list(viewshed.states.shape),
                    "state_counts": dict(zip(("blocked", "visible", "excluded", "unknown"), counts.tolist())),
                }
                if args.output:
                    result["export"] = str(viewshed.export_geotiff(args.output, project_data_root=args.project_data_root))
        elif args.command == "benchmark":
            from .benchmark import profile_block_cache, run_benchmark

            if args.profile_block_cache:
                measured = profile_block_cache(args.manifest, args.output, runs=args.runs, seed=args.seed)
                result = {"report": str(args.output), "profiles": [{key: p[key] for key in ("gdal_block_cache_mib", "summary", "peak_sampled_rss_bytes")} for p in measured["profiles"]]}
            else:
                result = run_benchmark(args.manifest, args.output, runs=args.runs, radii_m=args.radii, resolutions_m=args.resolution, seed=args.seed, target_height_m=args.target_height, keep_export=args.keep_export)
        else:
            raise AssertionError(args.command)
        print(json.dumps(result, indent=2, default=_json_default, allow_nan=False))
        return 0
    except (ValueError, RuntimeError, OSError, KeyError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, default=_json_default), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
