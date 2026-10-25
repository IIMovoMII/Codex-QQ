from __future__ import annotations

import hashlib
import copy
import json
import os
import queue
import shutil
import sys
import time
from dataclasses import replace as dataclass_replace
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from hook_bridge import hook_event_to_signals
from models import Notification, Signal, SignalKind
from metadata import ThreadMetadataResolver
from notifier import NotificationDispatcher
from launcher import stop_cc_connect_service, _query_process_identity
from process_watch import ProcessStatus, get_codex_desktop_process_status
from runtime_io import default_data_dir, ensure_runtime_dirs, atomic_write_json
from rollout import is_desktop_rollout
from state_machine import StateMachine
from tailer import IncrementalTailer
from winwatch import DirectoryChange, WindowsDirectoryWatcher


MISSING_TRANSCRIPT_GRACE = timedelta(days=7)
ORPHAN_RECORD_RETENTION = timedelta(days=30)
MAX_ORPHAN_RECORDS = 500
PROCESSED_SPOOL_RETENTION = timedelta(days=7)
FAILED_SPOOL_RETENTION = timedelta(days=30)
MAX_PROCESSED_SPOOL_FILES = 2000
MAX_FAILED_SPOOL_FILES = 500
SPOOL_CLEANUP_INTERVAL_SECONDS = 6 * 60 * 60
SENT_OUTBOX_RETENTION = timedelta(days=7)
FAILED_OUTBOX_RETENTION = timedelta(days=30)
OBSOLETE_OUTBOX_RETENTION = timedelta(days=30)
MAX_SENT_OUTBOX_FILES = 1000
MAX_FAILED_OUTBOX_FILES = 500
MAX_OBSOLETE_OUTBOX_FILES = 500
ROTATED_LOG_RETENTION = timedelta(days=30)
MAX_ROTATED_LOG_FILES = 4
MAX_ACTIVE_LOG_BYTES = 8 * 1024 * 1024
MAX_CONTROL_STATUS_ENTRIES = 2000
MAX_RECENT_TRANSCRIPTS = 1000
MAX_RECENT_SCAN_ENTRIES = 10000


@dataclass(frozen=True)
class MonitorConfig:
    codex_home: Path
    data_dir: Path
    reconciliation_seconds: float = 25.0
    process_check_seconds: float = 5.0
    process_exit_grace_seconds: float = 15.0
    exit_with_desktop: bool = False
    desktop_exit_grace_seconds: float = 20.0
    manage_cc_connect: bool = False
    unknown_rollout_grace_seconds: float = 120.0
    low_power_idle: bool = False


class DesktopMonitor:
    def __init__(self, config: MonitorConfig) -> None:
        self.config = config
        self.paths = ensure_runtime_dirs(config.data_dir)
        self.tailer = IncrementalTailer(self.paths["state"] / "cursors.json")
        self.machine = StateMachine()
        self.notifier = NotificationDispatcher(self.paths["root"])
        self.metadata = ThreadMetadataResolver(config.codex_home)
        self.events: queue.Queue[DirectoryChange] = queue.Queue()
        self.watchers: list[WindowsDirectoryWatcher] = []
        self._watch_roots: list[Path] = []
        self.last_reconciliation = 0.0
        self.last_process_check = 0.0
        self.last_spool_cleanup: float | None = None
        self.last_process_status: ProcessStatus | None = None
        self.process_missing_since: float | None = None
        self.desktop_seen = False
        self.desktop_missing_since: float | None = None
        self.exit_requested = False
        self.control_status: dict[str, dict[str, str]] = {}
        self.monitor_created = _query_process_identity(os.getpid()).get("process_start_time")
        self.async_not_before = datetime.now(timezone.utc) - timedelta(seconds=config.unknown_rollout_grace_seconds)

    @property
    def session_root(self) -> Path:
        return self.config.codex_home / "sessions"

    def _emit(self, notifications: list[Notification]) -> None:
        for notification in notifications:
            metadata = self.metadata.resolve(notification.session_id)
            label = metadata.title or notification.project_name
            if label:
                notification = replace(notification, title=f"【{label}】{notification.title}")
            self.notifier.emit(notification)
            if sys.stdout is None:  # pythonw has no console; durable logs remain.
                continue
            print(
                json.dumps(
                    {
                        "state": notification.state.value,
                        "title": notification.title,
                        "session_id": notification.session_id,
                        "turn_id": notification.turn_id,
                        "confidence": notification.confidence.value,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    def _apply(self, signals: list[Signal], *, cutoff: datetime | None = None) -> None:
        machine_before = copy.deepcopy(self.machine)
        status_before = copy.deepcopy(self.control_status)
        changed = False
        try:
            for signal in sorted(signals, key=lambda item: item.observed_at):
                # Questions are independent notices, not synthetic running turns.
                # An async question must not create/reset the task's state machine.
                notes = [] if signal.kind in (SignalKind.ASYNC_QUESTION_CREATED, SignalKind.ASYNC_QUESTION_ANSWERED) else self.machine.apply(signal)
                if signal.kind == SignalKind.ASYNC_QUESTION_ANSWERED and signal.session_id:
                    self.notifier.resolve_async_answers(signal.session_id, signal.data.get('answers') or [])
                elif signal.kind == SignalKind.ASYNC_QUESTION_CREATED and signal.observed_at >= self.async_not_before:
                    from async_questions import notification
                    turn = signal.turn_id or self.machine.active_turn_by_session.get(signal.session_id or '') or 'question:' + signal.data.get('call_id', '')
                    context = self.machine.session_context.get(signal.session_id or '', {})
                    note = notification(signal, turn_id=turn, model=context.get('model') or '')
                    if note:
                        notes.append(note)
                elif signal.kind in (SignalKind.TURN_COMPLETED, SignalKind.TURN_ABORTED) and signal.session_id and signal.turn_id:
                    self.notifier.close_async_turn(signal.session_id, signal.turn_id)
                if cutoff is None or signal.observed_at >= cutoff:
                    self._emit(notes)
                if signal.session_id and signal.kind == SignalKind.SESSION_ENDED:
                    if self.control_status.pop(signal.session_id, None) is not None:
                        changed = True
                elif signal.session_id:
                    tid = self.machine.active_turn_by_session.get(signal.session_id) or signal.turn_id
                    runtime = self.machine.turns.get((signal.session_id, tid))
                    if runtime:
                        status = {"turn_id": runtime.turn_id, "state": runtime.state.value}
                        if self.control_status.get(signal.session_id) != status:
                            self.control_status.pop(signal.session_id, None)
                            self.control_status[signal.session_id] = status
                            while len(self.control_status) > MAX_CONTROL_STATUS_ENTRIES:
                                self.control_status.pop(next(iter(self.control_status)))
                            changed = True
            if changed:
                # State transitions only; assistant deltas do not rewrite this index.
                atomic_write_json(self.paths["state"] / "control-status.json", {
                    "monitor_pid": os.getpid(), "monitor_created": self.monitor_created,
                    "threads": self.control_status,
                })
        except Exception:
            # Transcript cursors are committed after this method returns. Keep
            # the in-memory reducer aligned with that durable boundary too.
            self.machine = machine_before
            self.control_status = status_before
            raise

    def _read_transcript(
        self,
        path: Path,
        *,
        target_turn_id: str | None = None,
        hook_turn_id: str | None = None,
        not_before: datetime | None = None,
    ) -> bool:
        """Drain new records and report this hook turn's observed terminal event."""
        try:
            if is_desktop_rollout(path) is not True:
                return False
        except (FileNotFoundError, PermissionError, OSError):
            return False
        # Drain a bounded backlog immediately.  Ordinarily this loop runs once;
        # multiple iterations only occur after startup/race recovery.
        target_seen = target_turn_id is None
        hook_terminal_seen = False
        for _ in range(8):
            machine_before = copy.deepcopy(self.machine)
            status_before = copy.deepcopy(self.control_status)
            try:
                cursor = self.tailer.cursor_for(path)
                before = cursor.offset if cursor is not None else -1
                # Keep the cursor on disk at the last successfully applied
                # boundary.  If notification/state persistence fails, the
                # same records must be replayable after restart.
                previous_cursor = self.tailer.cursor_for(path)
                previous_cursor = dataclass_replace(previous_cursor) if previous_cursor is not None else None
                signals = self.tailer.read_new_signals(path, persist=False)
                cursor = self.tailer.cursor_for(path)
                after = cursor.offset if cursor is not None else before
            except (FileNotFoundError, PermissionError, OSError):
                return hook_terminal_seen
            if not target_seen:
                # A new cursor may read history before the hook turn and newer
                # fast turns after it. The cursor advances across this batch,
                # so discard only preceding history and retain later signals.
                selected = []
                for signal in signals:
                    if signal.turn_id == target_turn_id:
                        target_seen = True
                    if target_seen or signal.kind == SignalKind.SESSION_STARTED:
                        selected.append(signal)
                signals = selected
            if not_before is not None:
                signals = [
                    signal
                    for signal in signals
                    if signal.kind == SignalKind.SESSION_STARTED or signal.observed_at >= not_before
                ]
            try:
                self._apply(signals)
                self.tailer.commit()
            except Exception:
                self.tailer.restore_cursor(path, previous_cursor)
                self.machine = machine_before
                self.control_status = status_before
                raise
            hook_terminal_seen |= any(
                signal.turn_id == hook_turn_id
                and signal.kind in (SignalKind.TURN_COMPLETED, SignalKind.TURN_ABORTED)
                for signal in signals
            )
            if after <= before:
                break
            try:
                if after >= path.stat().st_size:
                    break
            except OSError:
                break
        return hook_terminal_seen

    def _register_from_hook(self, transcript: Path, turn_id: str | None) -> bool:
        try:
            desktop_rollout = is_desktop_rollout(transcript)
        except (FileNotFoundError, PermissionError, OSError):
            return False
        if desktop_rollout is not True:
            return False
        recovering_history = not self.tailer.is_known(transcript)
        if recovering_history:
            # SessionStart/SessionEnd have no turn id.  Do not advance an unknown
            # transcript here: a queued UserPromptSubmit may still need to recover
            # a complete fast turn from the same file.
            if not turn_id:
                return False
            try:
                self.tailer.register_with_lookback(transcript)
            except (FileNotFoundError, PermissionError, OSError):
                return False
        # A known cursor already excludes history. Its unread records may
        # finish the previous turn before this hook's turn begins.
        return self._read_transcript(
            transcript,
            target_turn_id=turn_id if recovering_history else None,
            hook_turn_id=turn_id,
        )

    def _process_spool_file(self, path: Path) -> None:
        try:
            event = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(event, dict):
                raise ValueError("spool event is not an object")
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            self._archive_spool_file(path, self.paths["failed"])
            return
        except OSError:
            return

        try:
            transcript_text = event.get("transcript_path")
            transcript = Path(transcript_text) if isinstance(transcript_text, str) and transcript_text else None
            if transcript is not None:
                try:
                    desktop_rollout = is_desktop_rollout(transcript)
                except FileNotFoundError:
                    if self._archive_expired_missing_transcript(path, event, transcript_text):
                        return
                    # A hook can arrive before the rollout is created. Keep
                    # recent events; only age out a transcript that is still
                    # definitively absent after the grace period.
                    return
                except (PermissionError, OSError):
                    # A transient lock or I/O error is not proof that history
                    # was deleted. Leave the event retryable.
                    return
                if desktop_rollout is None:
                    # A hook can race the first JSONL write.  Keep the event in
                    # incoming so reconciliation can retry it shortly.
                    return
                if not desktop_rollout:
                    self._archive_spool_file(path, self.paths["processed"])
                    return
            turn_id = str(event.get("turn_id") or "") or None
            hook_signals = hook_event_to_signals(event)
            native_terminal = False
            if transcript is not None:
                native_terminal = self._register_from_hook(transcript, turn_id)
            # Drain authoritative records before a new prompt can retire the
            # preceding turn. If this hook's turn already ended in the native
            # transcript, delayed hooks must not reopen it.
            if native_terminal:
                hook_signals = [signal for signal in hook_signals
                                if signal.kind in (SignalKind.SESSION_STARTED, SignalKind.SESSION_ENDED)]
            self._apply(hook_signals)
        except Exception as error:
            # Keep a failed application retryable. In particular, a transient
            # outbox or state-file error must not move the only hook event to
            # failed/ and permanently lose its notification.
            self._log_monitor_error("spool-process", error)
            return
        self._archive_spool_file(path, self.paths["processed"])

    def _archive_spool_file(self, path: Path, destination_root: Path) -> bool:
        destination = destination_root / path.name
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(path, destination)
            return True
        except FileNotFoundError:
            return not path.exists()
        except OSError as error:
            self._log_monitor_error("spool-archive", error)
            return False

    def _log_monitor_error(self, stage: str, error: BaseException) -> None:
        try:
            from runtime_io import append_json_line
            append_json_line(self.paths["logs"] / "monitor-errors.jsonl", {
                "at": datetime.now(timezone.utc).isoformat(),
                "stage": stage,
                "error_type": type(error).__name__,
            })
        except OSError:
            pass

    @staticmethod
    def _event_time(event: dict, spool_path: Path) -> datetime | None:
        raw = event.get("observed_at")
        if isinstance(raw, str):
            try:
                value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
            except ValueError:
                pass
        try:
            return datetime.fromtimestamp(spool_path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            return None

    def _archive_expired_missing_transcript(
        self, spool_path: Path, event: dict, transcript_text: str
    ) -> bool:
        observed_at = self._event_time(event, spool_path)
        now = datetime.now(timezone.utc)
        if observed_at is None or now - observed_at < MISSING_TRANSCRIPT_GRACE:
            return False

        index_path = self.paths["root"] / "spool" / "orphaned.json"
        try:
            if index_path.exists():
                index = json.loads(index_path.read_text(encoding="utf-8"))
                if not isinstance(index, dict) or index.get("version") != 1 or not isinstance(index.get("records"), list):
                    return False
                records = index["records"]
            else:
                records = []
            cutoff = now - ORPHAN_RECORD_RETENTION
            retained = []
            for record in records:
                if not isinstance(record, dict):
                    return False
                archived = datetime.fromisoformat(str(record["archived_at"]).replace("Z", "+00:00"))
                if archived.tzinfo is None:
                    archived = archived.replace(tzinfo=timezone.utc)
                if archived.astimezone(timezone.utc) >= cutoff:
                    retained.append(record)

            event_id = str(event.get("event_id") or spool_path.stem)
            event_hash = hashlib.sha256(event_id.encode("utf-8", errors="replace")).hexdigest()
            if any(record.get("event_id_sha256") == event_hash for record in retained):
                spool_path.unlink(missing_ok=True)
                return True

            hook_name = event.get("hook_event_name")
            if not isinstance(hook_name, str) or len(hook_name) > 40:
                hook_name = "unknown"
            session_id = event.get("session_id")
            if not isinstance(session_id, str) or len(session_id) > 100:
                session_id = ""
            turn_id = event.get("turn_id")
            if not isinstance(turn_id, str) or len(turn_id) > 160:
                turn_id = ""
            retained.append({
                "event_id_sha256": event_hash,
                "hook_event_name": hook_name,
                "session_id": session_id,
                "turn_id": turn_id,
                "observed_at": observed_at.isoformat(),
                "transcript_path_sha256": hashlib.sha256(
                    transcript_text.encode("utf-8", errors="replace")
                ).hexdigest(),
                "reason": "transcript_missing_after_grace_period",
                "archived_at": now.isoformat(),
            })
            atomic_write_json(index_path, {"version": 1, "records": retained[-MAX_ORPHAN_RECORDS:]})
            spool_path.unlink(missing_ok=True)
            return True
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            # Never discard the source event if the diagnostic index cannot be
            # read or durably updated. It remains available for later repair.
            return False

    @staticmethod
    def _prune_spool_directory(path: Path, *, retention: timedelta, max_files: int, now: float) -> None:
        entries = []
        for entry in path.glob("*.json"):
            try:
                entries.append((entry.stat().st_mtime, entry))
            except OSError:
                continue
        cutoff = now - retention.total_seconds()
        retained = []
        for modified, entry in entries:
            if modified < cutoff:
                try:
                    entry.unlink()
                except OSError:
                    retained.append((modified, entry))
            else:
                retained.append((modified, entry))
        retained.sort(key=lambda item: item[0], reverse=True)
        for _, entry in retained[max_files:]:
            try:
                entry.unlink()
            except OSError:
                continue

    @staticmethod
    def _rotate_and_prune_log(path: Path, *, now: float) -> None:
        if path.exists():
            try:
                if path.stat().st_size > MAX_ACTIVE_LOG_BYTES:
                    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                    archived = path.with_name(f"{path.stem}-{stamp}-{os.getpid()}.jsonl")
                    suffix = 1
                    while archived.exists():
                        archived = path.with_name(f"{path.stem}-{stamp}-{os.getpid()}-{suffix}.jsonl")
                        suffix += 1
                    os.replace(path, archived)
                    path.touch()
            except OSError:
                # Keep the active log intact if another process briefly holds it.
                pass

        archives = []
        pattern = f"{path.stem}-*.jsonl"
        for entry in path.parent.glob(pattern):
            try:
                archives.append((entry.stat().st_mtime, entry))
            except OSError:
                continue
        cutoff = now - ROTATED_LOG_RETENTION.total_seconds()
        retained = []
        for modified, entry in archives:
            if modified < cutoff:
                try:
                    entry.unlink()
                except OSError:
                    retained.append((modified, entry))
            else:
                retained.append((modified, entry))
        retained.sort(key=lambda item: item[0], reverse=True)
        for _, entry in retained[MAX_ROTATED_LOG_FILES:]:
            try:
                entry.unlink()
            except OSError:
                continue

    def _cleanup_spool_retention(self, *, now_mono: float | None = None) -> None:
        if now_mono is None:
            now_mono = time.monotonic()
        if (self.last_spool_cleanup is not None
                and now_mono - self.last_spool_cleanup < SPOOL_CLEANUP_INTERVAL_SECONDS):
            return
        wall_now = time.time()
        self._prune_spool_directory(
            self.paths["processed"], retention=PROCESSED_SPOOL_RETENTION,
            max_files=MAX_PROCESSED_SPOOL_FILES, now=wall_now,
        )
        self._prune_spool_directory(
            self.paths["failed"], retention=FAILED_SPOOL_RETENTION,
            max_files=MAX_FAILED_SPOOL_FILES, now=wall_now,
        )
        outbox = self.paths["root"] / "outbox"
        for name, retention, maximum in (
            ("sent", SENT_OUTBOX_RETENTION, MAX_SENT_OUTBOX_FILES),
            ("failed", FAILED_OUTBOX_RETENTION, MAX_FAILED_OUTBOX_FILES),
            ("obsolete", OBSOLETE_OUTBOX_RETENTION, MAX_OBSOLETE_OUTBOX_FILES),
        ):
            self._prune_spool_directory(outbox / name, retention=retention, max_files=maximum, now=wall_now)
        marker_dir = outbox / 'sent-state'
        try:
            marker_entries = sorted(
                ((marker.stat().st_mtime, marker) for marker in marker_dir.glob('*.json')
                 if not (outbox / 'pending' / marker.name).exists()),
                key=lambda pair: pair[0], reverse=True)
            marker_cutoff = wall_now - SENT_OUTBOX_RETENTION.total_seconds()
            for modified, marker in marker_entries:
                if modified < marker_cutoff or marker not in [row[1] for row in marker_entries[:MAX_SENT_OUTBOX_FILES]]:
                    marker.unlink(missing_ok=True)
        except OSError:
            pass
        for name in ("shadow-notifications.jsonl", "delivery.jsonl", "monitor-errors.jsonl", "tailer-errors.jsonl",
                     "app.jsonl", "autostart.jsonl"):
            self._rotate_and_prune_log(self.paths["logs"] / name, now=wall_now)
        for name in ("cc-connect.log", "monitor-console.log", "autostart-console.log"):
            self._bound_open_log(self.paths["logs"] / name)
        index_path = self.paths["root"] / "spool" / "orphaned.json"
        try:
            if index_path.exists():
                index = json.loads(index_path.read_text(encoding="utf-8"))
                if isinstance(index, dict) and index.get("version") == 1 and isinstance(index.get("records"), list):
                    cutoff = datetime.now(timezone.utc) - ORPHAN_RECORD_RETENTION
                    records = []
                    for record in index["records"]:
                        if not isinstance(record, dict):
                            continue
                        archived = datetime.fromisoformat(str(record["archived_at"]).replace("Z", "+00:00"))
                        if archived.tzinfo is None:
                            archived = archived.replace(tzinfo=timezone.utc)
                        if archived.astimezone(timezone.utc) >= cutoff:
                            records.append(record)
                    records = records[-MAX_ORPHAN_RECORDS:]
                    if records != index["records"]:
                        atomic_write_json(index_path, {"version": 1, "records": records})
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            # A damaged diagnostic index is preserved for manual inspection.
            pass
        self.last_spool_cleanup = now_mono

    @staticmethod
    def _bound_open_log(path: Path) -> None:
        """Trim an externally opened append log in place when rename is blocked."""
        try:
            size = path.stat().st_size
            if size <= MAX_ACTIVE_LOG_BYTES:
                return
            with path.open('r+b') as stream:
                stream.seek(max(0, size - MAX_ACTIVE_LOG_BYTES // 2))
                tail = stream.read(MAX_ACTIVE_LOG_BYTES // 2)
                newline = tail.find(b'\n')
                if newline >= 0:
                    tail = tail[newline + 1:]
                stream.seek(0)
                stream.write(tail)
                stream.truncate()
                stream.flush()
        except OSError:
            pass

    def _process_all_spool(self) -> None:
        for path in sorted(self.paths["incoming"].glob("*.json")):
            try:
                self._process_spool_file(path)
            except Exception as error:
                self._log_monitor_error("spool-file", error)

    def _handle_change(self, change: DirectoryChange) -> None:
        if change.path is None:
            if change.action.startswith('watch_error'):
                self._log_monitor_error('watcher', OSError(change.action))
                self._restart_watcher(change.root)
            self._reconcile()
            return
        path = change.path
        if change.root == self.paths["incoming"]:
            if path.suffix.lower() == ".json" and path.exists():
                self._process_spool_file(path)
            return
        if path.suffix.lower() != ".jsonl":
            return
        if self.tailer.is_known(path):
            self._read_transcript(path)
        elif path.exists():
            try:
                desktop_rollout = is_desktop_rollout(path)
            except (FileNotFoundError, PermissionError, OSError):
                return
            if desktop_rollout is not True:
                return
            try:
                self.tailer.register_with_lookback(path)
            except (FileNotFoundError, PermissionError, OSError):
                return
            # An old conversation can be touched when Desktop restores its open
            # windows.  Parse only a bounded tail and discard old lifecycle
            # records so restoring a window cannot send its whole history.
            not_before = datetime.now(timezone.utc) - timedelta(
                seconds=max(0.0, self.config.unknown_rollout_grace_seconds)
            )
            self._read_transcript(path, not_before=not_before)

    def _reconcile(self) -> None:
        now_mono = time.monotonic()
        self._process_all_spool()
        self._cleanup_spool_retention(now_mono=now_mono)
        self._discover_recent_transcripts()
        self._ensure_watchers()
        for path in self.tailer.known_paths():
            try:
                stat = path.stat()
            except OSError:
                continue
            cursor = self.tailer.cursor_for(path)
            if cursor and stat.st_size != cursor.offset:
                self._read_transcript(path)
        self.last_reconciliation = now_mono

    def _discover_recent_transcripts(self) -> None:
        """Recover recent files missed by watcher overflow or watcher failure."""
        cutoff = datetime.now(timezone.utc) - timedelta(
            seconds=max(2 * self.config.reconciliation_seconds,
                        self.config.unknown_rollout_grace_seconds)
        )
        candidates: list[tuple[float, Path]] = []
        # Codex layouts use either a flat sessions directory or YYYY/MM/DD.
        # Scan only the active date window (bounded to eight days) instead of
        # recursively walking years of history on every reconciliation.
        days = min(8, max(2, int(self.config.unknown_rollout_grace_seconds // 86400) + 2))
        directories = {self.session_root}
        today = date.today()
        for offset in range(days):
            day = today - timedelta(days=offset)
            directories.add(self.session_root / f'{day.year:04d}' / f'{day.month:02d}' / f'{day.day:02d}')
        for directory in directories:
            try:
                for entry_index, path in enumerate(directory.glob('*.jsonl')):
                    if entry_index >= MAX_RECENT_SCAN_ENTRIES:
                        break
                    if self.tailer.is_known(path):
                        continue
                    try:
                        modified = path.stat().st_mtime
                    except OSError:
                        continue
                    if modified >= cutoff.timestamp():
                        candidates.append((modified, path))
            except OSError:
                continue
        candidates.sort(key=lambda item: item[0], reverse=True)
        for _, path in candidates[:MAX_RECENT_TRANSCRIPTS]:
            try:
                if is_desktop_rollout(path) is not True:
                    continue
                self.tailer.register_with_lookback(path)
                self._read_transcript(path, not_before=cutoff)
            except (FileNotFoundError, PermissionError, OSError, ValueError):
                continue

    def _active_runtimes(self):
        return self.machine.active_runtimes()

    def _sync_maintenance_marker(self) -> None:
        active = False
        path = self.paths["root"] / "maintenance.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            expires = datetime.fromisoformat(str(value.get("expires_at")).replace("Z", "+00:00")).astimezone(timezone.utc)
            active = expires > datetime.now(timezone.utc)
        except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
            active = False
        if active != self.machine.maintenance:
            self.machine.apply(
                Signal(
                    SignalKind.MAINTENANCE_STARTED if active else SignalKind.MAINTENANCE_ENDED,
                    source="profile-switcher",
                )
            )

    def _check_processes(self) -> None:
        self._sync_maintenance_marker()
        status = get_codex_desktop_process_status()
        now = time.monotonic()
        previous_desktop_alive = bool(self.last_process_status and self.last_process_status.desktop_alive)
        if status.desktop_alive:
            self.process_missing_since = None
            if not previous_desktop_alive:
                for runtime in self._active_runtimes():
                    self.machine.apply(
                        Signal(
                            SignalKind.PROCESS_STARTED,
                            session_id=runtime.session_id,
                            turn_id=runtime.turn_id,
                            source="process",
                        )
                    )
        elif self._active_runtimes():
            # The monitor may start after Desktop has already disappeared.
            # Do not require a previous alive sample or the active turn can
            # remain running forever and keep the monitor out of standby.
            if self.process_missing_since is None:
                self.process_missing_since = now
            elif now - self.process_missing_since >= self.config.process_exit_grace_seconds:
                # Preserve real terminal/error records written before exit, then
                # silently retire remaining turns. Process exit itself sends no QQ.
                self._reconcile()
                for runtime in list(self._active_runtimes()):
                    self._apply([
                        Signal(
                            SignalKind.PROCESS_STOPPED,
                            session_id=runtime.session_id,
                            turn_id=runtime.turn_id,
                            source="process",
                        )
                    ])
                self.process_missing_since = None

        if status.desktop_alive:
            self.desktop_seen = True
            self.desktop_missing_since = None
        elif self.desktop_seen:
            if self.desktop_missing_since is None:
                self.desktop_missing_since = now
            elif (
                self.config.exit_with_desktop
                and now - self.desktop_missing_since >= self.config.desktop_exit_grace_seconds
            ):
                self.exit_requested = True
        self.last_process_status = status
        self.last_process_check = now

    def start(self) -> None:
        self.session_root.mkdir(parents=True, exist_ok=True)
        self._watch_roots = [self.paths["incoming"], self.session_root]
        for root in self._watch_roots:
            self._restart_watcher(root)
        self._process_all_spool()
        self._cleanup_spool_retention()
        self._check_processes()

    def close(self) -> None:
        for watcher in self.watchers:
            try:
                watcher.close()
            except OSError as error:
                self._log_monitor_error('watcher-close', error)
        self.watchers.clear()

    def _restart_watcher(self, root: Path) -> None:
        retained = []
        for watcher in self.watchers:
            if getattr(watcher, 'root', None) == root.resolve():
                try:
                    watcher.close()
                except OSError as error:
                    self._log_monitor_error('watcher-close', error)
            else:
                retained.append(watcher)
        self.watchers = retained
        try:
            watcher = WindowsDirectoryWatcher(root, self.events,
                                              recursive=root == self.session_root)
            if not hasattr(watcher, 'root'):
                watcher.root = root.resolve()
            watcher.start()
            self.watchers.append(watcher)
        except OSError as error:
            self._log_monitor_error('watcher-start', error)

    def _ensure_watchers(self) -> None:
        for root in self._watch_roots:
            active = next((watcher for watcher in self.watchers
                           if getattr(watcher, 'root', None) == root.resolve()), None)
            if active is None or not getattr(active, 'alive', True):
                self._restart_watcher(root)

    def _guard(self, stage: str, callback: Callable[[], Any]) -> None:
        try:
            callback()
        except Exception as error:
            self._log_monitor_error(stage, error)

    def _standby(self) -> bool:
        return bool(self.config.low_power_idle and self.last_process_status is not None
                    and not self.last_process_status.desktop_alive and not self._active_runtimes()
                    and not next(self.notifier.pending.glob('*.json'), None))

    def run(
        self,
        *,
        duration_seconds: float | None = None,
        on_started: Callable[[], None] | None = None,
    ) -> None:
        started = time.monotonic()
        try:
            self.start()
            if on_started is not None:
                on_started()
            while (
                not self.exit_requested
                and (duration_seconds is None or time.monotonic() - started < duration_seconds)
                and not (self.paths["root"] / "stop.request").exists()
            ):
                try:
                    change = self.events.get(timeout=30.0 if self._standby() else 1.0)
                except queue.Empty:
                    change = None
                if self.exit_requested:
                    break
                if change is not None:
                    self._guard('change', lambda: self._handle_change(change))
                now = time.monotonic()
                standby = self._standby()
                if now - self.last_reconciliation >= (120.0 if standby else self.config.reconciliation_seconds):
                    self._guard('reconcile', self._reconcile)
                if now - self.last_process_check >= (30.0 if standby else self.config.process_check_seconds):
                    self._guard('process-check', self._check_processes)
                self._guard('state-tick', lambda: self._emit(self.machine.tick()))
                self._guard('notification-flush', lambda: self.notifier.flush(max_items=1))
        finally:
            self.close()
            if self.config.manage_cc_connect:
                stop_cc_connect_service(self.paths["root"])
