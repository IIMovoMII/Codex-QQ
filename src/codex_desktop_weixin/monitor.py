from __future__ import annotations

import json
import os
import queue
import shutil
import sys
import time
from dataclasses import replace as dataclass_replace
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
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
        self.last_reconciliation = 0.0
        self.last_process_check = 0.0
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
        changed = False
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
            if signal.session_id:
                tid = self.machine.active_turn_by_session.get(signal.session_id) or signal.turn_id
                runtime = self.machine.turns.get((signal.session_id, tid))
                if runtime:
                    status = {"turn_id": runtime.turn_id, "state": runtime.state.value}
                    if self.control_status.get(signal.session_id) != status:
                        self.control_status[signal.session_id] = status
                        changed = True
        if changed:
            # State transitions only; assistant deltas do not rewrite this index.
            atomic_write_json(self.paths["state"] / "control-status.json", {
                "monitor_pid": os.getpid(), "monitor_created": self.monitor_created,
                "threads": self.control_status,
            })

    def _read_transcript(
        self,
        path: Path,
        *,
        target_turn_id: str | None = None,
        not_before: datetime | None = None,
    ) -> None:
        try:
            if is_desktop_rollout(path) is not True:
                return
        except (FileNotFoundError, PermissionError, OSError):
            return
        # Drain a bounded backlog immediately.  Ordinarily this loop runs once;
        # multiple iterations only occur after startup/race recovery.
        for _ in range(8):
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
                return
            if target_turn_id:
                signals = [
                    signal
                    for signal in signals
                    if signal.kind == SignalKind.SESSION_STARTED or signal.turn_id == target_turn_id
                ]
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
                raise
            if after <= before:
                break
            try:
                if after >= path.stat().st_size:
                    break
            except OSError:
                break

    def _register_from_hook(self, transcript: Path, turn_id: str | None) -> None:
        try:
            desktop_rollout = is_desktop_rollout(transcript)
        except (FileNotFoundError, PermissionError, OSError):
            return
        if desktop_rollout is not True:
            return
        if not self.tailer.is_known(transcript):
            # SessionStart/SessionEnd have no turn id.  Do not advance an unknown
            # transcript here: a queued UserPromptSubmit may still need to recover
            # a complete fast turn from the same file.
            if not turn_id:
                return
            try:
                self.tailer.register_with_lookback(transcript)
            except (FileNotFoundError, PermissionError, OSError):
                return
        self._read_transcript(transcript, target_turn_id=turn_id)

    def _process_spool_file(self, path: Path) -> None:
        try:
            event = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(event, dict):
                raise ValueError("spool event is not an object")
            transcript_text = event.get("transcript_path")
            transcript = Path(transcript_text) if isinstance(transcript_text, str) and transcript_text else None
            if transcript is not None:
                try:
                    desktop_rollout = is_desktop_rollout(transcript)
                except (FileNotFoundError, PermissionError, OSError):
                    desktop_rollout = None
                if desktop_rollout is None:
                    # A hook can race the first JSONL write.  Keep the event in
                    # incoming so reconciliation can retry it shortly.
                    return
                if not desktop_rollout:
                    destination = self.paths["processed"] / path.name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(path, destination)
                    return
            turn_id = str(event.get("turn_id") or "") or None
            hook_signals = hook_event_to_signals(event)
            # UserPromptSubmit is the logical beginning of a turn.  It must be
            # applied first when the rollout has already raced all the way to a
            # terminal record; otherwise a late hook could reopen a completed turn.
            prompt_started = event.get("hook_event_name") == "UserPromptSubmit"
            if prompt_started:
                self._apply(hook_signals)
            if transcript is not None:
                self._register_from_hook(transcript, turn_id)
            if not prompt_started:
                self._apply(hook_signals)
            destination = self.paths["processed"] / path.name
        except Exception:
            destination = self.paths["failed"] / path.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(path, destination)
        except FileNotFoundError:
            pass

    def _process_all_spool(self) -> None:
        for path in sorted(self.paths["incoming"].glob("*.json")):
            self._process_spool_file(path)

    def _handle_change(self, change: DirectoryChange) -> None:
        if change.path is None:
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
        self._process_all_spool()
        for path in self.tailer.known_paths():
            try:
                stat = path.stat()
            except OSError:
                continue
            cursor = self.tailer.cursor_for(path)
            if cursor and stat.st_size != cursor.offset:
                self._read_transcript(path)
        self.last_reconciliation = time.monotonic()

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
        self.watchers = [
            WindowsDirectoryWatcher(self.paths["incoming"], self.events, recursive=False),
            WindowsDirectoryWatcher(self.session_root, self.events, recursive=True),
        ]
        for watcher in self.watchers:
            watcher.start()
        self._process_all_spool()
        self._check_processes()

    def close(self) -> None:
        for watcher in self.watchers:
            watcher.close()
        self.watchers.clear()

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
                    self._handle_change(change)
                now = time.monotonic()
                standby = self._standby()
                if now - self.last_reconciliation >= (120.0 if standby else self.config.reconciliation_seconds):
                    self._reconcile()
                if now - self.last_process_check >= (30.0 if standby else self.config.process_check_seconds):
                    self._check_processes()
                self._emit(self.machine.tick())
                self.notifier.flush(max_items=1)
        finally:
            self.close()
            if self.config.manage_cc_connect:
                stop_cc_connect_service(self.paths["root"])
