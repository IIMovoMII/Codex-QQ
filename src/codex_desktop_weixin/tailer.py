from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from models import Signal, SignalKind
from rollout import parse_rollout_line
from runtime_io import append_json_line, atomic_write_json


_SESSION_ID_PATTERN = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?=\.jsonl$)", re.IGNORECASE)
_COLD_CURSOR_RETENTION = 30 * 86400
_MAX_COLD_CURSORS = 20000


def canonical_path_key(path: str | Path) -> str:
    """Return one stable key for normal and Windows device-prefixed paths."""
    value = os.fspath(path)
    if os.name == "nt":
        value = value.replace("/", "\\")
        folded = value.casefold()
        if folded.startswith("\\\\?\\unc\\"):
            value = "\\\\" + value[8:]
        elif folded.startswith("\\\\?\\"):
            value = value[4:]
        # Windows may supply an 8.3 alias (RUNNER~1) in TEMP/hooks while
        # Path.resolve() yields the long spelling. Both must share a cursor,
        # just like device-prefixed paths; otherwise a lookup can miss it.
        return os.path.normcase(os.path.realpath(os.path.abspath(os.path.normpath(value))))
    return str(Path(value).resolve())


def session_id_from_path(path: Path) -> str:
    match = _SESSION_ID_PATTERN.search(path.name)
    return match.group(1) if match else path.stem


@dataclass
class Cursor:
    offset: int
    identity: int
    session_id: str
    turn_id: str | None = None
    # The current offset is inside a JSONL record larger than the per-tick
    # read budget.  Once set, consume chunks until the next newline so one
    # pathological tool payload cannot wedge the transcript forever.
    skipping_oversized_line: bool = False
    context_incomplete: bool = False
    needs_context: bool = False


class IncrementalTailer:
    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path
        self.cursors: dict[str, Cursor] = {}
        # Keys whose cold metadata was unreadable are rebuilt from a bounded
        # transcript lookback rather than replayed from byte zero.
        self._cold_recovery_keys: set[str] = set()
        self.checkpoint: dict | None = None
        self.cold_path = state_path.with_suffix('.sqlite')
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (json.JSONDecodeError, OSError) as error:
            raise ValueError('invalid-monitor-checkpoint') from error
        if not isinstance(raw, dict):
            raise ValueError('invalid-monitor-checkpoint')
        if 'version' in raw:
            if raw.get('version') != 2 or not isinstance(raw.get('cursors'), dict) or not isinstance(raw.get('reducer'), dict):
                raise ValueError('unsupported-monitor-checkpoint')
            self.checkpoint = raw['reducer']
            raw = raw['cursors']
        for path, value in raw.items():
            if not isinstance(value, dict):
                raise ValueError('invalid-monitor-cursor')
            try:
                candidate = Cursor(
                    offset=max(0, int(value.get("offset", 0))),
                    identity=int(value.get("identity", 0)),
                    session_id=str(value.get("session_id") or session_id_from_path(Path(path))),
                    turn_id=value.get('turn_id') if isinstance(value.get('turn_id'), str) else None,
                    skipping_oversized_line=bool(value.get('skipping_oversized_line', False)),
                    context_incomplete=bool(value.get('context_incomplete', False)),
                    needs_context=bool(value.get('needs_context', self.checkpoint is None)),
                )
                key = canonical_path_key(path)
                existing = self.cursors.get(key)
                # A Codex hook can report ``\\?\C:\...`` while the directory
                # watcher reports ``C:\...``.  When upgrading an old state file,
                # keep the furthest cursor so the alias can never replay history.
                if existing is None or candidate.offset > existing.offset:
                    self.cursors[key] = candidate
            except (TypeError, ValueError) as error:
                raise ValueError('invalid-monitor-cursor') from error

    def _save(self) -> None:
        cursors = {path: asdict(cursor) for path, cursor in self.cursors.items()}
        value = cursors if self.checkpoint is None else {'version': 2, 'cursors': cursors, 'reducer': self.checkpoint}
        atomic_write_json(self.state_path, value)

    def compact(self, active_sessions: set[str], *, max_hot: int = 2000) -> None:
        candidates = [(key, cursor) for key, cursor in self.cursors.items()
                      if cursor.session_id not in active_sessions]
        evict = candidates[:max(0, len(self.cursors) - max_hot)]
        if not evict:
            return
        self.cold_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.cold_path)
        try:
            with db:
                db.execute('CREATE TABLE IF NOT EXISTS cursors (path TEXT PRIMARY KEY, value TEXT NOT NULL, updated REAL NOT NULL DEFAULT 0)')
                columns = {row[1] for row in db.execute('PRAGMA table_info(cursors)')}
                if 'updated' not in columns:
                    db.execute('ALTER TABLE cursors ADD COLUMN updated REAL NOT NULL DEFAULT 0')
                now = time.time()
                db.executemany('INSERT OR REPLACE INTO cursors(path,value,updated) VALUES(?,?,?)',
                               [(key, json.dumps(asdict(cursor)), now) for key, cursor in evict])
                self._prune_cold(db, now)
        finally:
            db.close()
        for key, _ in evict:
            self.cursors.pop(key, None)
        self._save()

    @staticmethod
    def _prune_cold(db: sqlite3.Connection, now: float) -> None:
        rows = db.execute('SELECT path,updated FROM cursors').fetchall()
        cutoff = now - _COLD_CURSOR_RETENTION
        missing = [(path,) for path, updated in rows
                   if updated < cutoff and not Path(path).exists()]
        if missing:
            db.executemany('DELETE FROM cursors WHERE path=?', missing)
        remaining = len(rows) - len(missing)
        if remaining <= _MAX_COLD_CURSORS:
            return
        # Never evict a still-existing path solely to meet a count limit:
        # replaying a reused transcript name is less safe than retaining a
        # small amount of metadata. Missing paths are safe after the window.
        candidates = [(updated, path) for path, updated in rows
                      if (path,) not in missing and updated < cutoff and not Path(path).exists()]
        for _updated, path in sorted(candidates)[:max(0, remaining - _MAX_COLD_CURSORS)]:
            db.execute('DELETE FROM cursors WHERE path=?', (path,))

    def _log_oversized_record(self) -> None:
        try:
            append_json_line(self.state_path.parent.parent / 'logs' / 'tailer-errors.jsonl',
                             {'event': 'oversized-jsonl-record-skipped', 'at': time.time()})
        except OSError:
            pass

    def _log_cold_error(self, event: str, error: BaseException | None = None) -> None:
        """Record cold-index recovery without exposing paths or database text."""
        value = {'event': event, 'at': time.time()}
        if error is not None:
            value['error_type'] = type(error).__name__
        try:
            append_json_line(self.state_path.parent.parent / 'logs' / 'tailer-errors.jsonl', value)
        except OSError:
            pass

    def _quarantine_cold_index(self, error: BaseException) -> None:
        """Move an unreadable cold index aside so one bad file cannot wedge scans."""
        if not self.cold_path.exists():
            return
        backup = self.cold_path.with_name(
            f'{self.cold_path.name}.corrupt-{time.time_ns()}'
        )
        try:
            os.replace(self.cold_path, backup)
        except OSError as move_error:
            self._log_cold_error('cold-index-quarantine-failed', move_error)
            return
        self._log_cold_error('cold-index-quarantined', error)

    def _discard_cold_row(self, key: str) -> None:
        """Remove one malformed row, preserving all other cold cursors."""
        db: sqlite3.Connection | None = None
        failure: sqlite3.Error | None = None
        try:
            db = sqlite3.connect(self.cold_path)
            with db:
                db.execute('DELETE FROM cursors WHERE path=?', (key,))
        except sqlite3.Error as error:
            failure = error
        finally:
            if db is not None:
                db.close()
        if failure is not None:
            # If the row cannot be removed, the index is not trustworthy as a
            # whole. Keep a recoverable copy and let the next scan rebuild from
            # the bounded transcript lookback.
            self._quarantine_cold_index(failure)

    def commit(self) -> None:
        """Persist the in-memory cursor after its signals were applied.

        The monitor uses this as a small transaction boundary: a cursor must
        not advance on disk before the state machine and notification queue
        have accepted the corresponding records.
        """
        self._save()

    def restore_cursor(self, path: str | Path, cursor: Cursor | None) -> None:
        key = self.key_for(path)
        if cursor is None:
            self.cursors.pop(key, None)
        else:
            self.cursors[key] = Cursor(**asdict(cursor))
        self._save()

    def key_for(self, path: str | Path) -> str:
        return canonical_path_key(path)

    def _cold_cursor(self, key: str) -> Cursor | None:
        if not self.cold_path.exists():
            return None
        db: sqlite3.Connection | None = None
        failure: sqlite3.Error | None = None
        row = None
        try:
            db = sqlite3.connect(self.cold_path)
            row = db.execute('SELECT value FROM cursors WHERE path=?', (key,)).fetchone()
        except sqlite3.Error as error:
            failure = error
        finally:
            if db is not None:
                db.close()
        if failure is not None:
            self._cold_recovery_keys.add(key)
            self._quarantine_cold_index(failure)
            return None
        if row is None:
            return None
        try:
            value = json.loads(row[0])
            if not isinstance(value, dict):
                raise TypeError('invalid-cold-cursor')
            cursor = Cursor(
                offset=max(0, int(value.get('offset', 0))),
                identity=int(value.get('identity', 0)),
                session_id=str(value.get('session_id') or session_id_from_path(Path(key))),
                turn_id=value.get('turn_id') if isinstance(value.get('turn_id'), str) else None,
                skipping_oversized_line=bool(value.get('skipping_oversized_line', False)),
                context_incomplete=bool(value.get('context_incomplete', False)),
                needs_context=bool(value.get('needs_context', False)),
            )
        except (json.JSONDecodeError, TypeError, ValueError, KeyError) as error:
            self._cold_recovery_keys.add(key)
            self._discard_cold_row(key)
            self._log_cold_error('cold-cursor-row-discarded', error)
            return None
        return cursor

    def cursor_for(self, path: str | Path, *, promote: bool = True) -> Cursor | None:
        key = self.key_for(path)
        if key not in self.cursors:
            cursor = self._cold_cursor(key)
            if cursor is not None:
                if not promote:
                    return cursor
                self.cursors[key] = cursor
        return self.cursors.get(key)

    def is_known(self, path: Path) -> bool:
        key = self.key_for(path)
        return key in self.cursors or self._cold_cursor(key) is not None

    def register_at_end(self, path: Path) -> Cursor:
        resolved = path.resolve()
        stat = resolved.stat()
        cursor = Cursor(offset=stat.st_size, identity=int(getattr(stat, "st_ino", 0)), session_id=session_id_from_path(resolved))
        self.cursors[self.key_for(resolved)] = cursor
        self._save()
        return cursor

    def register_from_start(self, path: Path) -> Cursor:
        resolved = path.resolve()
        stat = resolved.stat()
        cursor = Cursor(offset=0, identity=int(getattr(stat, "st_ino", 0)), session_id=session_id_from_path(resolved))
        key = self.key_for(resolved)
        self._cold_recovery_keys.discard(key)
        self.cursors[key] = cursor
        self._save()
        return cursor

    def register_with_lookback(self, path: Path, *, lookback_bytes: int = 32 * 1024 * 1024) -> Cursor:
        """Register near EOF, aligned to the start of a complete JSONL record.

        Hooks and rollout writes are produced by different processes.  A very fast
        turn can therefore reach the rollout before its hook event reaches our
        spool.  Looking back a bounded amount lets the monitor recover that turn
        without replaying the whole conversation.
        """
        resolved = path.resolve()
        stat = resolved.stat()
        start = max(0, stat.st_size - max(1, lookback_bytes))
        if start:
            with resolved.open("rb") as stream:
                stream.seek(start)
                while True:
                    chunk = stream.read(64 * 1024)
                    if not chunk:
                        start = stat.st_size
                        break
                    newline = chunk.find(b"\n")
                    if newline >= 0:
                        start = stream.tell() - len(chunk) + newline + 1
                        break
        cursor = Cursor(
            offset=start,
            identity=int(getattr(stat, "st_ino", 0)),
            session_id=session_id_from_path(resolved),
        )
        key = self.key_for(resolved)
        self._cold_recovery_keys.discard(key)
        self.cursors[key] = cursor
        self._save()
        return cursor

    def known_paths(self) -> list[Path]:
        return [Path(path) for path in self.cursors]

    def read_new_signals(self, path: Path, *, max_bytes: int = 32 * 1024 * 1024,
                         persist: bool = True) -> list[Signal]:
        resolved = path.resolve()
        key = self.key_for(resolved)
        stat = resolved.stat()
        cursor = self.cursor_for(resolved)
        if cursor is None:
            if key in self._cold_recovery_keys:
                cursor = self.register_with_lookback(resolved)
            else:
                cursor = self.register_from_start(resolved)
        identity = int(getattr(stat, "st_ino", 0))
        if stat.st_size < cursor.offset or (cursor.identity and identity and cursor.identity != identity):
            # Rollout names are unique and append-only.  A shorter/replaced file
            # at the same name is therefore unsafe to replay from byte zero.
            cursor.offset = stat.st_size
            cursor.identity = identity
            cursor.session_id = session_id_from_path(resolved)
            cursor.turn_id = None
            cursor.skipping_oversized_line = False
            if persist:
                self._save()
            return []

        signals: list[Signal] = []
        with resolved.open("rb") as stream:
            stream.seek(cursor.offset)
            raw = stream.read(max_bytes)
        if not raw:
            return []
        if cursor.skipping_oversized_line:
            newline = raw.find(b"\n")
            if newline < 0:
                cursor.offset += len(raw)
                if persist:
                    self._save()
                return []
            cursor.offset += newline + 1
            cursor.skipping_oversized_line = False
            if persist:
                self._save()
            # The next bounded monitor iteration reads the following records.
            return []
        last_newline = raw.rfind(b"\n")
        if last_newline < 0:
            if len(raw) >= max_bytes:
                self._log_oversized_record()
                cursor.context_incomplete = True
                cursor.offset += len(raw)
                cursor.skipping_oversized_line = True
                if persist:
                    self._save()
            return []
        complete = raw[: last_newline + 1]
        consumed = 0
        session_id = cursor.session_id
        for raw_line in complete.splitlines(keepends=True):
            consumed += len(raw_line)
            try:
                line = raw_line.decode("utf-8")
                parsed = parse_rollout_line(line, session_id=session_id, transcript_path=str(resolved))
            except (UnicodeDecodeError, ValueError, TypeError, KeyError):
                cursor.context_incomplete = True
                try:
                    append_json_line(self.state_path.parent.parent / 'logs' / 'tailer-errors.jsonl',
                                     {'event': 'invalid-record-skipped', 'offset': cursor.offset + consumed - len(raw_line),
                                      'at': time.time()})
                except OSError:
                    pass
                continue
            for signal in parsed:
                if signal.kind == SignalKind.TURN_STARTED:
                    cursor.context_incomplete = False
                if cursor.context_incomplete:
                    signal = Signal(kind=signal.kind, observed_at=signal.observed_at, session_id=signal.session_id,
                                    turn_id=signal.turn_id, transcript_path=signal.transcript_path,
                                    source=signal.source, data={**signal.data, 'context_incomplete': True})
                if signal.kind == SignalKind.SESSION_STARTED and signal.session_id:
                    session_id = signal.session_id
                    cursor.session_id = session_id
                if signal.turn_id and signal.kind in (SignalKind.TURN_STARTED, SignalKind.ACTIVITY):
                    cursor.turn_id = signal.turn_id
                elif (signal.turn_id is None and cursor.turn_id is not None
                      and signal.source == 'rollout'):
                    # Rollout records such as response_item may omit their
                    # turn_id after an explicit task_started in the same
                    # append-only transcript. This is a per-transcript
                    # context recovery, not the shared placeholder identity
                    # that hook payloads must never receive.
                    signal = Signal(kind=signal.kind, observed_at=signal.observed_at,
                                    session_id=signal.session_id, turn_id=cursor.turn_id,
                                    transcript_path=signal.transcript_path, source=signal.source, data=signal.data)
                if not signal.session_id:
                    signal = Signal(
                        kind=signal.kind,
                        observed_at=signal.observed_at,
                        session_id=session_id,
                        turn_id=signal.turn_id,
                        transcript_path=signal.transcript_path,
                        source=signal.source,
                        data=signal.data,
                    )
                signals.append(signal)
        cursor.offset += consumed
        cursor.identity = identity
        if persist:
            self._save()
        return signals
