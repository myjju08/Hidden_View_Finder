"""Run with .venv/bin/python examples/usage.py from the repository root."""
from seoul_visibility import TargetPoint, VisibilityEngine
from seoul_visibility.synthetic import create_synthetic

manifest = create_synthetic('data/usage', size_m=2400)
with VisibilityEngine.from_manifest(manifest) as engine:
    target = TargetPoint(127.00002829110686, 37.54954050092772, 120.0, 'agl')
    result = engine.visible_from_target(target, radius_m=1000)
    print({'shape': result.states.shape, 'timings': result.timings,
           'target': result.metadata['target'], 'source_kind': result.metadata['source_kind']})
    observers = result.sample_coordinates(max_points=8, seed=42)
    print(engine.check_observers(target, observers).states.tolist())
    cached = engine.visible_from_target(target, radius_m=1000)
    print({'repeat': cached.metadata['cache_status'], 'timings': cached.timings})
