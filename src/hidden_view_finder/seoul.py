"""Discover actual OSM pedestrian nodes after one native target viewshed.

The single sampled tower point cannot establish tower silhouette, forest or
skyline composition. Missing access, journey validation and crowds stay unknown.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from importlib.util import find_spec
from pathlib import Path
import math
import time

from .providers import WalkingRouter, bearing_deg, distance_m, load_context, ForecastProvider


class SeoulAdapter:
    def __init__(self, manifest: Path, context: Path, weather: ForecastProvider):
        self.manifest_path, self.context_path = manifest, context
        self.weather = weather
        self.engine = None
        self.context = None
        self.router = None
        self.signature = None
        self.context_signature = None

    @property
    def available(self) -> bool:
        return (self.manifest_path.is_file() and self.context_path.is_file()
                and all(find_spec(name) is not None for name in ('osgeo', 'numpy', 'pyproj')))

    def close(self) -> None:
        if self.engine is not None:
            self.engine.close()
            self.engine = None

    def context_info(self) -> dict:
        """Reuse the small local map context; do not open terrain for bootstrap."""
        signature = self.context_path.stat().st_mtime_ns
        if self.context is None or signature != self.context_signature:
            self.context = load_context(self.context_path)
            self.context_signature = signature
        return self.context

    def _open(self) -> None:
        from seoul_visibility import VisibilityEngine
        signature = (self.context_path.stat().st_mtime_ns, self.manifest_path.stat().st_mtime_ns)
        if self.engine is None or signature != self.signature:
            self.close()
            self.context = self.context_info()
            self.router = WalkingRouter(self.context['graph'])
            self.engine = VisibilityEngine.from_manifest(self.manifest_path)
            self.signature = signature

    def bundle(self, request: dict) -> dict:
        import numpy as np
        from seoul_visibility import State, TargetPoint
        self._open()
        assert self.context is not None and self.engine is not None and self.router is not None
        context = self.context
        landmark = context['landmarks'][0]
        target = TargetPoint(landmark['lon'], landmark['lat'], landmark['height_m'],
                             landmark.get('height_reference', 'agl'),
                             vertical_reference=landmark.get('vertical_reference'))
        radius = float(context.get('query_radius_m', 1800))
        if not math.isfinite(radius) or not 0 < radius <= 10000:
            raise ValueError('Context query_radius_m must be finite and within (0, 10000]')
        started = time.perf_counter()
        result = self.engine.visible_from_target(target, radius_m=radius,
            eye_height_m=request.get('eye_height_m', 1.7), resolution_m=5, curvature_coefficient=6/7)
        # Transform all map nodes in one array. Raster remains unchanged: only the
        # resulting states are intersected with candidate pedestrian nodes.
        node_map = {str(n['id']): n for n in context['graph']['nodes']}
        walk_edges = {}
        for edge in context['graph']['edges']:
            if edge.get('tags', {}).get('highway') not in ('footway', 'path', 'pedestrian', 'steps'):
                continue
            for node_id in (edge['u'], edge['v']):
                walk_edges.setdefault(str(node_id), edge)
        nodes = [node_map[k] for k in sorted(walk_edges) if k in node_map]
        if not nodes:
            raise ValueError('No pedestrian candidates in inspected map context')
        lon = np.array([n['lon'] for n in nodes])
        lat = np.array([n['lat'] for n in nodes])
        x, y = self.engine.to_xy.transform(lon, lat)
        gt = result.transform
        cols = np.floor((x-gt[0])/gt[1]).astype(int)
        rows = np.floor((y-gt[3])/gt[5]).astype(int)
        inside = (rows >= 0) & (cols >= 0) & (rows < result.states.shape[0]) & (cols < result.states.shape[1])
        start = (request['start']['lon'], request['start']['lat'])
        self.router.solve(start, request)
        # Bounded deterministic spatial representatives. This is discovery
        # sampling, not a claim that every possible viewpoint was searched.
        bins = {}
        total_states = {s.name.lower(): 0 for s in State}
        for index in np.flatnonzero(inside):
            n = nodes[index]
            if distance_m((n['lon'], n['lat']), (target.lon, target.lat)) > radius:
                continue
            state = State(int(result.states[rows[index], cols[index]])).name.lower()
            total_states[state] += 1
            key = (int(x[index]//100), int(y[index]//100), state)
            dist = distance_m(start, (n['lon'], n['lat']))
            if key not in bins or dist < bins[key][0]:
                bins[key] = (dist, n, state, int(rows[index]), int(cols[index]))
        sampled = sorted(bins.values(), key=lambda item: (item[0], str(item[1]['id'])))[:120]
        visit = datetime.fromisoformat(request['visit_time'])
        supplied_weather = request.get('weather') or {}
        # Preserve caller evidence and its original validity times. Ranking
        # separately checks freshness at each arrival; no stale evidence is
        # relabeled as a forecast or refreshed by this request.
        use_supplied = supplied_weather.get('status') in ('observed', 'forecast', 'estimated')
        def weather_at(arrival: datetime) -> dict:
            if use_supplied:
                return dict(supplied_weather, provenance='caller_provided_unverified')
            return self.weather.get(target.lon, target.lat, arrival)
        forecast = weather_at(visit + timedelta(minutes=30))
        candidates = []
        for _, n, state, row, col in sampled:
            edge = walk_edges[str(n['id'])]
            tags = edge.get('tags', {})
            route = self.router.route(str(n['id']), request['transport_mode'])
            arrival = visit + timedelta(minutes=route.get('travel_minutes') or 0)
            spot_weather = weather_at(arrival)
            title = tags.get('name:ko') or tags.get('name') or context.get('path_label', '주변 보행 경로')
            public = True if edge.get('access_status') == 'verified' else (
                False if edge.get('access_status') == 'prohibited' else None)
            hours = edge.get('opening_hours')
            opening = [{'start': '00:00', 'end': '24:00'}] if hours == '24/7' else None
            source_time = next((s.get('reference_time') for s in context['sources']
                                if s.get('id') == context.get('graph_source_id', 'osm_namsan')), None)
            effective_x = gt[0] + (col+.5)*gt[1]
            effective_y = gt[3] + (row+.5)*gt[5]
            effective_lon, effective_lat = self.engine.to_lonlat.transform(effective_x, effective_y)
            item = {'id': f"osm-{n['id']}", 'name': f"{title} · {n['id']}",
                'lon': n['lon'], 'lat': n['lat'], 'bearing_deg': round(bearing_deg((n['lon'], n['lat']), (target.lon, target.lat)), 1),
                'target_ids': [landmark['id']], 'target_names': [landmark['name']],
                'scenic_features': ['city'] if state == 'visible' else [], 'scenic_features_status': 'computed',
                'composition_signature': 'tower-point-only', 'visibility': state, 'evidence_kind': 'computed',
                'visibility_evidence': {'scope': 'One approximate tower-top point at the containing 5 m observer cell center.',
                    'effective_observer': {'lon': effective_lon, 'lat': effective_lat},
                    'snap_distance_m': round(distance_m((n['lon'], n['lat']), (effective_lon, effective_lat)), 3),
                    'backend': result.metadata.get('backend'), 'target': result.metadata['target'],
                    'surface': '2023 contour terrain + mostly 2018/2019 estimated building heights'},
                'route': route, 'access': {'public': public, 'public_status': edge.get('access_status', 'unknown'), 'opening_hours': opening,
                    'opening_hours_status': 'verified' if opening and edge.get('opening_hours_status') == 'verified' else 'unknown',
                    'step_free': False if edge.get('steps') else None},
                'crowd': {'level': None, 'status': 'unknown', 'reference_time': None,
                    'reason': 'No crowd observation or forecast provider; residential density is not used.'},
                'weather': spot_weather, 'map_reference_time': source_time,
                'feature_evidence': [{'feature': 'city', 'status': 'computed',
                    'detail': 'Only the modeled tower point, not the skyline or complete tower.'}],
                'composition': {'angular_size_deg': None, 'openness': None,
                    'foreground': 'unknown', 'middle_ground': 'unknown', 'background': 'unknown'},
                'source_ids': [s['id'] for s in context['sources']],
                'uncertainties': ['5 m cell snapping may move the endpoint off the mapped path.',
                    'Trees, walls, railings and construction are absent from the obstruction model.',
                    'Path access, full journey accessibility and current closures require verification.',
                    f"Target height is {landmark['height_m']} m {target.height_reference}; surveyed foundation datum is unresolved.",
                    'Nearby mapped woods/water are not asserted to be visible.',
                    *context.get('visibility_limitations', [])]}
            candidates.append(item)
        roads = [{'coordinates': e['geometry'], 'kind': e.get('tags', {}).get('highway', 'path')}
                 for e in context['graph']['edges'] if e.get('geometry')]
        target_summary = dict(landmark, visibility_support='point_only_approximate',
                    absolute_elevation_m=result.metadata['target']['absolute_elevation_m'])
        broad = [{k:v for k,v in item.items() if k != 'geometry'}
                 for item in context['landmarks'][1:]]
        return {'landmarks': [target_summary, *broad],
            'candidates': candidates, 'sources': context['sources'], 'weather': forecast,
            'assumptions': ['Mapped walking time assumes 4 km/h; no traffic signals, gradients or crowd delays.',
                f"Only a {radius:,.0f} m circle around {landmark['name']} within prepared coverage is searched.",
                'Real map records and old elevation products are not current on-site access observations.'],
            'map': {'kind': 'osm_geometry', 'bounds': context.get('bounds'), 'paths': roads[:6000],
                    'attribution': '© OpenStreetMap contributors · ODbL; snapshot date in sources'},
            'visibility_summary': {'backend_timings': result.timings, 'sampled_candidates': len(candidates),
                'cache_status': result.metadata.get('cache_status', 'unknown'),
                'backend': result.metadata.get('backend'),
                'pedestrian_node_states': total_states, 'target': result.metadata['target'],
                'limitations': result.metadata.get('limitations', []), 'elapsed_s': time.perf_counter()-started}}
