"""Optional, bounded OpenAI adapters after immutable geographic evaluation.

No credentials are read from Codex or login state. Paid calls require explicit
enablement, a positive durable spending limit, and recently reviewed pricing
for the exact model/request shape. There are no automatic billable retries.
"""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
import json
import math
import multiprocessing
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from urllib.parse import urlparse

import requests

from seoul_visibility.acquisition_safety import Budget
from seoul_visibility.errors import ResourceBudgetError
from .spending import SpendingLedger, ProviderUnavailable, micro_usd
from .image_cache import (ImageCache, IMAGE_LABEL, ENTRY_PEAK_BYTES, PROMPT_VERSION,
                          canonical, image_derivatives, image_identity, image_key)

CATEGORIES = {'city', 'building', 'skyline', 'river', 'water', 'mountain', 'ridge', 'peak', 'greenery', 'green_space', 'woodland', 'park'}
FOCUSES = {'supported_scene', 'direction', 'partial_evidence', 'composition'}
MODEL_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$')
ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,149}$')


def _bool(value):
    return value is True or value == '1' or value == 'true'


def _number(value, low, high):
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError('Numeric evidence outside supported bounds')
    return value


def evidence_payload(scenes: list[dict]) -> list[dict]:
    """Allowlisted numeric/enum evidence only; names/free text are excluded.

    AI sees relative bearings and distances, never a user's origin or local
    filesystem paths. Source names can contain instructions and are unnecessary
    for selecting already-supported evidence, so they are not sent at all.
    """
    if not 1 <= len(scenes) <= 3:
        raise ValueError('AI comparison takes one to three supported views')
    output, ids = [], set()
    for scene in scenes:
        view_id = scene.get('view_id') or scene.get('id')
        if not isinstance(view_id, str) or not ID_RE.fullmatch(view_id) or view_id in ids:
            raise ValueError('Invalid or duplicate view identity')
        ids.add(view_id)
        samples = scene.get('scene_samples', [])
        if not isinstance(samples, list) or len(samples) > 100:
            raise ValueError('Bounded scene sample list required')
        safe = []
        for sample in samples:
            if sample.get('state') != 'visible':
                continue
            evidence_id = sample.get('evidence_id')
            if not isinstance(evidence_id, str) or not ID_RE.fullmatch(evidence_id) or sample.get('category') not in CATEGORIES:
                continue
            safe.append({'evidence_id': evidence_id, 'category': sample['category'],
                         'bearing_deg': _number(sample['bearing_deg'], 0, 360),
                         'distance_m': _number(sample['distance_m'], 0, 10_000)})
            if len(safe) == 20:
                break
        if not safe:
            raise ValueError('AI requires visible supported scene evidence')
        coverage = scene.get('coverage', {})
        counts = {k: _number(coverage.get(k, 0), 0, 10_000) for k in ('visible', 'blocked', 'unknown', 'excluded', 'intended')}
        if sum(counts[k] for k in ('visible', 'blocked', 'unknown', 'excluded')) != counts['intended']:
            raise ValueError('Scene denominator mismatch')
        orientation = scene.get('orientation', {})
        output.append({'view_id': view_id, 'bearing_deg': _number(orientation.get('bearing_deg', scene.get('bearing_deg')), 0, 360),
                       'fov_deg': _number(orientation.get('fov_deg', scene.get('fov_deg')), 20, 120),
                       'samples': safe, 'counts': counts,
                       'composition': scene.get('composition', {}).get('kind', 'unknown')
                       if isinstance(scene.get('composition'), dict) and scene['composition'].get('kind') in ('open', 'framed') else 'unknown'})
    return output


def validate_comparison(value: dict, payload: list[dict]) -> list[dict]:
    if not isinstance(value, dict) or set(value) != {'annotations'} or not isinstance(value['annotations'], list):
        raise ProviderUnavailable('invalid_ai_output')
    allowed = {scene['view_id']: {s['evidence_id'] for s in scene['samples']} for scene in payload}
    seen, result = set(), []
    for item in value['annotations']:
        if not isinstance(item, dict) or set(item) != {'view_id', 'evidence_ids', 'focus'}:
            raise ProviderUnavailable('invalid_ai_output')
        view_id = item['view_id']
        refs = item['evidence_ids']
        if (view_id not in allowed or view_id in seen or item['focus'] not in FOCUSES
                or not isinstance(refs, list) or not 1 <= len(refs) <= 3
                or not all(isinstance(ref, str) and ref in allowed[view_id] for ref in refs)
                or len(set(refs)) != len(refs)):
            raise ProviderUnavailable('invalid_ai_evidence_reference')
        seen.add(view_id)
        result.append(item)
    if seen != set(allowed):
        raise ProviderUnavailable('incomplete_ai_comparison')
    # Preserve deterministic rank/order even if the model shuffled its response.
    return sorted(result, key=lambda row: list(allowed).index(row['view_id']))


class OpenAITransport:
    """Only official fixed endpoints; no redirects, arbitrary URLs or retries."""
    def post(self, endpoint, key, body, max_bytes, timeout, check):
        # Requests' read timeout is an inactivity timeout. A separate owned
        # process gives the entire POST a real wall-clock deadline, including a
        # peer slowly dribbling headers/body. Never abandon a live paid worker.
        context = multiprocessing.get_context('spawn')
        receiving, sending = context.Pipe(duplex=False)
        process = context.Process(target=_transport_worker, args=(sending, endpoint, key, body, max_bytes, timeout), daemon=True)
        started = time.monotonic()
        try:
            process.start()
            sending.close()
            while time.monotonic() - started < timeout:
                check()
                if receiving.poll(min(.25, max(0, timeout-(time.monotonic()-started)))):
                    raw = receiving.recv_bytes(maxlength=max_bytes+1024)
                    value = json.loads(raw)
                    if 'error' in value:
                        raise ProviderUnavailable(value['error'])
                    return value['response']
                if not process.is_alive():
                    raise ProviderUnavailable('provider_worker_stopped')
            raise ProviderUnavailable('provider_deadline')
        except ProviderUnavailable:
            raise
        except (EOFError, OSError, ValueError, RuntimeError):
            raise ProviderUnavailable('provider_worker_invalid_response') from None
        finally:
            if process.pid is not None:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=1)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=1)
            sending.close()
            receiving.close()

    def _post_direct(self, endpoint, key, body, max_bytes, timeout, check):
        if endpoint not in ('responses', 'images/generations'):
            raise ValueError('Unsupported provider endpoint')
        started = time.monotonic()
        try:
            with requests.Session() as session:
                # Do not discover unrelated netrc credentials. Honor configured
                # network proxies explicitly; disabling a required proxy could
                # bypass the environment's authorized network path.
                session.trust_env = False
                with session.post('https://api.openai.com/v1/' + endpoint,
                                  headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'},
                                  data=canonical(body), stream=True, allow_redirects=False,
                                  proxies=requests.utils.get_environ_proxies('https://api.openai.com/v1/' + endpoint),
                                  timeout=(min(5, timeout), timeout)) as response:
                    if response.status_code != 200:
                        code = ('provider_authentication' if response.status_code in (401, 403)
                                else 'provider_rate_limited' if response.status_code == 429
                                else 'provider_http_error')
                        raise ProviderUnavailable(code)
                    if response.url != 'https://api.openai.com/v1/' + endpoint:
                        raise ProviderUnavailable('provider_redirect_rejected')
                    content_type = response.headers.get('Content-Type', '').split(';', 1)[0]
                    if content_type != 'application/json':
                        raise ProviderUnavailable('provider_format_invalid')
                    length = response.headers.get('Content-Length')
                    if length and (not length.isdigit() or int(length) > max_bytes):
                        raise ProviderUnavailable('provider_response_too_large')
                    chunks, total = [], 0
                    for chunk in response.iter_content(chunk_size=65_536):
                        check()
                        if time.monotonic() - started > timeout:
                            raise ProviderUnavailable('provider_deadline')
                        total += len(chunk)
                        if total > max_bytes:
                            raise ProviderUnavailable('provider_response_too_large')
                        chunks.append(chunk)
                    if length and total != int(length):
                        raise ProviderUnavailable('provider_truncated_response')
                    value = json.loads(b''.join(chunks))
                    if not isinstance(value, dict):
                        raise ValueError
                    return value
        except ProviderUnavailable:
            raise
        except requests.Timeout:
            raise ProviderUnavailable('provider_timeout') from None
        except requests.RequestException:
            raise ProviderUnavailable('provider_network_error') from None
        except (ValueError, UnicodeError):
            raise ProviderUnavailable('provider_invalid_json') from None


def _transport_worker(connection, endpoint, key, body, max_bytes, timeout):
    """Private child protocol: no request/credential/debug logging or files."""
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        cap = 512 * 1024**2
        if hard != resource.RLIM_INFINITY:
            cap = min(cap, hard)
        if soft != resource.RLIM_INFINITY:
            cap = min(cap, soft)
        resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
        result = OpenAITransport()._post_direct(endpoint, key, body, max_bytes, timeout, lambda: None)
        blob = canonical({'response': result})
        if len(blob) > max_bytes+1024:
            raise ProviderUnavailable('provider_response_too_large')
        connection.send_bytes(blob)
    except ProviderUnavailable as error:
        connection.send_bytes(canonical({'error': error.code}))
    except Exception:
        connection.send_bytes(canonical({'error': 'provider_worker_failed'}))
    finally:
        connection.close()


class PrototypeAI:
    def __init__(self, config: dict, budget: Budget, runtime_root: Path, *, transport=None, environ=None):
        self.config = dict(config)
        self.environ = os.environ if environ is None else environ
        self.budget, self.runtime_root = budget, budget.safe_path(runtime_root)
        self.transport = transport or OpenAITransport()
        self.calls_attempted = 0
        self._lock = threading.Lock()
        self.models = {kind: self.environ.get(f'HVF_{kind.upper()}_MODEL') or self.config.get(f'{kind}_model', '') for kind in ('text', 'image')}
        for model in self.models.values():
            if model and (not isinstance(model, str) or not MODEL_RE.fullmatch(model)):
                raise ValueError('Invalid configured model ID')
        self.max_output_tokens = self.config.get('max_output_tokens', 512)
        if not isinstance(self.max_output_tokens, int) or not 128 <= self.max_output_tokens <= 1000:
            raise ValueError('max_output_tokens must be 128..1000')
        self.ledger = SpendingLedger(self.runtime_root / 'ai', budget,
            self.config.get('daily_usd', 0), self.config.get('daily_calls', 20), self.config.get('daily_images', 5))
        self.cache = ImageCache(self.runtime_root / 'images', budget,
            self.config.get('image_cache_bytes', 250_000_000), self.config.get('image_cache_entries', 64))

    def _enabled(self):
        # Kill switch checked immediately before each POST, not only at startup.
        return (_bool(self.config.get('enabled', False)) and _bool(self.environ.get('HVF_AI_ENABLED', 'false'))
                and not _bool(self.environ.get('HVF_AI_KILL_SWITCH', 'false'))
                and not (self.runtime_root / 'ai' / 'STOP_PAID_CALLS').exists())

    def _pricing(self, kind):
        model = self.models[kind]
        prices = self.config.get('pricing', {}).get(kind, {})
        try:
            when = datetime.fromisoformat(prices['verified_at'].replace('Z', '+00:00'))
            if when.tzinfo is None or not 0 <= (datetime.now(timezone.utc)-when).total_seconds() <= 30*86400:
                raise ValueError
            url = urlparse(prices['source_url'])
            if url.scheme != 'https' or url.hostname not in ('developers.openai.com', 'platform.openai.com') or url.username or url.password:
                raise ValueError
            if prices['model'] != model or prices.get('verified') is not True:
                raise ValueError
            if kind == 'text':
                if micro_usd(prices['input_per_million_usd']) <= 0 or micro_usd(prices['output_per_million_usd']) <= 0:
                    raise ValueError
            elif (prices['size'] != '1024x1024' or prices['quality'] != 'low'
                    or micro_usd(prices['maximum_request_usd']) <= 0
                    or prices.get('includes_prompt_cost') is not True
                    or prices.get('maximum_prompt_bytes', 0) < 12_000):
                raise ValueError
            return prices
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ProviderUnavailable('verified_pricing_required') from None

    def availability(self, kind):
        if not self._enabled():
            return 'disabled'
        if not self.environ.get('OPENAI_API_KEY'):
            return 'credentials_missing'
        if not self.models[kind]:
            return 'model_not_configured'
        if not self.ledger.limit:
            return 'spending_not_authorized'
        try:
            self._pricing(kind)
        except ProviderUnavailable as error:
            return error.code
        return 'configured_not_live_verified'

    def status(self):
        try:
            spending = self.ledger.status()
        except (ProviderUnavailable, sqlite3.Error, OSError):
            spending = {'status': 'unavailable', 'reason': 'spending_ledger_unavailable',
                        'paid_calls_blocked_until_review': True}
        return {'text': self.availability('text'), 'image': self.availability('image'),
                'credentials_present': bool(self.environ.get('OPENAI_API_KEY')),
                'paid_enabled': self._enabled(), 'calls_attempted_this_process': self.calls_attempted,
                'fallback': 'deterministic_evidence_and_geometry_schematic',
                'spending': spending}

    def _authorize(self, kind):
        status = self.availability(kind)
        if status != 'configured_not_live_verified':
            raise ProviderUnavailable(status)
        return self._pricing(kind)

    def _post(self, kind, body, max_bytes, timeout, reservation):
        self._authorize(kind)
        self.calls_attempted += 1
        return self.transport.post('responses' if kind == 'text' else 'images/generations',
            self.environ['OPENAI_API_KEY'], body, max_bytes, timeout, reservation.observe)

    def compare(self, scenes):
        fallback = {'status': 'unavailable', 'method': 'deterministic_template', 'annotations': []}
        try:
            prices = self._authorize('text')
            payload = evidence_payload(scenes)
            ids = [p['view_id'] for p in payload]
            refs = sorted({s['evidence_id'] for p in payload for s in p['samples']})
            schema = {'type': 'object', 'additionalProperties': False, 'required': ['annotations'], 'properties': {
                'annotations': {'type': 'array', 'minItems': len(ids), 'maxItems': len(ids), 'items': {
                    'type': 'object', 'additionalProperties': False, 'required': ['view_id', 'evidence_ids', 'focus'],
                    'properties': {'view_id': {'type': 'string', 'enum': ids},
                        'evidence_ids': {'type': 'array', 'minItems': 1, 'maxItems': 3, 'items': {'type': 'string', 'enum': refs}},
                        'focus': {'type': 'string', 'enum': sorted(FOCUSES)}}}}}}
            body = {'model': self.models['text'], 'store': False, 'max_output_tokens': self.max_output_tokens,
                    'input': [{'role': 'system', 'content': 'Select one presentation focus and up to three supplied visible evidence IDs for EACH view. The numeric evidence is immutable. Preserve all views. Unknown samples remain unknown. Select partial_evidence for incomplete context. No tools, browsing, new claims or ranking changes.'},
                              {'role': 'user', 'content': canonical({'views': payload}).decode()}],
                    'text': {'format': {'type': 'json_schema', 'name': 'view_evidence_focus', 'strict': True, 'schema': schema}}}
            blob = canonical(body)
            if len(blob) > 24_000:
                raise ProviderUnavailable('ai_input_limit')
            # A UTF-8 byte per token plus 1024 protocol tokens is conservative
            # for these bounded JSON inputs; no tools/images/context history.
            cost = (Decimal(len(blob)+1024) * Decimal(str(prices['input_per_million_usd']))
                    + Decimal(self.max_output_tokens) * Decimal(str(prices['output_per_million_usd']))) / 1_000_000
            if not self._lock.acquire(blocking=False):
                raise ProviderUnavailable('provider_busy')
            try:
                with self.budget.reserve(5_000_000, 5_000_000, 'prototype structured AI comparison') as reservation:
                    receipt = self.ledger.reserve(cost, 'text', reservation=reservation)
                    response = self._post('text', body, 128_000, 15, reservation)
                    if response.get('status') != 'completed':
                        raise ProviderUnavailable('provider_incomplete')
                    texts = [item['text'] for output in response.get('output', []) if output.get('type') == 'message'
                             for item in output.get('content', []) if item.get('type') == 'output_text']
                    if len(texts) != 1:
                        raise ProviderUnavailable('provider_refusal_or_invalid_output')
                    annotations = validate_comparison(json.loads(texts[0]), payload)
                    return {'status': 'completed', 'method': 'openai_structured_evidence_selection', 'annotations': annotations,
                            'geometry_changed': False, 'rank_changed': False, 'reservation': receipt}
            finally:
                self._lock.release()
        except ResourceBudgetError:
            return {**fallback, 'reason': 'storage_blocked'}
        except ProviderUnavailable as error:
            return {**fallback, 'reason': error.code}
        except (ValueError, KeyError, TypeError, sqlite3.Error):
            return {**fallback, 'reason': 'invalid_evidence_or_response'}

    def image_key(self, scene):
        return image_key(scene, self.models['image'])

    def generate_image(self, scene):
        fallback = {'status': 'unavailable', 'label': IMAGE_LABEL, 'fallback': 'geometry_schematic'}
        key = None
        try:
            payload = evidence_payload([scene])[0]
            key = self.image_key(scene)
            if self.cache.lookup(key):
                return self.cache.public(key)
            prices = self._authorize('image')
            identity = image_identity(scene, self.models['image'])
            # Private origin, full source descriptions and exact position are
            # excluded from the external brief. Geometry identity stays local.
            brief = {k: identity[k] for k in ('bearing_deg', 'fov_deg', 'solar_altitude_class_deg', 'solar_azimuth_class_deg', 'season', 'weather_class', 'weather_basis')}
            brief['samples'] = payload['samples']
            brief['support_counts'] = payload['counts']
            prompt = ('Create a soft, non-photoreal atmosphere illustration of this PARTIAL model-supported view. '
                      'Use only the supplied relative directions and category-level components. Unknown sectors should remain indistinct. '
                      'Do not invent cafes, benches, access, people, decorative details, canopy heights, reflections, lighting schedules or clear weather. '
                      'Mapped wooded terrain does not establish visible tree canopy. Roof samples do not establish visible facades. '
                      'Solar classes are approximate; atmospheric conditions are unknown unless explicitly supplied. '
                      'This is not geographic validation or a photograph. Data: ' + canonical(brief).decode())
            if len(prompt.encode()) > 12_000:
                raise ProviderUnavailable('ai_input_limit')
            body = {'model': self.models['image'], 'prompt': prompt, 'n': 1, 'size': '1024x1024',
                    'quality': 'low', 'output_format': 'jpeg', 'output_compression': 75}
            if not self._lock.acquire(blocking=False):
                raise ProviderUnavailable('provider_busy')
            try:
                with self.budget.reserve(ENTRY_PEAK_BYTES+5_000_000, ENTRY_PEAK_BYTES+5_000_000, 'prototype atmosphere image') as reservation:
                    self.cache.recover(reservation)
                    if self.cache.lookup(key):
                        return self.cache.public(key)
                    self.cache.prepare(key, identity, reservation)
                    try:
                        receipt = self.ledger.reserve(prices['maximum_request_usd'], 'image', reservation=reservation)
                        response = self._post('image', body, 12_000_000, 60, reservation)
                        data = response.get('data')
                        if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0].get('b64_json'), str):
                            raise ProviderUnavailable('provider_image_schema_invalid')
                        encoded = data[0]['b64_json']
                        if len(encoded) > 10_666_672:
                            raise ProviderUnavailable('image_response_too_large')
                        try:
                            blob = base64.b64decode(encoded, validate=True)
                        except ValueError:
                            raise ProviderUnavailable('image_invalid_base64') from None
                        display, thumbnail = image_derivatives(blob)
                        result = self.cache.store(key, identity, display, thumbnail, reservation)
                        return {**result, 'reservation': receipt}
                    except ProviderUnavailable as error:
                        self.cache.finish_error(key, error.code, reservation)
                        raise
            finally:
                self._lock.release()
        except ResourceBudgetError:
            return {**fallback, 'key': key, 'reason': 'storage_blocked'}
        except ProviderUnavailable as error:
            return {**fallback, 'key': key, 'reason': error.code}
        except (ValueError, KeyError, TypeError, sqlite3.Error):
            return {**fallback, 'key': key, 'reason': 'invalid_evidence_or_response'}

    def cached_image(self, key, thumbnail=False):
        path = self.cache.lookup(key, thumbnail)
        if path and self._lock.acquire(blocking=False):
            try:
                # A read remains available if a write/low disk prevents an LRU
                # timestamp update. This never evicts during an asset read.
                with self.budget.reserve(1_000_000, 1_000_000, 'prototype image cache access') as reservation:
                    self.cache.touch(key, reservation)
            except (ResourceBudgetError, ProviderUnavailable):
                pass
            finally:
                self._lock.release()
        return path


class ImageJobs:
    """One worker, at most two pending jobs, 32 in-memory terminal statuses.

    Identical requests share one job. Shutdown cancels unstarted jobs and waits
    for the active bounded HTTP request; it never leaves an unlimited worker.
    """
    def __init__(self, provider: PrototypeAI):
        self.provider = provider
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='hvf-image')
        self.lock = threading.Lock()
        self.jobs = {}
        self.closed = False

    def submit(self, scene):
        try:
            key = self.provider.image_key(scene)
            if self.provider.cached_image(key):
                return self.provider.cache.public(key)
            state = self.provider.availability('image')
            if state != 'configured_not_live_verified':
                return {'status': 'unavailable', 'reason': state, 'fallback': 'geometry_schematic', 'key': key}
            durable = self.provider.cache.job_status(key)
            if durable['status'] in ('queued', 'running'):
                with self.lock:
                    previous = self.jobs.get(key)
                    if previous is None or not previous.done():
                        return {**durable, 'coalesced': True}
        except (ValueError, ProviderUnavailable):
            return {'status': 'unavailable', 'reason': 'invalid_or_unsupported_view'}
        with self.lock:
            if self.closed:
                return {'status': 'unavailable', 'reason': 'service_stopping'}
            previous = self.jobs.get(key)
            if previous and not previous.done():
                return {'status': 'queued', 'key': key, 'coalesced': True}
            if sum(not future.done() for future in self.jobs.values()) >= 2:
                return {'status': 'unavailable', 'reason': 'image_queue_full'}
            self.jobs = {k: v for k, v in list(self.jobs.items())[-31:] if not v.cancelled()}
            # JSON roundtrip isolates an immutable snapshot from later ranking/UI
            # mutations; the payload size is bounded by the scene schema.
            blob = canonical(scene)
            if len(blob) > 128_000:
                return {'status': 'unavailable', 'reason': 'image_scene_payload_limit'}
            try:
                # Record intent before the worker is started. A busy shared
                # writer refuses this new costly job; cached reads still work.
                with self.provider.budget.reserve(1_000_000, 1_000_000, 'prototype image job queue') as reservation:
                    self.provider.cache.queue(key, image_identity(scene, self.provider.models['image']), reservation)
            except ResourceBudgetError:
                return {'status': 'unavailable', 'key': key, 'reason': 'storage_blocked'}
            except ProviderUnavailable as error:
                return {'status': 'unavailable', 'key': key, 'reason': error.code}
            self.jobs[key] = self.executor.submit(self.provider.generate_image, json.loads(blob))
            return {'status': 'queued', 'key': key, 'coalesced': False}

    def status(self, key):
        with self.lock:
            future = self.jobs.get(key)
        if future:
            if future.cancelled():
                return {'status': 'cancelled', 'key': key}
            if not future.done():
                return {'status': 'running' if future.running() else 'queued', 'key': key}
            try:
                return future.result()
            except Exception:
                return {'status': 'unavailable', 'key': key, 'reason': 'image_worker_failed'}
        return self.provider.cache.job_status(key)

    def close(self):
        with self.lock:
            self.closed = True
        self.executor.shutdown(wait=True, cancel_futures=True)
        for key, future in list(self.jobs.items()):
            if future.cancelled():
                try:
                    with self.provider.budget.reserve(1_000_000, 1_000_000, 'prototype image job cancellation') as reservation:
                        self.provider.cache.finish_error(key, 'service_stopped_before_generation', reservation)
                except (ResourceBudgetError, ProviderUnavailable):
                    # Preserve the durable queued record rather than deleting
                    # it; PID/start-time validation reports it interrupted.
                    pass
