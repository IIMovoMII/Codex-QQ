"""Durable UI receipts. Request payloads (including secrets) are never stored."""
from __future__ import annotations
import hashlib
import json
import sqlite3
import time
from pathlib import Path
from contextlib import contextmanager

class OperationConflict(ValueError):
    pass

class OperationJournal:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=2)
        db.execute("PRAGMA busy_timeout=2000")
        db.execute("CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, started REAL NOT NULL, response TEXT)")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def fingerprint(command: str, params: dict) -> str:
        return hashlib.sha256(json.dumps([command, params], sort_keys=True, ensure_ascii=False,
                                         separators=(",", ":")).encode()).hexdigest()

    def begin(self, operation: str, command: str, params: dict) -> dict | None:
        fingerprint = self.fingerprint(command, params)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT fingerprint,response FROM operations WHERE id=?", (operation,)).fetchone()
            if row:
                if row[0] != fingerprint:
                    raise OperationConflict("operation_id_reused")
                return json.loads(row[1]) if row[1] else {
                    "requestId": operation, "operationId": operation, "status": "unknown",
                    "preview": False, "errorCode": "operation_interrupted_check_result"}
            # Never evict an unresolved mutation and then replay it on retry.
            db.execute("DELETE FROM operations WHERE started<? AND response IS NOT NULL "
                       "AND json_extract(response,'$.status')='confirmed'", (time.time() - 30*86400,))
            if db.execute("SELECT count(*) FROM operations").fetchone()[0] >= 4096:
                raise OperationConflict("operation_journal_full")
            db.execute("INSERT INTO operations VALUES (?,?,?,NULL)", (operation, fingerprint, time.time()))
        return None

    def finish(self, operation: str, response: dict):
        with self._connect() as db:
            db.execute("UPDATE operations SET response=? WHERE id=?",
                       (json.dumps(response, ensure_ascii=False, separators=(",", ":")), operation))

    def get(self, operation: str) -> dict:
        with self._connect() as db:
            row = db.execute("SELECT response FROM operations WHERE id=?", (operation,)).fetchone()
        if row and row[0]:
            return json.loads(row[0])
        return {"operationId": operation, "status": "unknown", "preview": False,
                "errorCode": "operation_interrupted_check_result" if row else "operation_not_found"}

    def has_unresolved(self) -> bool:
        with self._connect() as db:
            return db.execute("SELECT 1 FROM operations WHERE response IS NULL LIMIT 1").fetchone() is not None
