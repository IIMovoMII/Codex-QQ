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
        legacy_value = None
        if legacy_routes is not None and legacy_routes.exists():
            try:
                legacy_value = json.loads(legacy_routes.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
                raise ValueError("Invalid legacy routes; preserved without migration") from error
            if not isinstance(legacy_value, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in legacy_value.items()
            ):
                raise ValueError("Invalid legacy routes; preserved without migration")
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
                CREATE TABLE IF NOT EXISTS bridge_sessions(
                    scope TEXT PRIMARY KEY, started REAL NOT NULL);
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
                    db.executemany("INSERT OR IGNORE INTO legacy_selections VALUES(?,?)", legacy_value.items())
                db.execute("INSERT INTO meta VALUES('legacy_import','1')")

    def begin_bridge_session(self, scope: str, *, now: float | None = None) -> float:
        """Record the QQ gateway's ready boundary for this bridge scope.

        The gateway may start its ACP child after the triggering message has
        already arrived. Its READY time, rather than Python process startup,
        determines which events predate the current QQ connection.
        """
        started = time.time() if now is None else now
        with self.connect() as db:
            db.execute("INSERT INTO bridge_sessions(scope,started) VALUES(?,?) "
                       "ON CONFLICT(scope) DO UPDATE SET started=excluded.started",
                       (scope, started))
        return started

    @staticmethod
    def _connection_key(scope: str | None) -> str:
        return 'desktop' if not scope else 'desktop|' + scope

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
        encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        now = time.time()
        with self.connect() as db:
            # Reuse permanent cards with identical targets (notably thread
            # receipts). Refreshing the same thread no longer grows refs forever.
            if ttl is None:
                old = db.execute(
                    "SELECT id FROM refs WHERE scope=? AND kind=? AND data=? AND expires IS NULL ORDER BY created DESC LIMIT 1",
                    (scope, kind, encoded),
                ).fetchone()
                if old is not None:
                    return str(old[0])
            token = secrets.token_urlsafe(9)
            db.execute("INSERT INTO refs VALUES(?,?,?,?,?,?)", (
                token, scope, kind, encoded,
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

    def ready(self, identity: str, online: bool, *, now: float | None = None,
              scope: str | None = None) -> float:
        """Record one Desktop generation for this QQ session."""
        now = time.time() if now is None else now
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            key = self._connection_key(scope)
            old = db.execute("SELECT * FROM connection WHERE key=?", (key,)).fetchone()
            bridge_row = db.execute("SELECT started FROM bridge_sessions WHERE scope=?",
                                    (scope,)).fetchone() if scope else None
            bridge_cutoff = float(bridge_row[0]) if bridge_row else 0.0
            if old is not None and not old["online"] and online:
                # offline() records the last confirmed disconnect. Using the
                # current message handling time here would always invalidate
                # the first fresh message after recovery.
                since = max(bridge_cutoff, old["ready_since"])
                if old['identity'] and old['identity'] != identity:
                    since = max(since, now)
            elif old is None or old["identity"] != identity:
                since = max(now, bridge_cutoff)
            else:
                since = max(old["ready_since"], bridge_cutoff)
            db.execute("INSERT INTO connection VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET identity=excluded.identity,ready_since=excluded.ready_since,online=excluded.online", (key, identity, since, int(online)))
        return since

    def offline(self, scope: str | None = None) -> None:
        now = time.time()
        with self.connect() as db:
            db.execute("INSERT INTO connection(key,identity,ready_since,online) VALUES(?, '', ?, 0) "
                       "ON CONFLICT(key) DO UPDATE SET online=0,ready_since=MAX(connection.ready_since,excluded.ready_since)",
                       (self._connection_key(scope), now))

    def prune(self) -> None:
        cutoff = time.time() - 30 * 86400
        with self.connect() as db:
            # Permanent thread cards are reusable and stable. Other old
            # references become finite-lived, including cards created by
            # earlier versions with expires=NULL.
            db.execute("UPDATE refs SET expires=created+? WHERE expires IS NULL AND kind!='thread'",
                       (30 * 86400,))
            db.execute("UPDATE refs SET expires=created+? WHERE expires IS NULL AND kind='thread' AND data LIKE '%\"connection\"%'",
                       (30 * 86400,))
            db.execute("DELETE FROM refs WHERE expires IS NOT NULL AND expires<?", (time.time(),))
            db.execute("UPDATE receipts SET state='unknown',reply='处理结果未知，不会自动重发。请在电脑核对。',updated=? WHERE state='dispatching' AND updated<?",
                       (time.time(), cutoff))
            db.execute("DELETE FROM receipts WHERE updated<? AND state!='dispatching'", (cutoff,))
            db.execute("UPDATE operations SET state='unknown',reply='操作结果未知，不会自动重发。请在电脑核对。',updated=? WHERE state='dispatching' AND updated<?",
                       (time.time(), cutoff))
            db.execute("DELETE FROM operations WHERE updated<?", (cutoff,))
            db.execute("DELETE FROM bridge_sessions WHERE started<?", (cutoff,))

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

    def release_operation(self, scope: str, key: str) -> None:
        """Release only a pre-dispatch claim that is known safe to retry."""
        with self.connect() as db:
            db.execute("DELETE FROM operations WHERE scope=? AND key=? AND state='dispatching'",
                       (scope, key))
