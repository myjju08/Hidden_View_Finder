"""Query the locally prepared real Seoul product; run from the repository root."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from seoul_visibility import State, TargetPoint, VisibilityEngine


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path,
        default=Path('data/seoul/processed/central-gba-maximum/manifest.json'))
    parser.add_argument('--radius', type=float, default=5000)
    parser.add_argument('--output', type=Path, help='Optional new GeoTIFF path inside the data budget root')
    args = parser.parse_args()
    # Explicit hypothetical point 100 m above bare earth near Gwanghwamun.
    # This is not a surveyed height of a named building or its rooftop.
    target = TargetPoint(126.9777, 37.578, 100.0, 'agl')
    with VisibilityEngine.from_manifest(args.manifest) as engine:
        result = engine.visible_from_target(target, radius_m=args.radius,
            eye_height_m=1.7, resolution_m=5, curvature_coefficient=6/7)
        print(json.dumps({
            'target': result.metadata['target'],
            'quality': result.metadata['quality'],
            'state_counts': {state.name.lower(): int(np.count_nonzero(result.states == state)) for state in State},
            'timings_s': result.timings,
            'limitations': result.metadata['limitations'],
        }, ensure_ascii=False, indent=2))
        if args.output:
            result.export_geotiff(args.output)


if __name__ == '__main__':
    main()
