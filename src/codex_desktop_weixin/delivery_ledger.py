"""Durable delivery facts, independent of rotating diagnostic journals."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
import time

MAX_LEDGER_ROWS = 5000
LEDGER_RETENTION_SECONDS = 30 * 86400
LEDGER_PRUNE_INTERVAL = 256


class DeliveryLedger:
    def __init__(self, path: Path, *, pending: Path) -> None:
        self.path = path
        self.pending = pending
        self._writes = 0
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1):
                raise ValueError('unsupported-delivery-ledger')
            db.execute('CREATE TABLE IF NOT EXISTS notices ('
                       'item TEXT PRIMARY KEY, shadowed INTEGER NOT NULL DEFAULT 0, '
                       'delivered INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL)')
            db.execute('PRAGMA user_version=1')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=2)
        try:
            db.execute('PRAGMA synchronous=FULL')
            with db:
                yield db
        finally:
            db.close()

    def contains(self, item: str, *, delivered: bool = False) -> bool:
        column = 'delivered' if delivered else 'shadowed'
        with self.connect() as db:
            row = db.execute(f'SELECT {column} FROM notices WHERE item=?', (item,)).fetchone()
        return bool(row and row[0])

    def record(self, item: str, *, delivered: bool = False) -> None:
        column = 'delivered' if delivered else 'shadowed'
        with self.connect() as db:
            db.execute(f'INSERT INTO notices(item,{column},updated) VALUES(?,1,?) '
                       f'ON CONFLICT(item) DO UPDATE SET {column}=1,updated=excluded.updated',
                       (item, time.time()))
        self._writes += 1
        if self._writes >= LEDGER_PRUNE_INTERVAL:
            self.prune(self.pending)
            self._writes = 0

    def prune(self, pending: Path) -> None:
        # Unresolved pending entries keep their facts regardless of age/count.
        cutoff = time.time() - LEDGER_RETENTION_SECONDS
        with self.connect() as db:
            rows = db.execute('SELECT item,updated FROM notices ORDER BY updated DESC').fetchall()
            expired = [(item,) for index, (item, updated) in enumerate(rows)
                       if (updated < cutoff or index >= MAX_LEDGER_ROWS)
                       and not (pending / item).exists()]
            db.executemany('DELETE FROM notices WHERE item=?', expired)

    def import_facts(self, shadowed: set[str], delivered: set[str]) -> None:
        with self.connect() as db:
            for column, items in (('shadowed', shadowed), ('delivered', delivered)):
                db.executemany(f'INSERT INTO notices(item,{column},updated) VALUES(?,1,?) '
                               f'ON CONFLICT(item) DO UPDATE SET {column}=1',
                               ((item, time.time()) for item in items))
