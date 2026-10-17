"""Bridge-owned, transactional state. Never writes Codex history databases."""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class ControlStore:
    VERSION = 2

    def __init__(self, path: Path, *, legacy_routes: Path | None = None) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, self.VERSION):
                raise ValueError("Unsupported control-state version")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS selections(scope TEXT PRIMARY KEY, thread_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS refs(
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL, kind TEXT NOT NULL,
                    data TEXT NOT NULL, created REAL NOT NULL, expires REAL);
                CREATE INDEX IF NOT EXISTS refs_scope ON refs(scope, created);
                CREATE TABLE IF NOT EXISTS receipts(
                    scope TEXT NOT NULL, message_id TEXT NOT NULL, sent_at REAL NOT NULL,
                    state TEXT NOT NULL, reply TEXT NOT NULL DEFAULT '', updated REAL NOT NULL,
                    PRIMARY KEY(scope, message_id));
                CREATE TABLE IF NOT EXISTS connection(
                    key TEXT PRIMARY KEY, identity TEXT NOT NULL, ready_since REAL NOT NULL,
                    online INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS legacy_selections(scope TEXT PRIMARY KEY, thread_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS operations(
                    scope TEXT NOT NULL, key TEXT NOT NULL, state TEXT NOT NULL,
                    reply TEXT NOT NULL DEFAULT '', updated REAL NOT NULL,
                    PRIMARY KEY(scope,key));
            """)
            db.execute(f"PRAGMA user_version={self.VERSION}")
            if legacy_routes is not None and not db.execute(
                "SELECT 1 FROM meta WHERE key='legacy_import'"
            ).fetchone():
                if legacy_routes.exists():
                    routes = json.loads(legacy_routes.read_text(encoding="utf-8"))
                    if not isinstance(routes, dict) or not all(
                        isinstance(k, str) and isinstance(v, str) for k, v in routes.items()
                    ):
                        raise ValueError("Invalid legacy routes; preserved without migration")
                    db.executemany("INSERT OR IGNORE INTO legacy_selections VALUES(?,?)", routes.items())
                db.execute("INSERT INTO meta VALUES('legacy_import','1')")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=2.0)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def selection(self, scope: str, legacy_scope: str = "") -> str | None:
        with self.connect() as db:
            row = db.execute("SELECT thread_id FROM selections WHERE scope=?", (scope,)).fetchone()
            if row:
                return row[0]
            old = db.execute("SELECT thread_id FROM legacy_selections WHERE scope=?", (legacy_scope,)).fetchone()
            if old:
                db.execute("INSERT OR IGNORE INTO selections VALUES(?,?)", (scope, old[0]))
                db.execute("DELETE FROM legacy_selections WHERE scope=?", (legacy_scope,))
                return old[0]
        return None

    def select(self, scope: str, thread_id: str | None) -> None:
        with self.connect() as db:
            if thread_id is None:
                db.execute("DELETE FROM selections WHERE scope=?", (scope,))
            else:
                db.execute("INSERT INTO selections VALUES(?,?) ON CONFLICT(scope) DO UPDATE SET thread_id=excluded.thread_id", (scope, thread_id))

    def reference(self, scope: str, kind: str, data: Any, *, ttl: float | None = None) -> str:
        token = secrets.token_urlsafe(9)
        now = time.time()
        with self.connect() as db:
            db.execute("INSERT INTO refs VALUES(?,?,?,?,?,?)", (
                token, scope, kind, json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                now, now + ttl if ttl is not None else None,
            ))
        return token

    def get_reference(self, scope: str, token: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM refs WHERE id=? AND scope=?", (token, scope)).fetchone()
        if row is None or row["expires"] is not None and row["expires"] < time.time():
            return None
        return {"kind": row["kind"], "data": json.loads(row["data"])}

    def begin(self, scope: str, message_id: str, sent_at: float) -> str | None:
        """Claim a platform event once; an interrupted dispatch remains unknown."""
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state,reply FROM receipts WHERE scope=? AND message_id=?", (scope, message_id)).fetchone()
            if row:
                return row["reply"] or "这条指令已接收，但结果尚未确认；不会重复执行。"
            db.execute("INSERT INTO receipts VALUES(?,?,?,'dispatching','',?)", (scope, message_id, sent_at, now))
        return None

    def finish(self, scope: str, message_id: str, state: str, reply: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE receipts SET state=?,reply=?,updated=? WHERE scope=? AND message_id=?", (state, reply[:2200], time.time(), scope, message_id))

    def ready(self, identity: str, online: bool, *, now: float | None = None) -> float:
        """Global backend identity, shared by ACP processes instead of per-prompt cutoffs."""
        now = time.time() if now is None else now
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM connection WHERE key='desktop'").fetchone()
            if old is not None and old["identity"] == identity and not old["online"] and online:
                since = time.time()
            elif old is None or old["identity"] != identity:
                since = now
            else:
                since = old["ready_since"]
            db.execute("INSERT INTO connection VALUES('desktop',?,?,?) ON CONFLICT(key) DO UPDATE SET identity=excluded.identity,ready_since=excluded.ready_since,online=excluded.online", (identity, since, int(online)))
        return since

    def offline(self) -> None:
        with self.connect() as db:
            db.execute("UPDATE connection SET online=0 WHERE key='desktop'")

    def prune(self) -> None:
        cutoff = time.time() - 30 * 86400
        with self.connect() as db:
            db.execute("DELETE FROM refs WHERE expires IS NOT NULL AND expires<?", (time.time(),))
            db.execute("DELETE FROM receipts WHERE updated<? AND state!='dispatching'", (cutoff,))
            db.execute("DELETE FROM operations WHERE updated<?", (cutoff,))

    def claim_operation(self, scope: str, key: str) -> dict | None:
        """One-shot actions across cards/processes; unknown never means retry."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT state,reply FROM operations WHERE scope=? AND key=?', (scope, key)).fetchone()
            if row:
                return {'state': row['state'] if row['state'] != 'dispatching' else 'unknown',
                        'reply': row['reply'] or '这项操作已提交但结果未知，不会重复执行。请重新查询状态。'}
            db.execute("INSERT INTO operations VALUES(?,?,'dispatching','',?)", (scope, key, time.time()))
        return None

    def operation(self, scope: str, key: str) -> dict | None:
        with self.connect() as db:
            row = db.execute('SELECT state,reply FROM operations WHERE scope=? AND key=?', (scope, key)).fetchone()
        if row is None:
            return None
        return {'state': row['state'] if row['state'] != 'dispatching' else 'unknown',
                'reply': row['reply'] or '这项操作已提交但结果未知，不会重复执行。请重新查询状态。'}

    def finish_operation(self, scope: str, key: str, state: str, reply: str) -> None:
        with self.connect() as db:
            db.execute('UPDATE operations SET state=?,reply=?,updated=? WHERE scope=? AND key=?',
                       (state, reply[:2200], time.time(), scope, key))
