#!/usr/bin/env python3
"""Generate a PNG from an exported recommendation or prototype scene JSON."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from hidden_view_finder.scene_image import (
    ImageError, SceneImageGenerator, build_scene, build_prompt, load_image_env,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--output', type=Path, default=Path('anticipated-view.png'))
    parser.add_argument('--candidate-id', help='Choose a recommendation/unverified/view ID; defaults to the first recommendation')
    parser.add_argument('--env-file', type=Path, default=Path('.env'))
    parser.add_argument('--dry-run', action='store_true', help='Write scene/prompt JSON only; no API call or key needed')
    args = parser.parse_args()
    try:
        if args.input.stat().st_size > 10 * 1024 * 1024:
            parser.error('Input JSON must be smaller than 10 MiB')
        data = json.loads(args.input.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            parser.error('Input must be a JSON object')
        candidates = data.get('recommendations', []) + data.get('unverified', []) + data.get('views', [])
        if args.candidate_id:
            candidate = next((item for item in candidates if item.get('id', item.get('view_id')) == args.candidate_id), None)
            if candidate is None:
                parser.error('Candidate ID is not in this result')
        else:
            candidate = candidates[0] if candidates else data
        scene = build_scene(candidate, data.get('landmarks', []),
                            data.get('request_summary', {}).get('request', {}))
        metadata = {'scene': scene, 'prompt': build_prompt(scene), 'status': 'not_generated'}
        if not args.dry_run:
            load_image_env(args.env_file)
            result = SceneImageGenerator().generate(scene)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_bytes(result.pop('png'))
            metadata.update(result, status='generated')
        sidecar = args.output.with_suffix('.json')
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'Saved {sidecar}' if args.dry_run else f'Saved {args.output} and {sidecar}')
    except ImageError as error:
        parser.exit(1, f'{error.code}: {error}\n')


if __name__ == '__main__':
    main()
