"""Durable, conservative daily cost reservations; never an invoice estimate.

The ledger consumes the maximum configured billable cost before a POST. No
refund is made after a timeout, refusal, invalid response, or process restart.
The shared filesystem writer reservation serializes updates across processes.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path
import sqlite3
import uuid

from seoul_visibility.acquisition_safety import Budget, Reservation


class ProviderUnavailable(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def micro_usd(value) -> int:
    try:
        parsed = Decimal(str(value))
        if not parsed.is_finite() or parsed < 0 or parsed > 1000:
            raise ValueError
        return int((parsed * 1_000_000).to_integral_value(rounding=ROUND_CEILING))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError('Cost must be a finite nonnegative USD amount <= 1000') from None


class SpendingLedger:
    """At most 100 calls/day, 366 daily aggregate rows, 2 MiB SQLite file.

This is an application authorization limit, not a provider billing guarantee.
The caller must use verified conservative prices for the exact request shape.
"""
    def __init__(self, root: Path, budget: Budget, daily_usd=0, daily_calls=20,
                 daily_images=5, clock=None):
        self.budget = budget
        self.root = budget.safe_path(root)
        self.path = budget.safe_path(self.root / 'spending.sqlite')
        self.limit = micro_usd(daily_usd)
        if not isinstance(daily_calls, int) or not 1 <= daily_calls <= 100:
            raise ValueError('daily_calls must be 1..100')
        if not isinstance(daily_images, int) or not 0 <= daily_images <= daily_calls:
            raise ValueError('daily_images must be 0..daily_calls')
        self.daily_calls, self.daily_images = daily_calls, daily_images
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @contextmanager
    def _writer(self, reservation: Reservation | None):
        if reservation is not None:
            if reservation.budget is not self.budget:
                raise ValueError('Wrong storage reservation')
            yield reservation
        else:
            with self.budget.reserve(4_194_304, 4_194_304, 'prototype cost ledger') as current:
                yield current

    def reserve(self, cost_usd, kind: str, *, reservation=None) -> dict:
        cost = micro_usd(cost_usd)
        if not self.limit or not cost or kind not in ('text', 'image'):
            raise ProviderUnavailable('spending_not_authorized')
        with self._writer(reservation) as current:
            # Include page growth, rollback journal and directory allocation.
            current.check_write(4_194_304, self.path)
            self.root.mkdir(parents=True, exist_ok=True)
            self.budget.safe_path(self.path)
            for suffix in ('-journal', '-wal', '-shm'):
                self.budget.safe_path(Path(str(self.path)+suffix))
            existed = self.path.exists()
            if existed and self.path.stat().st_size > 2_097_152:
                raise ProviderUnavailable('spending_ledger_invalid')
            with sqlite3.connect(self.path, timeout=1) as db:
                if existed and db.execute('PRAGMA application_id').fetchone()[0] != 0x48564641:
                    raise ProviderUnavailable('spending_ledger_unowned')
                db.execute('PRAGMA application_id=1213613633')
                db.execute('PRAGMA journal_mode=DELETE')
                db.execute('PRAGMA synchronous=FULL')
                db.execute('PRAGMA page_size=4096')
                db.execute('PRAGMA max_page_count=512')
                db.execute('CREATE TABLE IF NOT EXISTS daily(day TEXT PRIMARY KEY, used INTEGER NOT NULL, calls INTEGER NOT NULL, images INTEGER NOT NULL)')
                db.execute('BEGIN IMMEDIATE')
                day = self.clock().astimezone(timezone.utc).date().isoformat()
                row = db.execute('SELECT used,calls,images FROM daily WHERE day=?', (day,)).fetchone() or (0, 0, 0)
                if row[0] + cost > self.limit:
                    raise ProviderUnavailable('daily_spending_exhausted')
                if row[1] >= self.daily_calls or (kind == 'image' and row[2] >= self.daily_images):
                    raise ProviderUnavailable('daily_request_cap_exhausted')
                next_row = (row[0]+cost, row[1]+1, row[2]+int(kind == 'image'))
                db.execute('INSERT OR REPLACE INTO daily VALUES(?,?,?,?)', (day, *next_row))
                # Only disposable aggregate authorization history is compacted.
                db.execute('DELETE FROM daily WHERE day NOT IN (SELECT day FROM daily ORDER BY day DESC LIMIT 366)')
                db.commit()
            current.observe()
        return {'id': uuid.uuid4().hex, 'day_utc': day, 'reserved_micro_usd': cost,
                'day_reserved_micro_usd': next_row[0], 'accounting': 'conservative_maximum_no_refunds'}

    def status(self) -> dict:
        day = self.clock().astimezone(timezone.utc).date().isoformat()
        row = (0, 0, 0)
        if self.path.exists():
            self.budget.safe_path(self.path)
            if self.path.stat().st_size > 2_097_152:
                raise ProviderUnavailable('spending_ledger_invalid')
            with sqlite3.connect(f'file:{self.path}?mode=ro', uri=True, timeout=1) as db:
                row = db.execute('SELECT used,calls,images FROM daily WHERE day=?', (day,)).fetchone() or row
        return {'day_utc': day, 'daily_limit_micro_usd': self.limit,
                'reserved_micro_usd': row[0], 'calls': row[1], 'images': row[2],
                'accounting': 'maximum_reservations_not_actual_invoices'}
