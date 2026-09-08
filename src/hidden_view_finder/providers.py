"""Bounded map routing and optional timestamped weather, independent of ranking.

Walking times are estimates on the supplied OSM graph, never verified journey
measurements. A snapped start is not proof that its connecting segment is usable.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import heapq
import json
import math
from pathlib import Path
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


def distance_m(a: tuple, b: tuple) -> float:
    p, q = math.radians(a[1]), math.radians(b[1])
    dp, dl = q-p, math.radians(b[0]-a[0])
    h = math.sin(dp/2)**2 + math.cos(p)*math.cos(q)*math.sin(dl/2)**2
    return 6371008.8 * 2 * math.asin(math.sqrt(min(1, h)))


def bearing_deg(a: tuple, b: tuple) -> float:
    p, q, dl = math.radians(a[1]), math.radians(b[1]), math.radians(b[0]-a[0])
    return math.degrees(math.atan2(math.sin(dl)*math.cos(q),
        math.cos(p)*math.sin(q)-math.sin(p)*math.cos(q)*math.cos(dl))) % 360


def load_context(path: Path) -> dict:
    if path.stat().st_size > 12 * 1024**2:
        raise ValueError('Context exceeds the 12 MiB local demo cap')
    result = json.loads(path.read_text())
    if result.get('schema_version') != 1 or not result.get('graph', {}).get('nodes'):
        raise ValueError('Unsupported or empty context; run scripts/demo/acquire_context.py')
    return result


class WalkingRouter:
    """One Dijkstra per starting point; never a straight-line journey substitute."""
    def __init__(self, graph: dict):
        self.nodes = {str(n['id']): n for n in graph['nodes']}
        self.edges = graph['edges']
        self.adjacency: dict[str, list[tuple[str, dict]]] = {}
        for edge in self.edges:
            a, b = str(edge['u']), str(edge['v'])
            if a not in self.nodes or b not in self.nodes:
                continue
            self.adjacency.setdefault(a, []).append((b, edge))
            if not edge.get('directed', False) and edge.get('tags', {}).get('oneway:foot') not in ('yes', '1'):
                self.adjacency.setdefault(b, []).append((a, edge))
        self.cost: dict[str, float] = {}
        self.parents: dict[str, tuple[str, dict]] = {}
        self.origin: str | None = None
        self.connector = 0.0

    @staticmethod
    def permitted(edge: dict, request: dict) -> bool:
        tags = edge.get('tags', {})
        for barrier in edge.get('barriers', []):
            barrier_tags = barrier.get('tags', barrier)
            if barrier_tags.get('locked') == 'yes' or barrier_tags.get('foot') in ('no', 'private'):
                return False
            if barrier_tags.get('access') in ('no', 'private') and barrier_tags.get('foot') not in ('yes', 'designated', 'permissive'):
                return False
            if request.get('wheelchair') and barrier_tags.get('wheelchair') == 'no':
                return False
        if edge.get('access_status') == 'prohibited' or tags.get('foot') in ('no', 'private'):
            return False
        if tags.get('access') in ('no', 'private') and tags.get('foot') not in ('yes', 'designated', 'permissive'):
            return False
        stairs = edge.get('steps') or tags.get('highway') == 'steps'
        if (request.get('no_stairs') or request.get('wheelchair') or request.get('stroller')) and stairs:
            return False
        if request.get('wheelchair') and tags.get('wheelchair') == 'no':
            return False
        return True

    def solve(self, start: tuple, request: dict) -> None:
        self.cost, self.parents, self.origin = {}, {}, None
        incident = {str(e[k]) for e in self.edges for k in ('u', 'v')}
        # Snap geometrically first, including one-way sinks and restricted
        # endpoints. Never skip a stair/gate by choosing an easier component.
        eligible = [n for key, n in self.nodes.items() if key in incident]
        if not eligible:
            return
        n = min(eligible, key=lambda n: distance_m(start, (n['lon'], n['lat'])))
        self.connector = distance_m(start, (n['lon'], n['lat']))
        # Beyond this bound the map cannot establish an origin connection.
        if self.connector > 80:
            return
        self.origin = str(n['id'])
        self.cost[self.origin] = 0.0
        queue = [(0.0, self.origin)]
        while queue:
            cost, node = heapq.heappop(queue)
            if cost != self.cost[node]:
                continue
            for other, edge in self.adjacency.get(node, []):
                if not self.permitted(edge, request):
                    continue
                value = cost + float(edge['length_m'])
                if value < self.cost.get(other, math.inf):
                    self.cost[other] = value
                    self.parents[other] = (node, edge)
                    heapq.heappush(queue, (value, other))

    def route(self, node_id: str, mode: str = 'walking') -> dict:
        node_id = str(node_id)
        unknown = {'status': 'unknown', 'mode': mode, 'travel_minutes': None, 'walking_m': None,
                   'stairs': None, 'wheelchair': None, 'stroller': None, 'slope_percent': None,
                   'geometry': [], 'reason': 'No supported pedestrian route from supplied start.'}
        if mode != 'walking':
            return {**unknown, 'reason': 'Transit/driving routes and schedules are not connected in this demo.'}
        if node_id not in self.cost:
            return unknown
        trail, edges = [node_id], []
        current = node_id
        while current != self.origin:
            current, edge = self.parents[current]
            trail.append(current)
            edges.append(edge)
        trail.reverse()
        length = self.cost[node_id] + self.connector
        stairs = any(e.get('steps') or e.get('tags', {}).get('highway') == 'steps' for e in edges)
        # Omitted incline/kerbs/width and a starting connector cannot certify access.
        return {'status': 'estimated', 'mode': 'walking', 'travel_minutes': round(length / 66.67, 1),
                'walking_m': round(length, 1), 'stairs': bool(stairs),
                'wheelchair': False if stairs else None, 'stroller': False if stairs else None,
                'slope_percent': None, 'geometry': [[self.nodes[n]['lon'], self.nodes[n]['lat']] for n in trail],
                'start_snap_m': round(self.connector, 1), 'edge_count': len(edges),
                'method': 'Shortest mapped pedestrian length / assumed 4 km/h. No slope, signal or crowd delay model.',
                'reason': 'Estimated journey; connector, barriers, slope and current closures remain unverified.',
                'access_unknown_edges': sum(e.get('access_status') != 'verified' for e in edges)}


class ForecastProvider:
    """At most 8 cached requests, 15 minute TTL; never extend forecast valid times."""
    def __init__(self, enabled: bool = False):
        self.enabled = enabled
        self.cache: dict[tuple, tuple[float, dict]] = {}
        self.failures: dict[tuple, tuple[float, str]] = {}

    def get(self, lon: float, lat: float, arrival: datetime) -> dict:
        unknown = {'status': 'unknown', 'reference_time': None, 'precipitation_mm': None,
            'cloud_cover_percent': None, 'visibility_m': None, 'wind_m_s': None,
            'reason': 'Live weather is disabled; no assumed clear weather.'}
        if not self.enabled:
            return unknown
        now = datetime.now(timezone.utc)
        if not now - timedelta(hours=1) <= arrival.astimezone(timezone.utc) <= now + timedelta(days=15):
            return {**unknown, 'reason': 'Visit lies outside the supported current forecast window.'}
        local = arrival.astimezone(ZoneInfo('Asia/Seoul'))
        key = (lon, lat, str(local.date()))
        failure = self.failures.get(key)
        if failure and time.monotonic() - failure[0] < 60:
            return {**unknown, 'reason': failure[1]}
        try:
            cached = self.cache.get(key)
            if cached and time.monotonic() - cached[0] < 900:
                payload = cached[1]
            else:
                params = {'longitude': lon, 'latitude': lat, 'timezone': 'Asia/Seoul',
                    'start_date': str(local.date()), 'end_date': str(local.date()), 'wind_speed_unit': 'ms',
                    'hourly': 'precipitation,cloud_cover,visibility,wind_speed_10m,is_day',
                    'daily': 'sunrise,sunset'}
                url = 'https://api.open-meteo.com/v1/forecast?' + urlencode(params)
                with urlopen(Request(url, headers={'User-Agent': 'HiddenViewFinder-demo/0.2'}), timeout=6) as response:
                    raw = response.read(1_000_001)
                if len(raw) > 1_000_000:
                    raise ValueError('Forecast response exceeds cap')
                payload = json.loads(raw)
                payload['_retrieved_at'] = now.isoformat()
                payload['_url'] = url
                if len(self.cache) >= 8:
                    self.cache.pop(next(iter(self.cache)))
                self.cache[key] = (time.monotonic(), payload)
            hourly = payload['hourly']
            times = [datetime.fromisoformat(t).replace(tzinfo=ZoneInfo('Asia/Seoul')) for t in hourly['time']]
            index = min(range(len(times)), key=lambda i: abs((times[i]-local).total_seconds()))
            valid = times[index]
            if abs((valid-local).total_seconds()) > 3600:
                return {**unknown, 'reason': 'No forecast hour matches arrival.'}
            result = {'status': 'forecast', 'reference_time': payload['_retrieved_at'],
                'retrieved_at': payload['_retrieved_at'], 'model_run_time': None,
                'valid_from': (valid-timedelta(minutes=30)).isoformat(),
                'valid_until': (valid+timedelta(minutes=30)).isoformat(), 'forecast_time': valid.isoformat(),
                'source_url': payload['_url'], 'source': 'Open-Meteo Best Match, CC BY 4.0',
                'scope': 'Model grid prediction near destination, not an on-site observation.',
                'sunrise': payload.get('daily', {}).get('sunrise', [None])[0],
                'sunset': payload.get('daily', {}).get('sunset', [None])[0]}
            for key_out, key_in in [('precipitation_mm', 'precipitation'), ('cloud_cover_percent', 'cloud_cover'),
                                    ('visibility_m', 'visibility'), ('wind_m_s', 'wind_speed_10m')]:
                value = hourly.get(key_in, [None]*len(times))[index]
                result[key_out] = value if isinstance(value, (int, float)) and math.isfinite(value) else None
            return result
        except (OSError, ValueError, KeyError, IndexError) as error:
            reason = f'Forecast unavailable ({type(error).__name__}); no values imputed.'
            if len(self.failures) >= 8:
                self.failures.pop(next(iter(self.failures)))
            self.failures[key] = (time.monotonic(), reason)
            return {**unknown, 'reason': reason}
