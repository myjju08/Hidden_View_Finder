#!/usr/bin/env python3
"""Acquire bounded real Jamsil map context; preserves the separate Namsan product.

Raw OSM JSON is cached verbatim under data/acquisition/demo-jamsil. This is
pedestrian/context data only; prepared terrain and obstruction rasters are separate.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
from pyproj import Geod
from shapely.geometry import LineString, Point, box, mapping
from shapely.ops import unary_union
from seoul_visibility.resources import preflight
from acquire_context import edge_access, polygon, write_atomic

BBOX = [127.077, 37.492, 127.128, 37.533]
RAW_CAP = 10 * 1024**2
CONTEXT_CAP = 12 * 1024**2
ALLOWED_HIGHWAYS = {'footway', 'path', 'steps', 'pedestrian', 'residential', 'living_street', 'service', 'unclassified', 'tertiary', 'secondary'}
ENDPOINTS = ['https://overpass.private.coffee/api/interpreter',
             'https://overpass-api.de/api/interpreter']
QUERIES = {
    'osm_tower': '[out:json][timeout:25];(nwr["name:en"~"Lotte World Tower"](37.509,127.099,37.516,127.106);nwr["building:part"](37.5115,127.1015,37.5135,127.104);node[railway=subway_entrance](37.513,127.098,37.5155,127.104););out meta geom;',
    'osm_jamsil': '[out:json][timeout:40];(way[highway~"^(footway|path|steps|pedestrian|residential|living_street|service|unclassified|tertiary|secondary)$"](37.492,127.077,37.533,127.128);nwr[leisure=park](37.492,127.077,37.533,127.128);way[natural~"^(wood|water|scrub|grassland)$"](37.492,127.077,37.533,127.128);way[landuse~"^(forest|grass)$"](37.492,127.077,37.533,127.128);node[barrier](37.492,127.077,37.533,127.128););out meta geom;'
}
DIRECT_URLS = {
    'tower_direct': 'https://api.openstreetmap.org/api/0.6/way/914963586/full.json',
    'map_direct': 'https://api.openstreetmap.org/api/0.6/map.json?bbox=127.092,37.503,127.114,37.520',
}
OFFICIAL_SOURCES = [
    {'id': 'lotte-operator', 'name': 'LOTTE Property & Development company information',
     'url': 'https://www.lotte.co.kr/global/en/business/compDetail.do?compCd=L407',
     'kind': 'published', 'retrieved_at': '2026-09-08', 'reference_time': 'undated operator page',
     'facts': {'structural_height_m': 555, 'above_ground_floors': 123},
     'limitations': 'Structural height is not a surveyed DTM-compatible apex elevation; foundation offset and vertical reference are unknown.'},
    {'id': 'songpa-seokchon', 'name': 'Songpa District official Seokchon Lake tourism listing',
     'url': 'https://www.songpa.go.kr/culture/detailInfo.do?key=3822&rcpp=6&resrceCd=TR0147-1000053&sc1=TR0147',
     'kind': 'published', 'retrieved_at': '2026-09-08', 'reference_time': '2020-07-22 page last modified',
     'facts': {'lake_circuit_km': 2.5, 'park_address': '서울특별시 송파구 잠실동 47',
               'east_lake': 'jogging course', 'west_lake': 'Magic Island, Seoul Norimadang, photo island',
               'walking_path_name': '송파나루길'},
     'limitations': 'Park description/photos establish general mapped context, not exact observer visibility, current entry, every path schedule, or wheelchair accessibility.'},
    {'id': 'visitseoul-seokchon', 'name': 'Seoul Tourism Organization: Seokchon Lake Park',
     'url': 'https://english.visitseoul.net/attractions/Seokchon-Lake-Park/ENP001136',
     'kind': 'published', 'retrieved_at': '2026-09-08', 'reference_time': 'undated tourism listing',
     'limitations': 'General place evidence; no live crowd or exact viewpoint observation.'},
]


def acquire(raw: Path, key: str) -> tuple[dict, dict]:
    path, meta_path = raw / f'{key}.json', raw / f'{key}.source.json'
    if path.exists():
        if not meta_path.exists():
            raise ValueError(f'Cached source missing provenance: {meta_path}')
        meta = json.loads(meta_path.read_text())
        if (key in QUERIES and meta.get('query') != QUERIES[key]) or (key in DIRECT_URLS and meta.get('endpoint') != DIRECT_URLS[key]):
            raise ValueError(f'Cached query differs; preserve and inspect {path}')
        encoded = path.read_bytes()
        if meta.get('sha256') and hashlib.sha256(encoded).hexdigest() != meta['sha256']:
            raise ValueError(f'Cached source fingerprint mismatch: {path}')
        value = json.loads(encoded)
        if value.get('remark') or 'elements' not in value:
            raise ValueError('Cached Overpass response is incomplete')
        return value, meta
    used = sum(p.stat().st_size for p in raw.glob('*.json'))
    remaining = RAW_CAP - used
    if remaining <= 0:
        raise ValueError('Raw context download cap exceeded')
    errors = []
    for endpoint in ([DIRECT_URLS[key]] if key in DIRECT_URLS else ENDPOINTS):
        request = urllib.request.Request(endpoint,
            data=urllib.parse.urlencode({'data': QUERIES[key]}).encode() if key in QUERIES else None,
            headers={'User-Agent': 'HiddenViewFinder/0.2 bounded Jamsil research extract'})
        try:
            with urllib.request.urlopen(request, timeout=55) as response:
                encoded = response.read(remaining + 1)
            if len(encoded) > remaining:
                raise ValueError('Combined OSM source responses exceed 10 MiB cap')
            value = json.loads(encoded)
            if value.get('remark') or 'elements' not in value:
                raise ValueError(f'Incomplete OSM result: {value.get("remark")}')
            meta = {'endpoint': endpoint, 'query': QUERIES.get(key),
                    'fetched_utc': datetime.now(timezone.utc).isoformat(),
                    'sha256': hashlib.sha256(encoded).hexdigest()}
            part = path.with_suffix('.json.part')
            if part.exists():
                raise FileExistsError(f'Inspect interrupted download: {part}')
            try:
                with part.open('xb') as stream:
                    stream.write(encoded)
                part.replace(path)
            finally:
                part.unlink(missing_ok=True)
            write_atomic(meta_path, meta)
            return value, meta
        except (urllib.error.URLError, TimeoutError) as error:
            errors.append(f'{endpoint}: {type(error).__name__}: {error}')
    raise RuntimeError('OSM source unavailable; cached sources preserved. ' + '; '.join(errors))


def prepare_context(documents: dict, provenance: dict, raw: Path) -> dict:
    elements = {(e['type'], e['id']): dict(e) for d in documents.values() for e in d['elements']}
    # The OSM map API returns topology; construct geometry from original node IDs.
    for (kind, identifier), element in elements.items():
        if kind == 'way' and 'geometry' not in element:
            ids = element.get('nodes', [])
            if all(('node', n) in elements for n in ids):
                element['geometry'] = [{k: elements[('node', n)][k] for k in ['lon', 'lat']} for n in ids]
    for (kind, identifier), element in elements.items():
        if kind == 'relation':
            element['members'] = [dict(m, geometry=elements.get(('way', m['ref']), {}).get('geometry', m.get('geometry', [])))
                                  if m['type'] == 'way' else m for m in element.get('members', [])]
    direct_keys = {(e['type'], e['id']) for e in documents.get('map_direct', {}).get('elements', [])}
    sources = list(OFFICIAL_SOURCES)
    for key in documents:
        p = raw / f'{key}.json'
        sources.append({'id': key, 'name': 'OpenStreetMap bounded Jamsil extract',
            'url': provenance[key]['endpoint'], 'kind': 'mapped',
            'retrieved_at': provenance[key]['fetched_utc'],
            'reference_time': documents[key].get('osm3s', {}).get('timestamp_osm_base', provenance[key]['fetched_utc']),
            'reference_time_kind': 'replica base timestamp' if 'osm3s' in documents[key] else 'OSM API snapshot retrieval time; individual edits may be older',
            'feature_edit_time_range': [min(e['timestamp'] for e in documents[key]['elements'] if 'timestamp' in e), max(e['timestamp'] for e in documents[key]['elements'] if 'timestamp' in e)],
            'license': 'ODbL 1.0', 'attribution': '© OpenStreetMap contributors',
            'license_url': 'https://www.openstreetmap.org/copyright',
            'sha256': hashlib.sha256(p.read_bytes()).hexdigest(), 'bytes': p.stat().st_size,
            'query': QUERIES.get(key)})
    tower_key = ('way', 914963586)
    footprint = polygon(elements[tower_key])
    relation = elements[('relation', 8824257)]
    label = elements[('node', 12520558001)]
    if (footprint is None or relation.get('tags', {}).get('name:en') != 'Lotte World Tower'
            or not any(m['type'] == 'way' and m['ref'] == tower_key[1] for m in relation['members'])
            or not footprint.covers(Point(label['lon'], label['lat']))):
        raise ValueError('Named tower relation, footprint membership and label location do not agree')
    center = footprint.centroid
    target = {'id': 'lotte-world-tower', 'name': '롯데월드타워 상단 대표점', 'type': 'tower_point',
        'lon': center.x, 'lat': center.y, 'height_m': 555.0, 'height_reference': 'agl',
        'height_status': 'estimated', 'supported': True, 'features': ['city', 'landmark'],
        'geometry': mapping(footprint), 'sources': ['lotte-operator', 'osm_tower', 'tower_direct'],
        'source_url': f'https://www.openstreetmap.org/{tower_key[0]}/{tower_key[1]}',
        'coordinate_method': 'Centroid of mapped ordinary 2D tower footprint, then engine raster quantization',
        'height_method': 'DTM at effective source cell + operator-published 555 m structural height',
        'uncertainties': ['Foundation elevation relative to bare-earth DTM and surveyed apex vertical datum are unknown',
                          '555 m is the operator published structural height; modeled point is an approximation',
                          'Only the upper representative point is tested; tower body, water, reflections, and skyline visibility are not established']}
    areas, invalid, lakes = [], [], []
    for (kind, identifier), element in sorted(elements.items()):
        tags = element.get('tags', {})
        if not (tags.get('leisure') == 'park' or tags.get('natural') in {'water', 'wood', 'scrub', 'grassland'}
                or tags.get('landuse') in {'forest', 'grass'}):
            continue
        geom = polygon(element)
        if geom is None:
            invalid.append(f'{kind}/{identifier}')
            continue
        areas.append({'id': f'{kind}/{identifier}', 'name': tags.get('name', tags.get('name:en', '')),
                      'kind': 'park' if tags.get('leisure') == 'park' else tags.get('natural', tags.get('landuse')),
                      'geometry': mapping(geom), 'tags': tags, 'source': 'map_direct' if (kind, identifier) in direct_keys else 'osm_jamsil',
                      'source_url': f'https://www.openstreetmap.org/{kind}/{identifier}', 'visibility_status': 'unknown'})
        if tags.get('natural') == 'water' and ('석촌' in tags.get('name', '') or 'Seokchon' in tags.get('name:en', '')):
            lakes.append(geom)
    nodes, edges = {}, []
    geod, bounds = Geod(ellps='WGS84'), box(*BBOX)
    for (kind, identifier), element in sorted(elements.items()):
        tags = element.get('tags', {})
        if kind != 'way' or tags.get('highway') not in ALLOWED_HIGHWAYS:
            continue
        ids, geom = element.get('nodes', []), element.get('geometry', [])
        if len(ids) != len(geom):
            raise ValueError(f'Incomplete way geometry: {identifier}')
        for i, (u, v) in enumerate(zip(ids, ids[1:])):
            a, b = geom[i], geom[i + 1]
            line = LineString([(a['lon'], a['lat']), (b['lon'], b['lat'])])
            if u == v or not bounds.covers(line):
                continue
            for node, position in ((u, a), (v, b)):
                nodes[str(node)] = {'id': str(node), 'lon': position['lon'], 'lat': position['lat'],
                    'tags': elements.get(('node', node), {}).get('tags', {})}
            access_status, access_evidence = edge_access(tags)
            hours = tags.get('opening_hours')
            hours_status = 'verified' if hours == '24/7' else 'unknown'
            barriers = [dict(node_id=str(node), **nodes[str(node)]['tags']) for node in (u, v)
                        if 'barrier' in nodes[str(node)]['tags']]
            _, _, length = geod.inv(a['lon'], a['lat'], b['lon'], b['lat'])
            foot_direction = tags.get('oneway:foot', 'no')
            directed = foot_direction in {'yes', '1', 'true', '-1'}
            if foot_direction == '-1':
                u, v, a, b = v, u, b, a
            edges.append({'id': f'{identifier}:{i}', 'u': str(u), 'v': str(v), 'way_id': identifier,
                'length_m': length, 'geometry': [[a['lon'], a['lat']], [b['lon'], b['lat']]],
                'directed': directed, 'tags': tags, 'access_status': access_status,
                'access_evidence': access_evidence, 'opening_hours': hours,
                'opening_hours_status': hours_status, 'opening_hours_evidence': ['OSM opening_hours tag'] if hours else [],
                'steps': tags['highway'] == 'steps', 'barriers': barriers, 'source': 'map_direct' if (kind, identifier) in direct_keys else 'osm_jamsil',
                'source_url': f'https://www.openstreetmap.org/way/{identifier}'})
    # Actual outdoor path origin: the mapped subway exit is disconnected in this
    # extract. Keep that station separately; never invent a connector across it.
    start_key = ('node', 857271262)
    start = nodes[str(start_key[1])]
    start_way = elements[('way', 533735581)]
    if start_way.get('tags', {}).get('name') != '송파나루길' or start_key[1] not in start_way['nodes']:
        raise ValueError('Expected mapped Songpanaru-gil origin has changed; inspect the context')
    landmarks = [target]
    if lakes:
        landmarks.append({'id': 'seokchon-lake', 'name': '석촌호수 수면과 주변 공원', 'type': 'broad_scene',
            'geometry': mapping(unary_union(lakes)), 'features': ['nature', 'water', 'park'],
            'supported': False, 'visibility_status': 'unknown', 'sources': ['map_direct', 'songpa-seokchon'],
            'reason': 'Mapped lake context only; distributed water targets, vegetation and reflective appearance are not modeled'})
    return {'schema_version': 1, 'mode': 'real', 'region': 'Jamsil–Seokchon Lake, Seoul',
        'crs': 'EPSG:4326', 'coordinate_order': 'longitude, latitude', 'bounds': BBOX,
        'prepared_at': datetime.now(timezone.utc).isoformat(), 'sources': sources,
        'query_radius_m': 1000, 'path_label': '잠실·석촌호수 보행로', 'graph_source_id': 'osm_jamsil',
        'visibility_limitations': ['GBA footprint height for Lotte World Tower is about 137 m versus published structural height 555 m; the obstruction surface materially underestimates this tower and may falsely show rays through its body', 'The 555 m target is a DTM-relative approximate upper point, not a surveyed apex; lake water and whole tower visibility are not calculated', 'Strict available terrain coverage limits this demo to a 1000 m radius'],
        'landmarks': landmarks, 'graph': {'nodes': list(nodes.values()), 'edges': edges}, 'areas': areas,
        'default_start': {'name': '석촌호수 동호 북측 송파나루길', 'lon': start['lon'], 'lat': start['lat'],
            'source_url': f'https://www.openstreetmap.org/node/{start_key[1]}', 'wheelchair_status': 'unknown', 'source_way_url': 'https://www.openstreetmap.org/way/533735581',
            'assumption': 'Demo begins on the mapped lake walking path; route from subway station is not included'},
        'nearby_station': {'name': '잠실역 2번 출구', 'lon': 127.1009003, 'lat': 37.5128484,
            'source_url': 'https://www.openstreetmap.org/node/3401171490', 'route_status': 'unknown',
            'reason': 'Mapped entrance belongs to a disconnected component in this walking graph'},
        'weather': {'status': 'unknown', 'reference_time': None}, 'crowding': {'status': 'unknown', 'reference_time': None},
        'quality': {'invalid_or_unclosed_area_ids': invalid,
            'access_policy': 'Explicit foot/access tags only; park or lake proximity does not verify entry',
            'opening_hours_policy': 'Only explicit 24/7 way tags marked verified; no invented park-wide hours',
            'route_policy': 'Original OSM node adjacency and geodesic segment lengths; pedestrian one-way preserved; direct-map supplement filtered to the same explicit highway allowlist as Overpass',
            'limitations': ['No live closure, crowd or weather observations',
                'Network extent is bounded; routes leaving the extract are unavailable',
                'Barriers and accessibility tags may be missing or outdated',
                'Mapped nearby water and greenery are not evidence of visible scenery',
                'Trees, small walls, construction and overhangs are absent from the visibility surface']}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'data/demo/jamsil/context.json')
    args = parser.parse_args()
    budget = preflight(ROOT / 'data', additional_bytes=40 * 1024**2, temporary_bytes=RAW_CAP)
    raw = ROOT / 'data/acquisition/demo-jamsil'
    raw.mkdir(parents=True, exist_ok=True)
    documents, provenance = {}, {}
    for key in [*QUERIES, *DIRECT_URLS]:
        documents[key], provenance[key] = acquire(raw, key)
    result = prepare_context(documents, provenance, raw)
    result['storage_preflight'] = budget
    encoded = json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode()
    if len(encoded) > CONTEXT_CAP:
        raise ValueError('Prepared context exceeds demo server 12 MiB loading cap; narrow the extract')
    preflight(ROOT / 'data', additional_bytes=CONTEXT_CAP, temporary_bytes=RAW_CAP)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(args.output, result)
    print(json.dumps({'context': str(args.output), 'context_bytes': len(encoded),
        'raw_bytes': sum((raw / f'{key}.json').stat().st_size for key in documents),
        'nodes': len(result['graph']['nodes']), 'edges': len(result['graph']['edges']),
        'areas': len(result['areas']), 'target': {k: result['landmarks'][0][k] for k in ['lon','lat','height_m','source_url']},
        'default_start': result['default_start'],
        'access_counts': dict(Counter(e['access_status'] for e in result['graph']['edges'])),
        'source_timestamps': {s['id']: s['reference_time'] for s in result['sources']}}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
