"""Bounded acquisition primitives shared by the citywide runner.

The byte ceiling is application policy, not an OS quota. The writer flock only
coordinates users of this module; unrelated writers can still race free-space
checks. Reserve conservative peaks and a lag margin before native child writers.
All temporary files and caches must remain under the declared accounted roots.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import errno
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import random
import re
import shutil
import signal
import stat
import subprocess
import time
from typing import Callable, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from zipfile import ZipFile

from .resources import GiB, HARD_TOTAL_STORAGE_BYTES, available_memory_bytes
from .errors import ResourceBudgetError

REPORT_RESERVE = 16 * 1024 * 1024
MAX_METADATA_BYTES = 8 * 1024 * 1024
CHUNK = 1024 * 1024
# Buffered filesystem writers can reserve extents beyond EOF until close. This
# is included in both total and staging peak estimates, never extra allowance.
DOWNLOAD_ALLOCATION_MARGIN = 16 * 1024 * 1024


class AcquisitionError(RuntimeError):
    def __init__(self, status: str, message: str):
        self.status = status
        super().__init__(f"{status}: {message}")


def redact_url(url: str) -> str:
    """Keep source addresses useful without serializing credentials or signed URLs."""
    p = urlsplit(url)
    host = p.hostname or ''
    if p.port:
        host += f':{p.port}'
    secret = re.compile(r'token|secret|password|credential|signature|authorization|api.?key|access.?key|^key$', re.I)
    query = urlencode([(k, '[REDACTED]' if secret.search(k) or k.lower().startswith('x-amz-') else v)
                       for k, v in parse_qsl(p.query, keep_blank_values=True)])
    return urlunsplit((p.scheme, host, p.path, query, ''))


def sha256(path: Path) -> str:
    with Path(path).open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def _integer(value: int, name: str, maximum: int | None = None) -> int:
    if type(value) is not int or value < 0 or (maximum is not None and value > maximum):
        raise ResourceBudgetError(f'{name} must be a nonnegative integer <= {maximum or "unbounded"}')
    return value


def _inside(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


def _temporary(path: Path, stage_root: Path, roots: tuple[Path, ...] = ()) -> bool:
    relative = next((path.relative_to(root) for root in roots if path.is_relative_to(root)), path)
    return (path == stage_root or path.is_relative_to(stage_root)
            or any(part in {'.staging', 'staging', '.cache', 'cache', 'tmp', '.tmp'} or part.endswith(('.staging', '-staging', '.partial', '-partial', '.partdir', '-partdir')) for part in relative.parts)
            or path.name.endswith(('.part', '.tmp', '.writing', '-wal', '-shm', '-journal'))
            or '.partial.' in path.name or '.part.' in path.name)


class Budget:
    """Account files once by inode and serialize every large writer with flock.

    `incremental_bytes` is an uncertain *simultaneous* peak estimate. Reservations
    add at least 25%, plus a protected checkpoint allowance. Temporary bytes are a
    subset of the same peak, never a second additional-download budget.
    """
    def __init__(self, root: str | Path, *, extra_roots=(), limit=HARD_TOTAL_STORAGE_BYTES,
                 stage_root: str | Path | None = None, min_free=8 * GiB,
                 stage_limit=4 * GiB, additional_accounted_bytes: int = 0,
                 inherited_reservation: bool = False):
        self.root = Path(root).absolute().resolve()
        declared = [self.root, *(Path(p).absolute().resolve() for p in extra_roots)]
        self.roots = tuple(p for i, p in enumerate(declared)
                           if not any(p.is_relative_to(q) for q in declared[:i])
                           and not any(p != q and p.is_relative_to(q) for q in declared[i + 1:]))
        self.limit = _integer(limit, 'total ceiling', HARD_TOTAL_STORAGE_BYTES)
        self.additional_accounted_bytes = _integer(additional_accounted_bytes, 'additional accounted footprint', HARD_TOTAL_STORAGE_BYTES)
        self.min_free = _integer(min_free, 'minimum free space')
        if self.min_free < 8 * GiB:
            raise ResourceBudgetError('The citywide pipeline must retain at least 8 GiB free space')
        self.stage_limit = _integer(stage_limit, 'temporary ceiling', 4 * GiB)
        self.stage_root = Path(stage_root or self.root / 'data/.citywide-staging').absolute().resolve()
        if not _inside(self.stage_root, self.roots):
            raise ResourceBudgetError('Staging directory must be inside an accounted root')
        self._reservation: Reservation | None = None
        self._checkpoint_active = False
        self._lock_fd: int | None = None
        self._reservation_id: str | None = None
        self._delegated = False
        self.peak_accounted_bytes = 0
        self.peak_temporary_bytes = 0
        self.minimum_observed_free: dict[str, int] = {}
        self.lock_path = self.root / '.citywide-writer.lock'
        if inherited_reservation:
            self._attach_parent_reservation()

    def _attach_parent_reservation(self):
        """Accept only a descriptor explicitly inherited from the live parent.

        Matching metadata alone is insufficient: a separately opened descriptor
        cannot acquire the parent's flock. Child processes never unlock the
        inherited open-file description; the parent owns its lifetime.
        """
        try:
            fd = int(os.environ['HVF_BUDGET_RESERVATION_FD'])
            parent_pid = int(os.environ['HVF_BUDGET_PARENT_PID'])
            identity = os.environ['HVF_BUDGET_RESERVATION_ID']
            if parent_pid != os.getppid() or not identity:
                raise ValueError('delegated parent mismatch')
            inherited = os.fstat(fd)
            expected = self.lock_path.stat()
            if (inherited.st_dev, inherited.st_ino) != (expected.st_dev, expected.st_ino):
                raise ValueError('delegated descriptor does not match writer lock')
            payload = os.pread(fd, 65537, 0)
            if len(payload) > 65536:
                raise ValueError('delegated lock metadata too large')
            record = json.loads(payload)
            if (record['pid'] != parent_pid or record['reservation_id'] != identity
                    or sorted(record['roots']) != sorted(map(str, self.roots))
                    or record['total_limit'] != self.limit
                    or record['minimum_free_bytes'] != self.min_free
                    or record['stage_limit'] != self.stage_limit
                    or record['stage_root'] != str(self.stage_root)
                    or record['additional_accounted_bytes'] != self.additional_accounted_bytes):
                raise ValueError('delegated budget parameters do not match parent reservation')
            # Success on this fd confirms the inherited open-file description;
            # an independent invocation's fd would conflict with the live lock.
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._reservation = Reservation(self, record['peak_bytes'], record['temporary_bytes'],
                                            record['baseline_bytes'], record['temporary_baseline_bytes'])
            self._lock_fd = fd
            self._reservation_id = identity
            self._delegated = True
            self._reservation.observe()
        except (KeyError, ValueError, TypeError, OSError) as exc:
            raise ResourceBudgetError('storage_blocked: no valid live inherited writer reservation; use the parent citywide command') from exc

    def safe_path(self, path: str | Path) -> Path:
        path = Path(path).absolute()
        resolved = path.resolve()
        if not _inside(resolved, self.roots):
            raise ResourceBudgetError(f'Output is outside accounted roots (including symlink resolution): {path}')
        # Reject even internal symlinks for output locations, eliminating alias
        # ambiguity and preventing replacement of a symlink target on publication.
        for candidate in (path, *path.parents):
            if candidate.is_symlink():
                raise ResourceBudgetError(f'Symlink output path is not permitted: {candidate}')
            if candidate in self.roots:
                break
        return resolved

    def snapshot(self) -> dict:
        _integer(self.additional_accounted_bytes, 'additional accounted footprint', HARD_TOTAL_STORAGE_BYTES)
        seen: set[tuple[int, int]] = set()
        temporary_seen: set[tuple[int, int]] = set()
        device_paths: dict[int, str] = {}
        logical = allocated = accounted = temporary = files = 0
        filesystems: dict[int, dict] = {}
        stage = os.fspath(self.stage_root)
        root_strings = tuple(os.fspath(root) for root in self.roots)
        stage_prefix = stage + os.sep
        temporary_names = {'.staging', 'staging', '.cache', 'cache', 'tmp', '.tmp'}
        # Retain DirEntry.stat results instead of throwing them away then doing
        # another Path/lstat/relative_to chain for every entry. Temporary state
        # propagates down directories; it never needs repeated root resolution.
        stack = [(root, None, root == stage or root.startswith(stage_prefix)) for root in root_strings]
        while stack:
            path, info, is_temporary = stack.pop()
            try:
                info = info if info is not None else os.lstat(path)
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode):
                target = os.path.realpath(path)
                if not any(target == root or target.startswith(root + os.sep) for root in root_strings):
                    raise ResourceBudgetError(f'Accounted tree contains symlink escape: {path}')
            key = (info.st_dev, info.st_ino)
            nallocated = getattr(info, 'st_blocks', 0) * 512
            naccounted = max(info.st_size, nallocated)
            if is_temporary and key not in temporary_seen:
                temporary += naccounted
                temporary_seen.add(key)
            if key in seen:
                continue
            seen.add(key)
            is_directory = stat.S_ISDIR(info.st_mode)
            device_paths.setdefault(info.st_dev, path if is_directory else os.path.dirname(path))
            logical += info.st_size
            allocated += nallocated
            accounted += naccounted
            if is_directory:
                with os.scandir(path) as entries:
                    for entry in entries:
                        name = entry.name
                        child_temporary = (is_temporary or entry.path == stage or name in temporary_names
                            or name.endswith(('.staging', '-staging', '.partial', '-partial', '.partdir', '-partdir'))
                            or name.endswith(('.part', '.tmp', '.writing', '-wal', '-shm', '-journal'))
                            or '.partial.' in name or '.part.' in name)
                        try:
                            entry_info = entry.stat(follow_symlinks=False)
                        except FileNotFoundError:
                            continue
                        stack.append((entry.path, entry_info, child_temporary))
            else:
                files += 1
        for root in (*root_strings, stage, *device_paths.values()):
            parent = root
            while not os.path.exists(parent):
                parent = os.path.dirname(parent)
            device = os.stat(parent).st_dev
            if device not in filesystems:
                usage = shutil.disk_usage(parent)
                vfs = os.statvfs(parent)
                filesystems[device] = {'device': device, 'path': parent,
                    'free_bytes': usage.free, 'total_bytes': usage.total,
                    'available_inodes': vfs.f_favail,
                    'quota': 'not asserted; statvfs headroom only'}
                key = str(device)
                self.minimum_observed_free[key] = min(self.minimum_observed_free.get(key, usage.free), usage.free)
        measured_accounted = accounted
        accounted += self.additional_accounted_bytes
        self.peak_accounted_bytes = max(self.peak_accounted_bytes, accounted)
        self.peak_temporary_bytes = max(self.peak_temporary_bytes, temporary)
        return {'logical_bytes': logical, 'allocated_bytes': allocated,
                'accounted_bytes': accounted, 'temporary_bytes': temporary,
                'measured_accounted_bytes': measured_accounted,
                'additional_accounted_bytes': self.additional_accounted_bytes,
                'additional_accounted_measurement': 'conservative external/task footprint allowance; not filesystem-measured',
                'file_count': files, 'filesystems': list(filesystems.values()),
                'total_ceiling_bytes': self.limit, 'temporary_ceiling_bytes': self.stage_limit,
                'minimum_free_bytes': self.min_free, 'checkpoint_reserve_bytes': REPORT_RESERVE,
                'observed_peak_accounted_bytes': self.peak_accounted_bytes,
                'observed_peak_temporary_bytes': self.peak_temporary_bytes,
                'peak_measurement': 'sampled between guarded writes; not an OS quota',
                'roots': [str(p) for p in self.roots]}

    def check(self, additional: int = 0, temporary: int = 0, path: str | Path | None = None,
              *, checkpoint=False) -> dict:
        _integer(self.limit, 'total ceiling', HARD_TOTAL_STORAGE_BYTES)
        _integer(self.stage_limit, 'temporary ceiling', 4 * GiB)
        if self.min_free < 8 * GiB:
            raise ResourceBudgetError('The citywide pipeline must retain at least 8 GiB free space')
        _integer(additional, 'incremental bytes')
        _integer(temporary, 'incremental temporary bytes')
        if path is not None:
            self.safe_path(path)
        snap = self.snapshot()
        protected = 0 if checkpoint else REPORT_RESERVE
        if snap['accounted_bytes'] + additional + protected > self.limit:
            raise ResourceBudgetError(f'storage_blocked: accounted {snap["accounted_bytes"]} + incremental '
                                      f'{additional} + checkpoint {protected} exceeds {self.limit}')
        if snap['temporary_bytes'] + temporary + protected > self.stage_limit:
            raise ResourceBudgetError('storage_blocked: aggregate staging/temporary footprint exceeds ceiling')
        if self._delegated and self._reservation is not None and not checkpoint:
            reservation = self._reservation
            if max(0, snap['accounted_bytes'] - reservation.baseline) + additional > reservation.peak:
                raise ResourceBudgetError('storage_blocked: delegated writer cannot exceed parent peak reservation')
            if max(0, snap['temporary_bytes'] - reservation.temporary_baseline) + temporary > reservation.temporary_peak:
                raise ResourceBudgetError('storage_blocked: delegated writer cannot exceed parent temporary reservation')
        # Conservatively apply the whole growth to every receiving filesystem;
        # this never grants the same headroom twice across separate mounts.
        for fs in snap['filesystems']:
            if fs['free_bytes'] - additional - protected < self.min_free:
                raise ResourceBudgetError(f'storage_blocked: filesystem {fs["device"]} free {fs["free_bytes"]} '
                                          f'cannot fit {additional} plus reserve {self.min_free + protected}')
            if fs['available_inodes'] == 0:
                raise ResourceBudgetError('storage_blocked: no available inodes')
        return snap

    @contextmanager
    def checkpoint_writer(self):
        """Serialize emergency metadata while allowing the protected reserve.

        Reuse an active stage's lock. A standalone checkpoint obtains the same
        flock without demanding another normal-stage reserve, so it can still
        report storage pressure. It never bypasses the total or staging ceiling.
        """
        if self._reservation is not None or self._checkpoint_active:
            yield
            return
        self.check(8192, path=self.lock_path, checkpoint=True)
        self.root.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.safe_path(self.lock_path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        acquired = False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError as exc:
                raise ResourceBudgetError('storage_blocked: another pipeline writer holds the shared reservation lock') from exc
            self.check(8192, checkpoint=True)
            self._checkpoint_active = True
            yield
        finally:
            self._checkpoint_active = False
            if acquired:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @contextmanager
    def reserve(self, incremental_bytes: int, temporary_bytes: int = 0, label: str = '',
                *, safety_factor: float = 1.25) -> Iterator['Reservation']:
        _integer(incremental_bytes, 'peak incremental bytes')
        _integer(temporary_bytes, 'peak temporary bytes')
        if safety_factor < 1.25 or not math.isfinite(safety_factor):
            raise ResourceBudgetError('Uncertain peak estimates require at least 25% safety margin')
        if self._reservation is not None:
            raise ResourceBudgetError('Nested writer reservations are not allowed')
        # Include lock, JSON sidecars, directory allocations, and filesystem blocks.
        peak = math.ceil(incremental_bytes * safety_factor) + REPORT_RESERVE
        temp_peak = math.ceil(temporary_bytes * safety_factor)
        self.check(peak, temp_peak, self.lock_path)
        self.root.mkdir(parents=True, exist_ok=True)
        # Never unlink this lock: unlinking a live inode creates a second lock.
        fd = os.open(self.safe_path(self.lock_path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        acquired = False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError as exc:
                raise ResourceBudgetError('storage_blocked: another pipeline writer holds the shared reservation lock') from exc
            baseline = self.check(peak, temp_peak)
            self._reservation_id = os.urandom(16).hex()
            payload = json.dumps({'pid': os.getpid(), 'label': label[:200], 'peak_bytes': peak,
                                  'temporary_bytes': temp_peak, 'started_unix': time.time(),
                                  'reservation_id': self._reservation_id,
                                  'baseline_bytes': baseline['accounted_bytes'],
                                  'temporary_baseline_bytes': baseline['temporary_bytes'],
                                  'roots': list(map(str, self.roots)), 'total_limit': self.limit,
                                  'minimum_free_bytes': self.min_free, 'stage_limit': self.stage_limit,
                                  'stage_root': str(self.stage_root),
                                  'additional_accounted_bytes': self.additional_accounted_bytes}).encode()
            os.ftruncate(fd, 0)
            os.write(fd, payload)
            os.fsync(fd)
            reservation = Reservation(self, peak, temp_peak, baseline['accounted_bytes'], baseline['temporary_bytes'])
            self._reservation = reservation
            self._lock_fd = fd
            yield reservation
            reservation.observe()
        finally:
            self._reservation = None
            self._lock_fd = None
            self._reservation_id = None
            if acquired:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


@dataclass
class Reservation:
    budget: Budget
    peak: int
    temporary_peak: int
    baseline: int
    temporary_baseline: int
    _small_batch_mode: bool = False
    _small_credit: int = 0
    _small_entries: int = 0
    _small_checked_at: float = 0.0
    _small_snapshot: dict | None = None

    @contextmanager
    def bounded_small_writes(self):
        """Authorize at most 4 MiB / 64 small writes / one second per scan.

        This is prospective byte credit inside the existing reservation. Every
        payload and metadata write consumes its bytes plus 8 KiB for blocks and
        directories. Writes >=64 KiB always receive a full fresh scan. Available
        disk bytes are still checked before every small write. Closing/resetting
        the context samples actual allocated space, including journals/aliases.
        """
        if self._small_batch_mode:
            raise ResourceBudgetError('Nested small-write batches are not allowed')
        self._small_batch_mode = True
        try:
            yield self
            self.observe()
        finally:
            self._small_batch_mode = False
            self._small_credit = 0
            self._small_snapshot = None

    @property
    def remaining(self) -> int:
        return max(0, self.peak - max(0, self.budget.snapshot()['accounted_bytes'] - self.baseline))

    def check_write(self, n: int, path: str | Path) -> dict:
        # Include one filesystem block and one possible directory allocation.
        n = _integer(n, 'write bytes') + 8192
        target = self.budget.safe_path(path)
        if self._small_batch_mode and n < 64 * 1024:
            if (self._small_credit < n or self._small_entries >= 64
                    or time.monotonic() - self._small_checked_at >= 1.0):
                snap = self.budget.check(path=target)
                growth = max(0, snap['accounted_bytes'] - self.baseline)
                growth_temp = max(0, snap['temporary_bytes'] - self.temporary_baseline)
                available = min(self.peak - growth, self.temporary_peak - growth_temp,
                                self.budget.limit - snap['accounted_bytes'] - REPORT_RESERVE,
                                self.budget.stage_limit - snap['temporary_bytes'] - REPORT_RESERVE,
                                *(fs['free_bytes'] - self.budget.min_free - REPORT_RESERVE for fs in snap['filesystems']))
                # Treat every credit byte as temporary, a conservative bound even
                # when some files are immediately renamed to permanent members.
                self._small_credit = min(4 * 1024 * 1024, available)
                self._small_entries = 0
                self._small_checked_at = time.monotonic()
                self._small_snapshot = snap
            if self._small_credit < n:
                raise ResourceBudgetError('storage_blocked: bounded small-write credit cannot fit next write')
            assert self._small_snapshot is not None
            for fs in self._small_snapshot['filesystems']:
                free = shutil.disk_usage(fs['path']).free
                if free - self._small_credit - REPORT_RESERVE < self.budget.min_free:
                    raise ResourceBudgetError('storage_blocked: filesystem free space fell during bounded small-write batch')
            self._small_credit -= n
            self._small_entries += 1
            return self._small_snapshot
        self._small_credit = 0
        snap = self.budget.check(n, n if _temporary(target, self.budget.stage_root, self.budget.roots) else 0, target)
        growth = max(0, snap['accounted_bytes'] - self.baseline)
        if growth + n > self.peak:
            raise ResourceBudgetError(f'storage_blocked: writer exhausted reserved peak before write; baseline={self.baseline}, current_accounted={snap["accounted_bytes"]}, logical={snap["logical_bytes"]}, allocated={snap["allocated_bytes"]}, proposed={n}, reserved_incremental={self.peak}')
        if _temporary(target, self.budget.stage_root, self.budget.roots):
            growth_temp = max(0, snap['temporary_bytes'] - self.temporary_baseline)
            if growth_temp + n > self.temporary_peak:
                raise ResourceBudgetError(f'storage_blocked: writer exhausted temporary reservation before write; baseline_temporary={self.temporary_baseline}, current_temporary={snap["temporary_bytes"]}, proposed={n}, reserved_temporary={self.temporary_peak}')
        return snap

    def observe(self) -> dict:
        self._small_credit = 0
        snap = self.budget.check()
        if max(0, snap['accounted_bytes'] - self.baseline) > self.peak:
            raise ResourceBudgetError('storage_blocked: reserved peak exceeded by external writer')
        if max(0, snap['temporary_bytes'] - self.temporary_baseline) > self.temporary_peak:
            raise ResourceBudgetError('storage_blocked: temporary reservation exceeded by external writer')
        return snap


def atomic_json(path: str | Path, value: dict, budget: Budget | None = None, *, checkpoint=False) -> None:
    path = Path(path)
    blob = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()
    if len(blob) > MAX_METADATA_BYTES:
        raise AcquisitionError('schema_error', 'JSON metadata exceeds bounded 8 MiB allowance')
    temporary = path.with_name(path.name + '.writing')
    if budget and checkpoint and budget._reservation is None and not budget._checkpoint_active:
        with budget.checkpoint_writer():
            return atomic_json(path, value, budget, checkpoint=True)
    if budget and budget._reservation is None and not checkpoint:
        with budget.reserve(len(blob) + 16384, len(blob) + 16384, 'atomic metadata'):
            return atomic_json(path, value, budget)
    if budget:
        budget.safe_path(path)
        budget.safe_path(temporary)
        if budget._reservation is not None and not checkpoint:
            budget._reservation.check_write(len(blob), temporary)
        else:
            budget.check(len(blob) + 8192, len(blob) + 8192, path, checkpoint=checkpoint)
    path.parent.mkdir(parents=True, exist_ok=True)
    # A crash leaves this owned staging name. Preserve and report it: automatic
    # deletion could race a live caller. Main orchestration owns recovery policy.
    with temporary.open('xb') as handle:
        handle.write(blob)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def preallocate_keep_size(handle, size: int, path: str | Path, reservation: Reservation) -> dict:
    """Reserve an exact bounded file extent without changing logical length.

    Linux KEEP_SIZE avoids speculative buffered-write allocation doubling at
    power-of-two boundaries and preserves partial length for HTTP Range resume.
    If unavailable, this writer stops before payload writes; it never silently
    falls back to an allocation pattern whose peak is not bounded.
    """
    import ctypes
    import sys
    _integer(size, 'preallocated artifact bound')
    target = reservation.budget.safe_path(path)
    before = os.fstat(handle.fileno())
    if size < before.st_size:
        raise AcquisitionError('source_changed', 'Preallocation bound is below retained partial length')
    if size == 0:
        return {'method': 'empty artifact; no allocation', 'reserved_size_bytes': 0,
                'logical_size_before_bytes': before.st_size, 'allocated_bytes': 0}
    if not sys.platform.startswith('linux'):
        raise AcquisitionError('preallocation_unavailable', 'Bounded writer requires Linux KEEP_SIZE allocation on this filesystem; partial preserved')
    fragment = os.fstatvfs(handle.fileno()).f_frsize or 4096
    rounded = ((size + fragment - 1) // fragment) * fragment
    previous_accounted = getattr(before, 'st_blocks', 0) * 512
    reservation.check_write(max(0, rounded - previous_accounted), target)
    libc = ctypes.CDLL(None, use_errno=True)
    allocate = getattr(libc, 'fallocate', None)
    if allocate is None:
        raise AcquisitionError('preallocation_unavailable', 'libc has no fallocate; bounded writer stopped before payload')
    allocate.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_longlong, ctypes.c_longlong)
    allocate.restype = ctypes.c_int
    # FALLOC_FL_KEEP_SIZE = 1. This does not mark unwritten bytes as downloaded.
    if allocate(handle.fileno(), 1, 0, size) != 0:
        error = ctypes.get_errno()
        if error in {errno.ENOSPC, errno.EDQUOT}:
            raise ResourceBudgetError('storage_blocked: filesystem cannot reserve bounded artifact extent')
        raise AcquisitionError('preallocation_unavailable', f'Filesystem rejected KEEP_SIZE allocation (errno {error}); partial preserved')
    after = os.fstat(handle.fileno())
    if after.st_size != before.st_size:
        raise AcquisitionError('preallocation_protocol_error', 'KEEP_SIZE unexpectedly changed logical length; preserve and inspect')
    allocated_growth = max(0, getattr(after, 'st_blocks', 0) * 512 - getattr(before, 'st_blocks', 0) * 512)
    if (not reservation._small_batch_mode or size >= 64 * 1024
            or allocated_growth > max(0, rounded - previous_accounted) + 8192):
        reservation.observe()
    return {'method': 'Linux fallocate FALLOC_FL_KEEP_SIZE', 'reserved_size_bytes': size,
            'logical_size_before_bytes': before.st_size,
            'allocated_bytes': getattr(after, 'st_blocks', 0) * 512,
            'growth_allocated_bytes': max(0, getattr(after, 'st_blocks', 0) * 512 - getattr(before, 'st_blocks', 0) * 512)}


def _validate_file(path: Path, expected_size=None, expected_sha256=None, magic=None,
                   validator: Callable[[Path], None] | None = None) -> str:
    size = path.stat().st_size
    if not size or (expected_size is not None and size != expected_size):
        raise AcquisitionError('corrupt_content', f'Unexpected size {size}; expected {expected_size}')
    with path.open('rb') as stream:
        head = stream.read(512)
    if head.lstrip().lower().startswith((b'<!doctype html', b'<html', b'<head', b'<body')):
        raise AcquisitionError('corrupt_content', 'HTML/login/error document instead of geographic data')
    if magic:
        prefixes = (magic,) if isinstance(magic, bytes) else tuple(magic)
        if not any(head.startswith(prefix) for prefix in prefixes):
            raise AcquisitionError('corrupt_content', 'Format magic does not match declared source')
    digest = sha256(path)
    if expected_sha256 and digest.lower() != expected_sha256.lower():
        raise AcquisitionError('source_changed', 'Pinned SHA-256 mismatch; preserve source and inspect upstream version')
    if validator:
        validator(path)
    return digest


def _retry_delay(error: Exception, attempt: int) -> float:
    retry_after = getattr(error, 'headers', {}).get('Retry-After') if getattr(error, 'headers', None) else None
    if retry_after:
        try:
            delay = float(retry_after)
        except ValueError:
            from email.utils import parsedate_to_datetime
            try:
                delay = parsedate_to_datetime(retry_after).timestamp() - time.time()
            except (ValueError, TypeError):
                delay = 0
        if delay > 60:
            raise AcquisitionError('rate_limited', 'Retry-After exceeds in-process wait; checkpoint and resume later') from error
        if delay > 0:
            return delay
    return min(30, 2**attempt + random.random())


def guarded_download(url: str, destination: str | Path, budget: Budget, *, max_bytes: int,
                     expected_sha256: str | None = None, expected_size: int | None = None,
                     method='GET', data: bytes | None = None, headers: dict | None = None,
                     allowed_hosts=None, max_retries=5, max_transfer_bytes: int | None = None,
                     magic=None, validator=None, timeout=45, open_url=None) -> dict:
    """Stream one bounded artifact and retain verified-identity partials on failure.

    Resuming requires a strong ETag and exact 206/Content-Range. A weak or absent
    validator leaves a preserved, explicitly blocked partial. Local fingerprints
    never claim independent publisher verification. Existing finals require their
    sidecar fingerprint (or an externally pinned expected digest) for reuse.
    """
    max_bytes = _integer(max_bytes, 'artifact transfer bound')
    if max_bytes <= 0 or not 0 <= max_retries <= 5:
        raise ValueError('Positive transfer cap and at most five transient retries required')
    if expected_size is not None and expected_size > max_bytes:
        raise AcquisitionError('transfer_limit', 'Expected object cannot fit per-artifact limit')
    max_transfer_bytes = max_transfer_bytes if max_transfer_bytes is not None else max_bytes * 2
    _integer(max_transfer_bytes, 'per-source transfer bound')
    destination = budget.safe_path(destination)
    partial = budget.safe_path(destination.with_name(destination.name + '.part'))
    sidecar = budget.safe_path(destination.with_name(destination.name + '.part.json'))
    manifest_path = budget.safe_path(destination.with_name(destination.name + '.source.json'))
    source = redact_url(url)
    if destination.exists():
        previous = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
        expected = expected_sha256 or previous.get('sha256')
        if not expected:
            raise AcquisitionError('unverified_existing', 'Existing artifact lacks pinned digest or successful download manifest')
        if previous and previous.get('url') != source:
            raise AcquisitionError('source_changed', 'Existing artifact was acquired from a different source URL')
        digest = _validate_file(destination, expected_size or previous.get('size_bytes'), expected, magic, validator)
        return {**previous, 'url': source, 'sha256': digest, 'size_bytes': destination.stat().st_size,
                'status': 'reused_validated', 'network_bytes_this_run': 0}
    state = json.loads(sidecar.read_text()) if sidecar.exists() else {'url': source}
    if partial.exists() and not sidecar.exists():
        raise AcquisitionError('partial_metadata_missing', 'Partial has no identity sidecar; preserve and inspect')
    if state and state.get('url') != source:
        raise AcquisitionError('source_changed', 'Partial belongs to a different source')
    if partial.exists() and partial.stat().st_size > max_bytes:
        raise AcquisitionError('transfer_limit', 'Preserved partial exceeds artifact limit')
    parsed = urlsplit(url)
    if parsed.scheme not in {'https', 'http'} or parsed.username or parsed.password:
        raise AcquisitionError('unsafe_url', 'Only credential-free HTTP(S) source URLs supported')
    allowed = set(allowed_hosts or [parsed.hostname])
    opener = open_url or urlopen
    transfer = 0
    source_transfer = int(state.get('received_bytes', 0))
    initial_partial_info = partial.stat() if partial.exists() else None
    initial_partial_size = initial_partial_info.st_size if initial_partial_info else 0
    initial_partial_accounted = getattr(initial_partial_info, 'st_blocks', 0) * 512
    # The partial is staging even when deliberately beside its final for atomic rename.
    with budget.reserve(max(0, max_bytes - initial_partial_accounted) + 128 * 1024 + DOWNLOAD_ALLOCATION_MARGIN,
                        max(0, max_bytes - initial_partial_accounted) + 128 * 1024 + DOWNLOAD_ALLOCATION_MARGIN,
                        label=f'download {parsed.hostname}') as reservation:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if initial_partial_size and initial_partial_size == state.get('object_size'):
            # An interruption can occur after all bytes arrive but before atomic
            # publication. Never send bytes=EOF, and never accept merely a full
            # length: reconfirm remote identity and revalidate pinned integrity.
            etag = state.get('etag')
            if not etag or etag.startswith('W/'):
                raise AcquisitionError('resume_identity_unavailable', 'Complete partial has no strong ETag; preserved')
            if not expected_sha256 and validator is None:
                raise AcquisitionError('resume_integrity_unavailable', 'Complete partial requires a pinned checksum or source validator before publication')
            probe_headers = {'User-Agent': 'HiddenViewFinder/0.2 bounded public-data acquisition',
                             'Accept-Encoding': 'identity', **(headers or {})}
            try:
                with opener(Request(url, headers=probe_headers, method='HEAD'), timeout=timeout) as response:
                    final_parts = urlsplit(response.geturl())
                    if (final_parts.hostname not in allowed or final_parts.scheme not in {'https', 'http'}
                            or (parsed.scheme == 'https' and final_parts.scheme != 'https')):
                        raise AcquisitionError('redirect_blocked', 'Undocumented complete-partial identity redirect')
                    if (response.status != 200 or response.headers.get('ETag') != etag
                            or response.headers.get('Content-Length') != str(initial_partial_size)):
                        raise AcquisitionError('source_changed', 'Complete-partial HEAD identity/size changed; preserve bytes')
            except (HTTPError, URLError, TimeoutError, ConnectionError, OSError) as exc:
                raise AcquisitionError('resume_identity_blocked', f'Cannot reconfirm complete partial identity: {type(exc).__name__}') from exc
            digest = _validate_file(partial, expected_size or state['object_size'], expected_sha256, magic, validator)
            result = {**state, 'status': 'acquired_validated', 'size_bytes': initial_partial_size,
                      'sha256': digest, 'network_bytes_this_run': 0, 'received_bytes': source_transfer,
                      'completed_unix': time.time(), 'recovered_complete_partial': True,
                      'resume_identity_check': 'HEAD strong ETag and Content-Length',
                      'integrity': 'pinned expected SHA-256' if expected_sha256 else 'source validator plus local SHA-256 fingerprint',
                      'checksum_scope': 'complete downloaded artifact'}
            atomic_json(manifest_path, result, budget)
            if destination.exists():
                raise AcquisitionError('publication_conflict', 'Final appeared while recovering complete partial; preserve both')
            os.replace(partial, destination)
            state.update(status='complete', partial_bytes=initial_partial_size)
            atomic_json(sidecar, state, budget)
            return result
        for attempt in range(max_retries + 1):
            offset = partial.stat().st_size if partial.exists() else 0
            request_headers = {'User-Agent': 'HiddenViewFinder/0.2 bounded public-data acquisition',
                               'Accept-Encoding': 'identity', **(headers or {})}
            etag = state.get('etag')
            if offset:
                if not etag or etag.startswith('W/'):
                    raise AcquisitionError('resume_identity_unavailable', 'Partial has no strong ETag; preserved without unsafe append')
                request_headers.update({'Range': f'bytes={offset}-', 'If-Range': etag})
            request = Request(url, data=data, headers=request_headers, method=method)
            try:
                with opener(request, timeout=timeout) as response:
                    final_url = response.geturl()
                    final_parts = urlsplit(final_url)
                    if final_parts.hostname not in allowed or final_parts.scheme not in {'http', 'https'}:
                        raise AcquisitionError('redirect_blocked', f'Undocumented redirect host: {final_parts.hostname}')
                    if parsed.scheme == 'https' and final_parts.scheme != 'https':
                        raise AcquisitionError('redirect_blocked', 'HTTPS source redirected to unencrypted HTTP')
                    status_code = response.status
                    if (offset and status_code != 206) or (not offset and status_code != 200):
                        raise AcquisitionError('range_protocol_error', f'HTTP {status_code}; expected {206 if offset else 200}')
                    remote_etag = response.headers.get('ETag')
                    if not offset and partial.exists() and state.get('etag') and remote_etag != state['etag']:
                        raise AcquisitionError('source_changed', 'Empty preallocated partial remote identity changed; preserved')
                    if response.headers.get('Content-Encoding', 'identity') != 'identity':
                        raise AcquisitionError('range_protocol_error', 'Unexpected transfer Content-Encoding')
                    content_length = response.headers.get('Content-Length')
                    try:
                        content_length = int(content_length) if content_length is not None else None
                    except ValueError as exc:
                        raise AcquisitionError('schema_error', 'Malformed Content-Length') from exc
                    if content_length is not None and (content_length < 0 or offset + content_length > max_bytes):
                        raise AcquisitionError('transfer_limit', 'Declared Content-Length exceeds artifact cap')
                    total = offset + content_length if content_length is not None else expected_size
                    if offset:
                        match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('Content-Range', ''))
                        if not match or remote_etag != etag:
                            raise AcquisitionError('source_changed', 'Resume ETag or Content-Range identity mismatch')
                        start, end, total = map(int, match.groups())
                        if start != offset or end != total - 1 or total > max_bytes or end < start:
                            raise AcquisitionError('range_protocol_error', 'Wrong resumed byte range or total')
                        if content_length is not None and content_length != end - start + 1:
                            raise AcquisitionError('range_protocol_error', 'Content-Length disagrees with resumed range')
                        if state.get('object_size') is not None and state['object_size'] != total:
                            raise AcquisitionError('source_changed', 'Remote object size changed during resume')
                    if expected_size is not None and total is not None and expected_size != total:
                        raise AcquisitionError('source_changed', 'Remote object no longer matches pinned size')
                    state = {'url': source, 'final_url': redact_url(final_url), 'etag': remote_etag,
                             'last_modified': response.headers.get('Last-Modified'), 'object_size': total,
                             'received_bytes': source_transfer, 'partial_bytes': offset,
                             'expected_sha256': expected_sha256, 'max_bytes': max_bytes,
                             'started_unix': state.get('started_unix', time.time()), 'status': 'downloading'}
                    atomic_json(sidecar, state, budget)
                    with partial.open('ab' if partial.exists() else 'xb') as output:
                        state['preallocation'] = preallocate_keep_size(output, total or max_bytes, partial, reservation)
                        atomic_json(sidecar, state, budget)
                        while True:
                            block = response.read(min(CHUNK, max_bytes - offset + 1))
                            if not block:
                                break
                            transfer += len(block)
                            source_transfer += len(block)
                            if source_transfer > max_transfer_bytes or offset + len(block) > max_bytes:
                                raise AcquisitionError('transfer_limit', 'Actual streamed bytes exceed artifact/source transfer cap')
                            if total is not None and offset + len(block) > total:
                                raise AcquisitionError('corrupt_content', 'Actual streamed bytes exceed declared object size')
                            reservation.check_write(len(block), partial)
                            output.write(block)
                            output.flush()
                            offset += len(block)
                        # Release only unused unwritten allocation beyond actual
                        # EOF; no received bytes or source copies are removed.
                        output.truncate(offset)
                        os.fsync(output.fileno())
                    if total is not None and offset != total:
                        raise AcquisitionError('corrupt_content', 'Truncated response: actual bytes differ from declared total')
                    digest = _validate_file(partial, expected_size or total, expected_sha256, magic, validator)
                    state['object_size'] = offset
                    result = {**state, 'status': 'acquired_validated', 'size_bytes': offset,
                              'sha256': digest, 'network_bytes_this_run': transfer,
                              'received_bytes': source_transfer, 'completed_unix': time.time(),
                              'integrity': 'pinned expected SHA-256' if expected_sha256 else 'local SHA-256 fingerprint; no independent publisher checksum',
                              'checksum_scope': 'complete downloaded artifact'}
                    atomic_json(manifest_path, result, budget)
                    # Publish on the same filesystem after validation. Existing final
                    # races are refused, never silently overwritten.
                    if destination.exists():
                        raise AcquisitionError('publication_conflict', 'Final appeared while downloading; preserve both')
                    os.replace(partial, destination)
                    state.update(status='complete', partial_bytes=offset, received_bytes=source_transfer)
                    atomic_json(sidecar, state, budget)
                    return result
            except HTTPError as exc:
                category = ('authentication_blocked' if exc.code in {401, 403} else
                            'missing_source' if exc.code in {404, 410} else
                            'rate_limited' if exc.code == 429 else 'network_error')
                state.update(status=category, partial_bytes=partial.stat().st_size if partial.exists() else 0,
                             received_bytes=source_transfer)
                atomic_json(sidecar, state, budget, checkpoint=True)
                if exc.code not in {408, 429, 500, 502, 503, 504} or attempt >= max_retries:
                    raise AcquisitionError(category, f'HTTP {exc.code} from {parsed.hostname}') from exc
                time.sleep(_retry_delay(exc, attempt))
            except (URLError, TimeoutError, ConnectionError, OSError) as exc:
                state.update(status='network_error', partial_bytes=partial.stat().st_size if partial.exists() else 0,
                             received_bytes=source_transfer)
                atomic_json(sidecar, state, budget, checkpoint=True)
                if isinstance(exc, OSError) and exc.errno in {errno.ENOSPC, errno.EDQUOT}:
                    raise ResourceBudgetError('storage_blocked: filesystem refused bounded write') from exc
                if attempt >= max_retries:
                    raise AcquisitionError('network_error', f'Bounded retries exhausted for {parsed.hostname}: {type(exc).__name__}') from exc
                time.sleep(_retry_delay(exc, attempt))
            except BaseException:
                state.update(status='interrupted_or_validation_blocked',
                             partial_bytes=partial.stat().st_size if partial.exists() else 0,
                             received_bytes=source_transfer)
                # If an atomic JSON crash left a .writing file, preserve it and
                # let the original exception through instead of masking it.
                try:
                    atomic_json(sidecar, state, budget, checkpoint=True)
                except (OSError, ResourceBudgetError):
                    pass
                raise
    raise AssertionError('unreachable')


def safe_cleanup(path: str | Path, manifestproof: dict, budget: Budget) -> dict:
    """Delete an owned disposable only after a validated successor exists.

    The ownership record must have been persisted before exclusive creation.
    The caller supplies the current artifact SHA-256 and a durable validated
    successor receipt. Retaining a raw recovery source alone never permits
    deletion. A durable deletion intent precedes unlink, and completion is
    recorded afterward. The shared writer lock excludes live pipeline workers.
    """
    target = budget.safe_path(path)
    if budget._reservation is None:
        with budget.reserve(128 * 1024, 128 * 1024, 'owned disposable cleanup'):
            return safe_cleanup(target, manifestproof, budget)
    ownership_path = budget.safe_path(manifestproof['ownership_record'])
    ownership = json.loads(ownership_path.read_text())
    if (ownership.get('schema') != 'citywide-owned-partial-v1'
            or ownership.get('task_owned') is not True
            or ownership.get('disposable') is not True
            or ownership.get('exclusive_creation') is not True
            or Path(ownership.get('path', '')).resolve() != target):
        raise AcquisitionError('cleanup_refused', 'No matching persisted ownership record')
    source = budget.safe_path(ownership['recovery_source'])
    if source == target or not source.is_file() or sha256(source) != ownership['recovery_source_sha256']:
        raise AcquisitionError('cleanup_refused', 'Recovery source is missing or changed; preserve the partial')
    if not target.is_file() or sha256(target) != manifestproof['artifact_sha256']:
        raise AcquisitionError('cleanup_refused', 'Disposable file identity changed; preserve it')
    if not manifestproof.get('validated_successor_receipt'):
        raise AcquisitionError('cleanup_refused', 'A retained raw source is insufficient; a validated successor receipt is required')
    receipt_path = budget.safe_path(manifestproof['validated_successor_receipt'])
    if not receipt_path.is_file() or receipt_path.stat().st_size > MAX_METADATA_BYTES:
        raise AcquisitionError('cleanup_refused', 'Validated successor receipt is missing or oversized')
    receipt = json.loads(receipt_path.read_text())
    if receipt.get('valid') is not True or not receipt.get('path') or not receipt.get('sha256'):
        raise AcquisitionError('cleanup_refused', 'Successor receipt lacks explicit successful validation and identity')
    successor = budget.safe_path(receipt['path'])
    if successor in {target, source} or not successor.is_file() or sha256(successor) != receipt['sha256']:
        raise AcquisitionError('cleanup_refused', 'Distinct validated successor is missing or changed; preserve the partial')
    event = {'path': str(target), 'sha256': manifestproof['artifact_sha256'],
             'size_bytes': target.stat().st_size, 'recovery_source': str(source),
             'recovery_source_sha256': ownership['recovery_source_sha256'],
             'validated_successor': str(successor), 'validated_successor_sha256': receipt['sha256'],
             'validated_successor_receipt': str(receipt_path),
             'reason': 'Distinct validated successor exists; retained source also verified',
             'status': 'deletion_intent', 'unix': time.time()}
    log_path = budget.safe_path(manifestproof.get('deletion_log', ownership_path.with_name(ownership_path.name + '.cleanup.json')))
    history = json.loads(log_path.read_text()) if log_path.exists() else {'schema': 'citywide-cleanup-v1', 'events': []}
    history['events'].append(event)
    atomic_json(log_path, history, budget)
    # Reconfirm immediately before unlink after the durable record is written.
    if sha256(target) != manifestproof['artifact_sha256']:
        raise AcquisitionError('cleanup_refused', 'Disposable changed before deletion')
    target.unlink()
    event['status'] = 'deleted'
    atomic_json(log_path, history, budget)
    return event


def safe_extract_zip(archive: str | Path, destination: str | Path, budget: Budget, *,
                     max_expanded_bytes: int, expected_expanded_bytes: int | None = None,
                     max_member_bytes: int | None = None) -> dict:
    """Inspect all members before writes, with bounded, recoverable extraction.

    Each partial has a persisted ownership record. Resume retains the owned
    partial under a unique accounted path after verifying its archive byte prefix.
    Retained raw sources alone never justify deleting interrupted outputs.
    Completed members are CRC/size checked and atomically published independently.
    """
    archive = budget.safe_path(archive)
    destination = budget.safe_path(destination)
    _integer(max_expanded_bytes, 'ZIP expansion bound')
    max_member_bytes = max_member_bytes or max_expanded_bytes
    archive_hash = sha256(archive)
    with ZipFile(archive) as zipped:
        members = zipped.infolist()
        paths: set[Path] = set()
        expanded = 0
        for member in members:
            relative = PurePosixPath(member.filename)
            mode = member.external_attr >> 16
            if ('\\' in member.filename or '\x00' in member.filename or relative.is_absolute()
                    or '..' in relative.parts or (relative.parts and ':' in relative.parts[0])
                    or (stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)))
                    or member.flag_bits & 1):
                raise AcquisitionError('unsafe_archive', 'ZIP contains unsafe path, link/device, or encrypted member')
            target = budget.safe_path(destination.joinpath(*relative.parts))
            if target in paths:
                raise AcquisitionError('unsafe_archive', 'ZIP has duplicate member paths')
            paths.add(target)
            if member.file_size > max_member_bytes:
                raise AcquisitionError('expansion_limit', 'ZIP member exceeds inspected expansion bound')
            expanded += member.file_size
        if expanded > max_expanded_bytes or (expected_expanded_bytes is not None and expanded != expected_expanded_bytes):
            raise AcquisitionError('expansion_limit', 'ZIP expansion differs from inspected bound/version')
        additional = sum(m.file_size for m in members if not destination.joinpath(*PurePosixPath(m.filename).parts).exists())
        with budget.reserve(additional + len(members) * 32768 + DOWNLOAD_ALLOCATION_MARGIN,
                            max((m.file_size for m in members), default=0) + len(members) * 32768 + DOWNLOAD_ALLOCATION_MARGIN,
                            label='ZIP extraction') as reservation:
            actual = 0
            output_records = []
            preserved_records = []
            with reservation.bounded_small_writes():
                for member in members:
                    target = budget.safe_path(destination.joinpath(*PurePosixPath(member.filename).parts))
                    if member.is_dir():
                        reservation.check_write(4096, target)
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    if target.exists():
                        import zlib
                        crc = 0
                        with target.open('rb') as source:
                            while chunk := source.read(CHUNK):
                                crc = zlib.crc32(chunk, crc)
                        if target.stat().st_size != member.file_size or crc != member.CRC:
                            raise AcquisitionError('corrupt_content', 'Existing extracted member differs from archive; preserved')
                    else:
                        temporary = target.with_name(target.name + '.part')
                        ownership_path = target.with_name(target.name + '.part.owner.json')
                        budget.safe_path(temporary)
                        budget.safe_path(ownership_path)
                        if temporary.exists():
                            if not ownership_path.exists():
                                raise AcquisitionError('partial_metadata_missing', 'Unowned extraction partial preserved')
                            owner = json.loads(ownership_path.read_text())
                            if (owner.get('archive_member') != member.filename
                                    or owner.get('recovery_source_sha256') != archive_hash
                                    or temporary.stat().st_size > member.file_size):
                                raise AcquisitionError('source_changed', 'Extraction partial does not belong to retained source')
                            with zipped.open(member) as source, temporary.open('rb') as partial_stream:
                                while block := partial_stream.read(CHUNK):
                                    if source.read(len(block)) != block:
                                        raise AcquisitionError('corrupt_content', 'Partial bytes differ from retained archive; preserve')
                            # Keep the interrupted member even after its successor
                            # validates. The reservation already includes the full
                            # new member while baseline accounting retains this one.
                            identity = os.urandom(16).hex()
                            retained = temporary.with_name(temporary.name + '.' + identity + '.partial')
                            retained_record = temporary.with_name(temporary.name + '.' + identity + '.preservation.json')
                            budget.safe_path(retained)
                            preserved = {'status': 'preservation_planned', 'original_path': str(temporary),
                                'preserved_path': str(retained), 'sha256': sha256(temporary),
                                'logical_bytes': temporary.stat().st_size,
                                'allocated_bytes': temporary.stat().st_blocks * 512,
                                'ownership_before_preservation': owner,
                                'recovery_source_sha256': archive_hash,
                                'deletion_performed': False}
                            atomic_json(retained_record, preserved, budget)
                            os.replace(temporary, retained)
                            preserved['status'] = 'preserved'
                            atomic_json(retained_record, preserved, budget)
                            preserved_records.append(preserved)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        atomic_json(ownership_path, {
                            'schema': 'citywide-owned-partial-v1', 'task_owned': True,
                            'disposable': True, 'exclusive_creation': True,
                            'path': str(temporary), 'recovery_source': str(archive),
                            'recovery_source_sha256': archive_hash, 'archive_member': member.filename,
                            'expected_size': member.file_size, 'created_unix': time.time()}, budget)
                        count = 0
                        with zipped.open(member) as source, temporary.open('xb') as output:
                            allocation = preallocate_keep_size(output, member.file_size, temporary, reservation)
                            while block := source.read(min(CHUNK, max_member_bytes - count + 1)):
                                if count + len(block) > member.file_size or count + len(block) > max_member_bytes or actual + len(block) > max_expanded_bytes:
                                    raise AcquisitionError('expansion_limit', 'Actual expanded bytes exceed declared/bounded size')
                                reservation.check_write(len(block), temporary)
                                output.write(block)
                                output.flush()
                                count += len(block)
                                actual += len(block)
                            os.fsync(output.fileno())
                        if count != member.file_size:
                            raise AcquisitionError('corrupt_content', 'Truncated expanded member')
                        os.replace(temporary, target)
                    output_records.append({'path': str(target.relative_to(destination)), 'size_bytes': member.file_size,
                                           'sha256': sha256(target), 'zip_crc32': f'{member.CRC:08x}',
                                           'write_allocation': 'Linux KEEP_SIZE exact member bound'})
    return {'archive_sha256': archive_hash, 'expanded_bytes': expanded,
            'written_bytes_this_run': actual, 'members': output_records,
            'cleanup': [], 'preserved_interrupted_members': preserved_records}


def run_bounded(command: list[str], budget: Budget, *, peak_bytes: int,
                temporary_bytes: int, cwd: Path, env: dict, timeout: float,
                log_path: Path, maximum_log_bytes=2 * 1024**2,
                lag_margin_bytes=64 * 1024**2, poll_seconds=0.2) -> dict:
    """Monitor an already conservatively bounded child; no unbounded conversion.

    A reservation and lag margin do not make polling an OS quota. Only use for
    stages with independently bounded tile/batch/output sizes. Child stdout uses
    a nonblocking pipe and a capped, accounted log, never an unbounded temp file.
    """
    log_path = budget.safe_path(log_path)
    if peak_bytes <= 0 or lag_margin_bytes <= 0:
        raise ValueError('Child output bound and monitoring-lag margin required')
    budget.safe_path(cwd)
    with budget.reserve(peak_bytes + lag_margin_bytes + maximum_log_bytes,
                        temporary_bytes + maximum_log_bytes, 'bounded child') as reservation:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open('xb') as log:
            assert budget._lock_fd is not None and budget._reservation_id is not None
            child_env = {**env, 'HVF_BUDGET_RESERVATION_FD': str(budget._lock_fd),
                         'HVF_BUDGET_PARENT_PID': str(os.getpid()),
                         'HVF_BUDGET_RESERVATION_ID': budget._reservation_id}
            process = subprocess.Popen(command, cwd=cwd, env=child_env, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, start_new_session=True,
                                       pass_fds=(budget._lock_fd,))
            assert process.stdout is not None
            os.set_blocking(process.stdout.fileno(), False)
            started = time.monotonic()
            logged = 0
            try:
                while True:
                    block = process.stdout.read(65536) or b''
                    if block and logged < maximum_log_bytes:
                        block = block[:maximum_log_bytes - logged]
                        reservation.check_write(len(block), log_path)
                        log.write(block)
                        log.flush()
                        logged += len(block)
                    observed = reservation.observe()
                    if max(0, observed['accounted_bytes'] - reservation.baseline) > peak_bytes + maximum_log_bytes:
                        raise ResourceBudgetError('storage_blocked: child exceeded independently declared output bound; stopping within lag margin')
                    budget.check(lag_margin_bytes)
                    if time.monotonic() - started > timeout:
                        raise AcquisitionError('resource_timeout', 'Bounded child exceeded its stage timeout')
                    if available_memory_bytes() < 128 * 1024**2:
                        raise ResourceBudgetError('memory_blocked: child exhausted safe RAM headroom')
                    if process.poll() is not None and not block:
                        break
                    time.sleep(poll_seconds)
            except BaseException:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                raise
            finally:
                process.stdout.close()
        if process.returncode:
            raise AcquisitionError('child_failed', f'Exit {process.returncode}; inspect bounded log')
        return {'returncode': process.returncode, 'log_bytes': logged,
                'elapsed_seconds': time.monotonic() - started, 'storage': budget.snapshot()}
