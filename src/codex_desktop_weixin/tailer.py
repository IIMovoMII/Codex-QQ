from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from models import Signal, SignalKind
from rollout import parse_rollout_line
from runtime_io import atomic_write_json


_SESSION_ID_PATTERN = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?=\.jsonl$)", re.IGNORECASE)


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


class IncrementalTailer:
    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path
        self.cursors: dict[str, Cursor] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        if not isinstance(raw, dict):
            return
        for path, value in raw.items():
            if not isinstance(value, dict):
                continue
            try:
                candidate = Cursor(
                    offset=max(0, int(value.get("offset", 0))),
                    identity=int(value.get("identity", 0)),
                    session_id=str(value.get("session_id") or session_id_from_path(Path(path))),
                    turn_id=value.get('turn_id') if isinstance(value.get('turn_id'), str) else None,
                )
                key = canonical_path_key(path)
                existing = self.cursors.get(key)
                # A Codex hook can report ``\\?\C:\...`` while the directory
                # watcher reports ``C:\...``.  When upgrading an old state file,
                # keep the furthest cursor so the alias can never replay history.
                if existing is None or candidate.offset > existing.offset:
                    self.cursors[key] = candidate
            except (TypeError, ValueError):
                continue

    def _save(self) -> None:
        atomic_write_json(self.state_path, {path: asdict(cursor) for path, cursor in self.cursors.items()})

    def key_for(self, path: str | Path) -> str:
        return canonical_path_key(path)

    def cursor_for(self, path: str | Path) -> Cursor | None:
        return self.cursors.get(self.key_for(path))

    def is_known(self, path: Path) -> bool:
        return self.cursor_for(path) is not None

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
        self.cursors[self.key_for(resolved)] = cursor
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
        self.cursors[self.key_for(resolved)] = cursor
        self._save()
        return cursor

    def known_paths(self) -> list[Path]:
        return [Path(path) for path in self.cursors]

    def read_new_signals(self, path: Path, *, max_bytes: int = 32 * 1024 * 1024) -> list[Signal]:
        resolved = path.resolve()
        key = self.key_for(resolved)
        stat = resolved.stat()
        cursor = self.cursors.get(key)
        if cursor is None:
            cursor = self.register_from_start(resolved)
        identity = int(getattr(stat, "st_ino", 0))
        if stat.st_size < cursor.offset or (cursor.identity and identity and cursor.identity != identity):
            # Rollout names are unique and append-only.  A shorter/replaced file
            # at the same name is therefore unsafe to replay from byte zero.
            cursor.offset = stat.st_size
            cursor.identity = identity
            cursor.session_id = session_id_from_path(resolved)
            cursor.turn_id = None
            self._save()
            return []

        signals: list[Signal] = []
        with resolved.open("rb") as stream:
            stream.seek(cursor.offset)
            raw = stream.read(max_bytes)
        if not raw:
            return []
        last_newline = raw.rfind(b"\n")
        if last_newline < 0:
            return []
        complete = raw[: last_newline + 1]
        consumed = 0
        session_id = cursor.session_id
        for raw_line in complete.splitlines(keepends=True):
            consumed += len(raw_line)
            try:
                line = raw_line.decode("utf-8")
                parsed = parse_rollout_line(line, session_id=session_id, transcript_path=str(resolved))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            for signal in parsed:
                if signal.kind == SignalKind.SESSION_STARTED and signal.session_id:
                    session_id = signal.session_id
                    cursor.session_id = session_id
                if signal.turn_id and signal.kind in (SignalKind.TURN_STARTED, SignalKind.ACTIVITY):
                    cursor.turn_id = signal.turn_id
                elif signal.turn_id is None and cursor.turn_id is not None:
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
        self._save()
        return signals
