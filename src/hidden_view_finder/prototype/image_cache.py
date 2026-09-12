"""Owned, byte-capped atmosphere image derivatives with atomic publication.

Input images exist only in bounded memory. The cache retains one JPEG display
image, one JPEG thumbnail, and bounded metadata. Nothing in the source package
is ever an eviction candidate. All writers require the shared Budget lock.
"""
from __future__ import annotations

from hashlib import sha256
from io import BytesIO
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import time
import uuid
from zoneinfo import ZoneInfo

from PIL import Image, ImageOps
from seoul_visibility.acquisition_safety import Budget, Reservation
from .spending import ProviderUnavailable

IMAGE_LABEL = 'AI-generated atmosphere preview. Actual scenery may differ.'
IMAGE_LIMIT = 250_000_000
ENTRY_PEAK_BYTES = 12_000_000
KEY_RE = re.compile(r'^[a-f0-9]{64}$')
PROMPT_VERSION = 'atmosphere-evidence-v1'


def _process_start(pid):
    """Linux start-time token avoids mistaking a reused PID for a live job."""
    try:
        return Path(f'/proc/{int(pid)}/stat').read_text().rsplit(')', 1)[1].split()[19]
    except (OSError, ValueError, IndexError):
        return None


def canonical(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def image_identity(scene: dict, model: str) -> dict:
    """Exact position/direction and evidence identity; no private origin/text.

    Five-degree solar classes prevent second-by-second cache explosion. The
    image brief uses those same classes, so it never claims finer light timing.
    """
    standing = scene.get('standing', {})
    point = scene.get('effective_position') or scene.get('effective_coordinates')
    if not point and standing.get('effective_x') is not None:
        point = [standing.get('effective_x'), standing.get('effective_y')]
    elif not point and standing:
        point = [standing.get('lon'), standing.get('lat')]
    if isinstance(point, dict):
        point = [point.get('lon'), point.get('lat')]
    if not point:
        point = [scene.get('lon'), scene.get('lat')]
    if len(point) != 2 or any(not isinstance(x, (float, int)) or not math.isfinite(x) for x in point):
        raise ValueError('Image requires a valid effective position')
    solar = scene.get('solar') or scene.get('sunlight') or scene.get('sun') or {}
    altitude = solar.get('altitude_deg', solar.get('elevation_deg'))
    azimuth = solar.get('azimuth_deg')
    def bucket(value):
        return int(math.floor(value / 5)) * 5 if isinstance(value, (int, float)) and math.isfinite(value) else None
    versions = scene.get('data_versions') or scene.get('versions') or scene.get('geometry_version')
    evidence_version = scene.get('evidence_version') or scene.get('evidence_hash')
    if not evidence_version and scene.get('scene_samples'):
        # Only geometric sample evidence enters this fingerprint, never origin,
        # free text, rank, or exact-second viewing time.
        evidence_version = sha256(canonical({'samples': [
            {k: sample.get(k) for k in ('evidence_id', 'target_id', 'category', 'bearing_deg', 'distance_m', 'state', 'target', 'angular_elevation_deg')}
            for sample in scene['scene_samples']]})).hexdigest()
    if not versions or not evidence_version:
        raise ValueError('Image requires geometry and evidence versions')
    weather = scene.get('weather', {})
    material_weather, weather_basis = 'unknown', 'unknown'
    if weather.get('status') == 'user_selected_hypothetical':
        material_weather, weather_basis = weather.get('material_class', 'unknown'), 'hypothetical_not_forecast'
    elif weather.get('status') == 'forecast':
        try:
            view_at = datetime.fromisoformat(scene['view_at'])
            valid_from = datetime.fromisoformat(weather['valid_from'])
            valid_until = datetime.fromisoformat(weather['valid_until'])
            retrieved = datetime.fromisoformat(weather['retrieved_at'])
            if (all(value.tzinfo is not None for value in (view_at, valid_from, valid_until, retrieved))
                    and valid_from <= view_at <= valid_until
                    and 0 <= (datetime.now(timezone.utc)-retrieved).total_seconds() <= 6*3600):
                material_weather, weather_basis = weather.get('material_class', 'unknown'), 'forecast'
        except (KeyError, TypeError, ValueError):
            pass
    if material_weather not in ('unknown', 'clear', 'cloudy', 'rain', 'snow', 'fog'):
        material_weather = 'unknown'
    if material_weather == 'unknown':
        weather_basis = 'unknown'
    season = 'unknown'
    try:
        at = datetime.fromisoformat(scene['view_at'])
        if at.tzinfo is not None:
            month = at.astimezone(ZoneInfo('Asia/Seoul')).month
            season = ('winter' if month in (12, 1, 2) else 'spring' if month in (3, 4, 5)
                      else 'summer' if month in (6, 7, 8) else 'autumn')
    except (KeyError, TypeError, ValueError):
        pass
    orientation = scene.get('orientation', {})
    return {'prompt_version': PROMPT_VERSION, 'model': model,
            'view_id': scene.get('candidate_id') or scene.get('view_id') or scene.get('id'), 'effective_position': point,
            'effective_crs': 'EPSG:5186' if standing.get('effective_x') is not None else 'EPSG:4326',
            'bearing_deg': orientation.get('bearing_deg', scene.get('bearing_deg')),
            'fov_deg': orientation.get('fov_deg', scene.get('fov_deg')),
            'geometry_versions': versions, 'evidence_version': evidence_version,
            'solar_altitude_class_deg': bucket(altitude), 'solar_azimuth_class_deg': bucket(azimuth),
            'season': season, 'weather_class': material_weather, 'weather_basis': weather_basis}


def image_key(scene: dict, model: str) -> str:
    return sha256(canonical(image_identity(scene, model))).hexdigest()


class CappedBuffer(BytesIO):
    def __init__(self, limit):
        super().__init__()
        self.limit = limit

    def write(self, value):
        if self.tell() + len(value) > self.limit:
            raise ProviderUnavailable('image_encoding_limit')
        return super().write(value)


def image_derivatives(blob: bytes) -> tuple[bytes, bytes]:
    if len(blob) > 8_000_000:
        raise ProviderUnavailable('image_response_too_large')
    try:
        with Image.open(BytesIO(blob)) as source:
            if source.format not in ('PNG', 'JPEG', 'WEBP') or source.width * source.height > 2_000_000:
                raise ProviderUnavailable('image_dimensions_or_format_invalid')
            if source.width < 32 or source.height < 32 or getattr(source, 'n_frames', 1) != 1:
                raise ProviderUnavailable('image_dimensions_or_format_invalid')
            source.load()
            display = ImageOps.exif_transpose(source).convert('RGB')
            display.thumbnail((1024, 1024))
            full = CappedBuffer(4_000_000)
            display.save(full, format='JPEG', quality=82, optimize=False)
            display.thumbnail((320, 320))
            thumb = CappedBuffer(500_000)
            display.save(thumb, format='JPEG', quality=70, optimize=False)
            return full.getvalue(), thumb.getvalue()
    except ProviderUnavailable:
        raise
    except Exception:
        raise ProviderUnavailable('image_decode_failed') from None


class ImageCache:
    def __init__(self, root: Path, budget: Budget, max_bytes=IMAGE_LIMIT, max_entries=64):
        if not isinstance(max_bytes, int) or isinstance(max_bytes,bool) or not 0 <= max_bytes <= IMAGE_LIMIT:
            raise ValueError('Image cache ceiling cannot exceed 250000000 bytes')
        if not isinstance(max_entries, int) or not 1 <= max_entries <= 128:
            raise ValueError('Image cache max_entries must be 1..128')
        self.root = budget.safe_path(root)
        self.budget, self.max_bytes, self.max_entries = budget, max_bytes, max_entries
        self.index = self.root / 'cache.json'

    def _load(self):
        if not self.index.exists():
            return {'schema': 1, 'owner': 'hvf-prototype-image-cache-v1', 'entries': {}, 'jobs': {}, 'deletion_count': 0, 'recent_deletions': []}
        self.budget.safe_path(self.index)
        if self.index.stat().st_size > 512_000:
            raise ProviderUnavailable('image_cache_manifest_invalid')
        try:
            value = json.loads(self.index.read_bytes())
            if value['schema'] != 1 or value['owner'] != 'hvf-prototype-image-cache-v1' or len(value['entries']) > 128 or len(value['jobs']) > 128:
                raise ValueError
            return value
        except (ValueError, KeyError, TypeError):
            raise ProviderUnavailable('image_cache_manifest_invalid') from None

    def used_bytes(self):
        total = 0
        if self.root.exists():
            for path in self.root.rglob('*'):
                self.budget.safe_path(path)
                if path.is_file():
                    stat = path.stat()
                    total += max(stat.st_size, stat.st_blocks * 512)
                elif path.is_dir():
                    total += path.stat().st_blocks * 512
        return total

    def _write(self, path: Path, blob: bytes, reservation: Reservation):
        if reservation.budget is not self.budget:
            raise ValueError('Wrong storage reservation')
        self.budget.safe_path(path)
        temporary = self.budget.safe_path(path.with_name(path.name + '.part.' + uuid.uuid4().hex))
        reservation.check_write(len(blob) + 16_384, temporary)
        if self.used_bytes() + len(blob) + 16_384 > self.max_bytes:
            raise ProviderUnavailable('image_cache_full')
        self.root.mkdir(parents=True, exist_ok=True)
        with temporary.open('xb') as handle:
            for offset in range(0, len(blob), 262_144):
                chunk = blob[offset:offset+262_144]
                reservation.check_write(len(chunk), temporary)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _save(self, record, reservation):
        blob = canonical(record)
        if len(blob) > 512_000:
            raise ProviderUnavailable('image_cache_manifest_full')
        self._write(self.index, blob, reservation)

    def lookup(self, key, thumbnail=False):
        if not isinstance(key, str) or not KEY_RE.fullmatch(key):
            return None
        record = self._load()
        entry = record['entries'].get(key)
        if not entry or entry.get('status') != 'ready':
            return None
        expected = entry['files'].get('thumbnail' if thumbnail else 'display')
        if not expected or expected['name'] != key + ('.thumb.jpg' if thumbnail else '.jpg'):
            return None
        path = self.budget.safe_path(self.root / expected['name'])
        if not path.is_file() or path.stat().st_size != expected['bytes'] or path.stat().st_size > 4_000_000:
            return None
        if sha256(path.read_bytes()).hexdigest() != expected['sha256']:
            return None
        return path

    def prepare(self, key, identity, reservation):
        """Evict only verified owned derivatives, reserve cache peak before POST."""
        record = self._load()
        for candidate, entry in sorted(record['entries'].items(), key=lambda item: item[1].get('last_used', 0)):
            if self.used_bytes() + ENTRY_PEAK_BYTES <= self.max_bytes and len(record['entries']) < self.max_entries:
                break
            if candidate == key:
                continue
            # Record deletion intent before unlink; restart can repeat safely.
            names = []
            for kind in ('display', 'thumbnail'):
                field = entry.get('files', {}).get(kind)
                if not field or field.get('name') != candidate + ('.thumb.jpg' if kind == 'thumbnail' else '.jpg'):
                    raise ProviderUnavailable('image_cache_ownership_invalid')
                path = self.budget.safe_path(self.root / field['name'])
                if path.exists() and (path.stat().st_size != field['bytes'] or sha256(path.read_bytes()).hexdigest() != field['sha256']):
                    raise ProviderUnavailable('image_cache_owned_content_changed')
                names.append(path)
            entry['status'] = 'evicting'
            self._save(record, reservation)
            for path in names:
                path.unlink(missing_ok=True)
            record['recent_deletions'] = (record['recent_deletions'] + [{'key': candidate, 'reason': 'owned_disposable_lru', 'files': entry['files']}])[-32:]
            record['deletion_count'] += len(names)
            del record['entries'][candidate]
            self._save(record, reservation)
        if self.used_bytes() + ENTRY_PEAK_BYTES > self.max_bytes:
            raise ProviderUnavailable('image_cache_full')
        # Bounded terminal records; active records are never silently removed.
        while len(record['jobs']) >= 128:
            terminal = next((k for k, v in record['jobs'].items() if v.get('status') not in ('queued', 'running')), None)
            if terminal is None:
                raise ProviderUnavailable('image_job_history_full')
            del record['jobs'][terminal]
        record['jobs'][key] = {'status': 'running', 'pid': os.getpid(), 'process_start': _process_start(os.getpid()),
                               'started': time.time(), 'identity': identity}
        self._save(record, reservation)

    def queue(self, key, identity, reservation):
        record = self._load()
        if self.used_bytes()+512_000 > self.max_bytes:
            raise ProviderUnavailable('image_cache_full')
        while len(record['jobs']) >= 128:
            terminal = next((k for k, v in record['jobs'].items() if v.get('status') not in ('queued', 'running')), None)
            if terminal is None:
                raise ProviderUnavailable('image_job_history_full')
            del record['jobs'][terminal]
        record['jobs'][key] = {'status': 'queued', 'pid': os.getpid(), 'process_start': _process_start(os.getpid()),
                               'started': time.time(), 'identity': identity}
        self._save(record, reservation)

    def store(self, key, identity, display, thumbnail, reservation):
        record = self._load()
        outputs = [('display', display, '.jpg'), ('thumbnail', thumbnail, '.thumb.jpg')]
        files = {kind: {'name': key + suffix, 'bytes': len(blob), 'sha256': sha256(blob).hexdigest()}
                 for kind, blob, suffix in outputs}
        # Ownership and expected successors are durable before any image write.
        record['entries'][key] = {'status': 'publishing', 'identity': identity, 'files': files, 'last_used': time.time(), 'label': IMAGE_LABEL}
        self._save(record, reservation)
        for kind, blob, suffix in outputs:
            path = self.root / (key + suffix)
            if path.exists():
                self.budget.safe_path(path)
                if path.stat().st_size != len(blob) or sha256(path.read_bytes()).hexdigest() != files[kind]['sha256']:
                    raise ProviderUnavailable('image_owned_partial_preserved')
            else:
                self._write(path, blob, reservation)
        record['entries'][key] = {'status': 'ready', 'identity': identity, 'files': files, 'last_used': time.time(), 'label': IMAGE_LABEL}
        record['jobs'][key] = {'status': 'ready', 'finished': time.time()}
        self._save(record, reservation)
        return self.public(key)

    def recover(self, reservation):
        """Promote only complete hash-verified publications; retain partials."""
        record = self._load()
        changed = False
        for key, entry in record['entries'].items():
            if entry.get('status') != 'publishing' or not KEY_RE.fullmatch(key):
                continue
            complete = True
            for kind, suffix in [('display', '.jpg'), ('thumbnail', '.thumb.jpg')]:
                field = entry.get('files', {}).get(kind, {})
                if field.get('name') != key + suffix:
                    complete = False
                    break
                path = self.budget.safe_path(self.root / field['name'])
                if (not path.is_file() or path.stat().st_size != field.get('bytes') or path.stat().st_size > 4_000_000
                        or sha256(path.read_bytes()).hexdigest() != field.get('sha256')):
                    complete = False
                    break
            if complete:
                entry['status'] = 'ready'
                record['jobs'][key] = {'status': 'ready', 'recovered': True, 'finished': time.time()}
                changed = True
        if changed:
            self._save(record, reservation)

    def finish_error(self, key, code, reservation):
        record = self._load()
        record['jobs'][key] = {'status': 'unavailable', 'reason': code, 'finished': time.time()}
        self._save(record, reservation)

    def job_status(self, key):
        value = self._load()['jobs'].get(key)
        if not value:
            return {'status': 'unknown', 'key': key}
        if value['status'] == 'ready' and self.lookup(key):
            return self.public(key)
        # Worker state survives restarts, but a stale charge is never retried.
        if value['status'] in ('running', 'queued'):
            try:
                pid = value.get('pid', 0)
                if not isinstance(pid, int) or pid <= 0 or not value.get('process_start') or _process_start(pid) != value['process_start']:
                    raise ValueError
                os.kill(pid, 0)
            except (OSError, ValueError):
                return {'status': 'interrupted', 'key': key, 'reason': 'previous_worker_stopped_no_automatic_paid_retry'}
        return {k: v for k, v in {'status': value['status'], 'key': key, 'reason': value.get('reason')}.items() if v is not None}

    def touch(self, key, reservation):
        record = self._load()
        if key in record['entries']:
            record['entries'][key]['last_used'] = time.time()
            self._save(record, reservation)

    @staticmethod
    def public(key):
        return {'status': 'ready', 'key': key, 'url': f'/api/images/{key}.jpg',
                'thumbnail_url': f'/api/images/{key}.thumb.jpg', 'label': IMAGE_LABEL,
                'kind': 'ai_atmosphere_illustration', 'geometry_validation': False}
