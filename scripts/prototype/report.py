#!/usr/bin/env python3
"""Refresh small local readiness/storage reports; no acquisition or provider calls."""
from collections import defaultdict
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import json
import os
import stat

from hidden_view_finder.prototype.runtime import ROOT, artifact_usage, budget, config
from hidden_view_finder.prototype.ai import PrototypeAI
from seoul_visibility.acquisition_safety import atomic_json


def read(name):
    path = ROOT / 'reports/prototype' / name
    if path.stat().st_size > 2_000_000:
        raise ValueError('Aggregate report exceeds bounded metadata size')
    return json.loads(path.read_text())


def relative(value):
    if isinstance(value, dict):
        return {key: relative(item) for key, item in value.items()}
    if isinstance(value, list):
        return [relative(item) for item in value]
    return value.replace(str(ROOT), '.') if isinstance(value, str) else value


def inventory():
    prefixes = [
        ('source_package', 'data/citywide/packages'),
        ('prepared_rasters', 'data/citywide/prepared'),
        ('raw_geography', 'data/citywide/raw'),
        ('normalized_development', 'data/citywide/normalized'),
        ('existing_gis_dependencies', 'data/citywide/dependencies'),
        ('prototype_dependencies', 'data/prototype/dependencies'),
        ('bundled_map_font_assets', 'src/hidden_view_finder/static/vendor'),
        ('all_staging', 'data/citywide/staging'),
        ('prototype_runtime_artifacts', 'data/prototype'),
    ]
    entries = []
    stack = [ROOT]
    while stack:
        path = stack.pop()
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        name = str(path.relative_to(ROOT))
        index = next((i for i, (_, prefix) in enumerate(prefixes)
                      if name == prefix or name.startswith(prefix + '/')), len(prefixes))
        entries.append((index, name, info))
        if stat.S_ISDIR(info.st_mode):
            stack.extend(path.iterdir())
    seen = set()
    categories = defaultdict(lambda: {'logical_bytes': 0, 'allocated_bytes': 0,
                                      'accounted_bytes': 0, 'inodes': 0})
    for index, name, info in sorted(entries, key=lambda row: (row[0], row[1])):
        identity = (info.st_dev, info.st_ino)
        if identity in seen:
            continue
        seen.add(identity)
        category = prefixes[index][0] if index < len(prefixes) else 'checkout_other'
        row = categories[category]
        allocated = info.st_blocks * 512
        row['logical_bytes'] += info.st_size
        row['allocated_bytes'] += allocated
        row['accounted_bytes'] += max(info.st_size, allocated)
        row['inodes'] += 1
    return dict(categories)


def main():
    c = config()
    b = budget(c)
    with b.reserve(1_000_000, 1_000_000, 'prototype bounded aggregate reports'):
        startup = read('startup.json')
        tests = read('tests-final.json')
        browser = read('browser-validation.json')
        demos = read('demonstrations.json')
        inputs = read('input-inspection.json')
        recovery = read('safety-recovery.json')
        provider = PrototypeAI(c['ai'], b, Path(c['runtime_root']))
        categories = inventory()
        snapshot = b.check()
        receipt_bytes = (read('dependencies.json')['wheel_bytes']
                         + read('browser-dependencies.json')['npm']['bytes']
                         + read('browser-dependencies.json')['browser']['bytes']
                         + read('firefox-dependencies.json')['archive_bytes']
                         + read('font.json')['bytes']
                         + sum(p['bytes'] for name in ('browser-native.json', 'browser-native-firefox.json')
                               for p in read(name)['packages']))
        # Leaflet has its own receipt; probe/metadata/retry traffic was not metered.
        leaflet = read('leaflet.json')
        receipt_bytes += sum(item['bytes'] for item in leaflet.get('assets', []))
        observed_at = datetime.now(timezone.utc).isoformat()
        storage = {
            'observed_at_utc': observed_at,
            'mandatory_total_ceiling_bytes': 20_000_000_000,
            'effective_total_ceiling_bytes': b.limit,
            'initial_development_snapshot': startup['snapshot'],
            'final_snapshot': snapshot,
            'prototype_growth_bytes': snapshot['accounted_bytes'] - startup['snapshot']['accounted_bytes'],
            'categories': categories,
            'category_policy': 'Disjoint inode accounting including directories; cross-category hard links assigned to first listed category. Snapshot also includes external allowance. Report publication adds a few KiB afterward.',
            'core_source_and_raster_accounted_bytes': sum(categories[name]['accounted_bytes'] for name in ('source_package', 'prepared_rasters')),
            'artifact_usage': artifact_usage(c),
            'dependency_network_receipt_bytes_lower_bound': receipt_bytes,
            'network_measurement': 'Known retained dependency/assets receipts only; small metadata/probe/retry bytes were not fully metered. Geographic downloads and live paid-provider calls: zero.',
            'persistent_scene_cache_bytes': 0,
            'generated_image_cache_bytes': provider.cache.used_bytes(),
            'map_derivative_bytes': 0,
            'peak_measurements': {
                'last_full_test_sampled_peak_accounted_bytes': tests['storage']['observed_peak_accounted_bytes'],
                'last_full_test_sampled_peak_staging_bytes': tests['storage']['observed_peak_temporary_bytes'],
                'browser_sampled_peak_combined_artifacts_bytes': read('browser-run.json')['sampled_peak_browser_artifact_bytes'],
                'development_reserved_peak_upper_bound_bytes': recovery['previous_reservation']['baseline_bytes'] + recovery['previous_reservation']['peak_bytes'],
                'development_reserved_staging_upper_bound_bytes': recovery['previous_reservation']['temporary_baseline_bytes'] + recovery['previous_reservation']['temporary_bytes'],
                'limitation': 'Sampled and reserved bounds are distinct. The initial development guard lost its final in-memory peak counters on a safe fixture-symlink refusal; continuous peak telemetry is not claimed. Later browser/tests used independent shared reservations.',
            },
            'cleanup': {'source_files_deleted': 0, 'manual_fixture_links_deleted': 2,
                        'receipt': 'reports/prototype/safety-recovery.json',
                        'browser_profiles': 'Only task-owned disposable profiles auto-removed by Playwright; logs/screenshots/archives retained.'},
            'os_quota_verified': False,
            'shared_disk_limitation': 'Application reservations and polling do not constrain unrelated filesystem writers. No quotas or mounts were changed.',
        }
        readiness = {
            'observed_at_utc': observed_at,
            'report_scope': 'Aggregate of recorded validations plus current storage/provider configuration. Does not rerun tests/source validation; application startup independently verifies inputs.',
            'source_package': inputs['package_id'],
            'source_readiness': inputs['readiness_preserved'],
            'geographic_ready': False,
            'visibility_ready': False,
            'local_exploration_available': inputs['tile_adapter']['local_exploration_available'],
            'public_deployment_status': c['public_deployment_status'],
            'tests': tests['test_summary'],
            'browser_status': browser['status'],
            'district_outcomes': demos['citywide_status_counts'],
            'application_geometry_version': demos['demonstrations'][0]['views'][0]['versions']['geometry'],
            'config_sha256': sha256((ROOT/'configs/prototype.json').read_bytes()).hexdigest(),
            'recorded_validation_live_paid_provider_calls': browser['live_paid_provider_calls'],
            'current_provider_status': provider.status(),
            'remaining_limitations': ['Missing compatible surrounding terrain', 'Water-surface elevation unsupported',
                'Seven source-invalid building flags quarantined', 'Incomplete relation/source readiness preserved',
                'Estimated roofs and mapped terrain samples; canopy/full facades/field scenery not verified',
                'Full-response five-second performance goal not met', 'Actual published-layer licence review required'],
            'start_command': 'bash scripts/prototype/python.sh scripts/prototype/run.py serve --port 8000',
            'resume_validation_command': 'bash scripts/prototype/python.sh scripts/prototype/benchmark.py',
            'acquisition_rerun': False,
            'field_verified': False,
        }
        atomic_json(ROOT/'reports/prototype/storage-final.json', relative(storage), b)
        atomic_json(ROOT/'reports/prototype/readiness.json', readiness, b)
        print(json.dumps({'accounted_bytes': snapshot['accounted_bytes'],
                          'free_bytes': min(fs['free_bytes'] for fs in snapshot['filesystems']),
                          'readiness_report': 'reports/prototype/readiness.json'}))


if __name__ == '__main__':
    main()
