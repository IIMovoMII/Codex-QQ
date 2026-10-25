from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


PACKAGE_DIR = Path(__file__).resolve().parents[1] / "src" / "codex_desktop_weixin"
sys.path.insert(0, str(PACKAGE_DIR))

from monitor import DesktopMonitor, MonitorConfig
from models import Signal, SignalKind
from process_watch import ProcessStatus
from state_machine import MAX_RETAINED_TERMINAL_TURNS
from winwatch import DirectoryChange


class ControlStatusIndexTests(unittest.TestCase):
    def test_only_state_changes_publish_minimal_status_without_prompt_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            monitor = DesktopMonitor(MonitorConfig(codex_home=root / "codex", data_dir=root / "data"))
            with patch("monitor.atomic_write_json") as write:
                monitor._apply([Signal(SignalKind.TURN_STARTED, session_id="s", turn_id="t")])
                self.assertEqual(write.call_count, 1)
                monitor._apply([Signal(SignalKind.ASSISTANT_MESSAGE, session_id="s", turn_id="t", data={"text": "private test body"})])
                self.assertEqual(write.call_count, 1)
                encoded = json.dumps(write.call_args.args[1])
                self.assertNotIn("private test body", encoded)
                self.assertIn('"running"', encoded)

    def test_session_end_removes_status_and_recent_index_stays_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            monitor = DesktopMonitor(MonitorConfig(codex_home=root / 'codex', data_dir=root / 'data'))
            with patch('monitor.MAX_CONTROL_STATUS_ENTRIES', 2):
                for index in range(3):
                    monitor._apply([Signal(SignalKind.TURN_STARTED, session_id=f's{index}', turn_id='t')])
                self.assertLessEqual(len(monitor.control_status), 2)
                self.assertIn('s2', monitor.control_status)
                monitor._apply([Signal(SignalKind.SESSION_ENDED, session_id='s2')])
                self.assertNotIn('s2', monitor.control_status)
            saved = json.loads((monitor.paths['state'] / 'control-status.json').read_text(encoding='utf-8'))
            self.assertNotIn('s2', saved['threads'])

    def test_reconciliation_restarts_a_dead_directory_watcher(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            monitor = DesktopMonitor(MonitorConfig(codex_home=root / 'codex', data_dir=root / 'data'))
            class Watcher:
                def __init__(self, path, _events, recursive=False):
                    self.root = path.resolve(); self.alive = False; self.closed = False
                def start(self): self.alive = True
                def close(self): self.closed = True; self.alive = False
            dead = Watcher(monitor.session_root, monitor.events)
            monitor.watchers = [dead]; monitor._watch_roots = [monitor.session_root]
            with patch('monitor.WindowsDirectoryWatcher', Watcher):
                monitor._ensure_watchers()
            self.assertTrue(dead.closed)
            self.assertEqual(len(monitor.watchers), 1)
            self.assertTrue(monitor.watchers[0].alive)


def record(payload: dict, *, timestamp: str = "2026-08-23T00:00:00Z") -> str:
    return json.dumps(
        {"timestamp": timestamp, "type": "event_msg", "payload": payload}
    ) + "\n"


def session_meta(session_id: str, *, source: str = "vscode") -> str:
    return json.dumps(
        {
            "timestamp": "2026-08-23T00:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "originator": "Codex Desktop",
                "source": source,
                "cwd": "D:\\project",
                "model_provider": "openai",
            },
        }
    ) + "\n"


class MonitorRaceTests(unittest.TestCase):
    def test_reconcile_discovers_recent_new_transcript_without_watcher_event(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session_id = '99999999-9999-9999-9999-999999999999'
            monitor = DesktopMonitor(MonitorConfig(root / 'codex', root / 'runtime'))
            transcript = monitor.session_root / f'rollout-{session_id}.jsonl'
            transcript.parent.mkdir(parents=True)
            now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
            meta = json.loads(session_meta(session_id))
            meta['timestamp'] = now
            completion = record({'type': 'task_complete', 'turn_id': 'recent'}, timestamp=now)
            transcript.write_text(json.dumps(meta) + '\n' + completion, encoding='utf-8')
            self.assertFalse(monitor.tailer.is_known(transcript))
            monitor._reconcile()
            self.assertTrue(monitor.tailer.is_known(transcript))
            rows = [json.loads(row) for row in monitor.notifier.log_path.read_text(encoding='utf-8').splitlines()]
            self.assertEqual([(row['turn_id'], row['state']) for row in rows], [('recent', 'completed')])

    def test_notification_persistence_failure_rolls_back_state_and_replay_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session_id = '99999999-9999-9999-9999-999999999998'
            monitor = DesktopMonitor(MonitorConfig(root / 'codex', root / 'runtime'))
            transcript = monitor.session_root / f'rollout-{session_id}.jsonl'
            transcript.parent.mkdir(parents=True)
            now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
            meta = json.loads(session_meta(session_id)); meta['timestamp'] = now
            transcript.write_text(json.dumps(meta) + '\n' + record({'type': 'task_started', 'turn_id': 't'}, timestamp=now)
                                  + record({'type': 'task_complete', 'turn_id': 't'}, timestamp=now), encoding='utf-8')
            monitor.tailer.register_from_start(transcript)
            real_write = __import__('monitor').atomic_write_json
            def fail_index(path, value):
                if path.name == 'control-status.json':
                    raise OSError('temporary status write failure')
                return real_write(path, value)
            with patch('monitor.atomic_write_json', side_effect=fail_index):
                with self.assertRaises(OSError):
                    monitor._read_transcript(transcript)
            self.assertFalse(monitor.machine.active_runtimes())
            monitor._read_transcript(transcript)
            rows = [json.loads(row) for row in monitor.notifier.log_path.read_text(encoding='utf-8').splitlines()]
            self.assertEqual([row['state'] for row in rows], ['completed'])

    def test_hook_lookback_preserves_later_fast_turns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session_id = "66666666-6666-6666-6666-666666666666"
            monitor = DesktopMonitor(MonitorConfig(codex_home=root / "codex", data_dir=root / "runtime"))
            transcript = monitor.session_root / f"rollout-{session_id}.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text(
                session_meta(session_id)
                + record({"type": "task_started", "turn_id": "historical"})
                + record({"type": "task_complete", "turn_id": "historical"})
                + record({"type": "task_started", "turn_id": "first"})
                + record({"type": "task_complete", "turn_id": "first"})
                + record({"type": "task_started", "turn_id": "second"})
                + record({"type": "task_complete", "turn_id": "second"}),
                encoding="utf-8",
            )
            for turn in ("first", "second"):
                spool = monitor.paths["incoming"] / f"{turn}.json"
                spool.write_text(json.dumps({
                    "hook_event_name": "UserPromptSubmit", "session_id": session_id,
                    "turn_id": turn, "transcript_path": str(transcript),
                    "observed_at": "2026-08-23T00:00:01Z",
                }), encoding="utf-8")
                monitor._process_spool_file(spool)
            rows = [json.loads(line) for line in monitor.notifier.log_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([(row["turn_id"], row["state"]) for row in rows],
                             [("first", "completed"), ("second", "completed")])
            self.assertNotIn((session_id, "historical"), monitor.machine.turns)
            self.assertEqual(monitor.machine.active_runtimes(), [])

    def test_next_prompt_hook_cannot_hide_previous_turn_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session_id = "77777777-7777-7777-7777-777777777777"
            monitor = DesktopMonitor(MonitorConfig(codex_home=root / "codex", data_dir=root / "runtime"))
            transcript = monitor.session_root / f"rollout-{session_id}.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text(session_meta(session_id)
                                  + record({"type": "task_started", "turn_id": "first"}), encoding="utf-8")
            monitor.tailer.register_from_start(transcript)
            monitor._read_transcript(transcript)
            with transcript.open("a", encoding="utf-8") as stream:
                stream.write(record({"type": "task_complete", "turn_id": "first"})
                             + record({"type": "task_started", "turn_id": "second"})
                             + record({"type": "task_complete", "turn_id": "second"}))
            spool = monitor.paths["incoming"] / "next-prompt.json"
            spool.write_text(json.dumps({
                "hook_event_name": "UserPromptSubmit", "session_id": session_id,
                "turn_id": "second", "transcript_path": str(transcript),
                "observed_at": "2026-08-23T00:00:01Z",
            }), encoding="utf-8")
            monitor._process_spool_file(spool)
            rows = [json.loads(line) for line in monitor.notifier.log_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([(row["turn_id"], row["state"]) for row in rows],
                             [("first", "completed"), ("second", "completed")])
            self.assertEqual(monitor.machine.active_runtimes(), [])

    def test_late_hook_cannot_reopen_terminal_evicted_during_large_backlog(self) -> None:
        for known_cursor in (False, True):
            for event_name in ("UserPromptSubmit", "Stop", "PermissionRequest"):
                with self.subTest(known_cursor=known_cursor, event=event_name), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    session_id = "88888888-8888-8888-8888-888888888888"
                    monitor = DesktopMonitor(MonitorConfig(codex_home=root / "codex", data_dir=root / "runtime"))
                    transcript = monitor.session_root / f"rollout-{session_id}.jsonl"
                    transcript.parent.mkdir(parents=True)
                    transcript.write_text(session_meta(session_id), encoding="utf-8")
                    if known_cursor:
                        monitor.tailer.register_at_end(transcript)
                    with transcript.open("a", encoding="utf-8") as stream:
                        for index in range(MAX_RETAINED_TERMINAL_TURNS + 1):
                            stream.write(record({"type": "task_started", "turn_id": str(index)})
                                         + record({"type": "task_complete", "turn_id": str(index)}))
                    spool = monitor.paths["incoming"] / "delayed-hook.json"
                    spool.write_text(json.dumps({
                        "hook_event_name": event_name, "session_id": session_id,
                        "turn_id": "0", "transcript_path": str(transcript),
                        "observed_at": "2026-08-23T00:00:01Z",
                    }), encoding="utf-8")
                    with patch.object(monitor, "_emit") as emit:
                        monitor._process_spool_file(spool)
                    completed = [note for call in emit.call_args_list for note in call.args[0]]
                    self.assertEqual(len(completed), MAX_RETAINED_TERMINAL_TURNS + 1)
                    self.assertNotIn((session_id, "0"), monitor.machine.turns)
                    self.assertEqual(monitor.machine.active_runtimes(), [])

    def test_transcript_cursor_is_replayed_when_apply_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex_home = root / ".codex"
            transcript = codex_home / "sessions" / "rollout-2026-08-23T00-00-00-55555555-5555-5555-5555-555555555555.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text(
                session_meta("55555555-5555-5555-5555-555555555555")
                + record({"type": "task_started", "turn_id": "t"}),
                encoding="utf-8",
            )
            monitor = DesktopMonitor(MonitorConfig(codex_home=codex_home, data_dir=root / "runtime"))
            monitor.tailer.register_from_start(transcript)
            original = monitor._apply
            with patch.object(monitor, '_apply', side_effect=RuntimeError('state write failed')):
                with self.assertRaises(RuntimeError):
                    monitor._read_transcript(transcript)
            self.assertEqual(monitor.tailer.cursor_for(transcript).offset, 0)
            original([])
            monitor._read_transcript(transcript)
            self.assertIn(("55555555-5555-5555-5555-555555555555", "t"), monitor.machine.turns)

    def test_starting_monitor_retires_active_turn_after_desktop_was_already_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            monitor = DesktopMonitor(MonitorConfig(
                codex_home=root / '.codex', data_dir=root / 'runtime', process_exit_grace_seconds=10.0,
            ))
            monitor.machine.apply(Signal(SignalKind.TURN_STARTED, session_id='s', turn_id='t', source='test'))
            missing = ProcessStatus(desktop_alive=False, app_server_alive=False)
            with patch('monitor.get_codex_desktop_process_status', return_value=missing), patch(
                    'monitor.time.monotonic', side_effect=[100.0, 111.0, 111.0]):
                monitor._check_processes()
                self.assertFalse(monitor.machine.turns[('s', 't')].terminal)
                monitor._check_processes()
            self.assertTrue(monitor.machine.turns[('s', 't')].terminal)
            self.assertEqual(monitor.machine.active_runtimes(), [])
    def test_unknown_old_rollout_is_not_replayed_but_new_append_is_seen(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex_home = root / ".codex"
            data_dir = root / "runtime"
            session_id = "22222222-2222-2222-2222-222222222222"
            transcript = codex_home / "sessions" / f"rollout-2026-08-22T00-00-00-{session_id}.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text(
                session_meta(session_id)
                + record({"type": "task_started", "turn_id": "old"})
                + record({"type": "task_complete", "turn_id": "old", "last_agent_message": "historical"}),
                encoding="utf-8",
            )
            monitor = DesktopMonitor(MonitorConfig(codex_home=codex_home, data_dir=data_dir))
            change = DirectoryChange(monitor.session_root, transcript, "modified")
            monitor._handle_change(change)

            self.assertFalse(monitor.notifier.log_path.exists())
            self.assertNotIn((session_id, "old"), monitor.machine.turns)

            now = datetime.now(timezone.utc).isoformat()
            with transcript.open("a", encoding="utf-8") as stream:
                stream.write(record({"type": "task_started", "turn_id": "current"}, timestamp=now))
                stream.write(
                    record(
                        {"type": "task_complete", "turn_id": "current", "last_agent_message": "new"},
                        timestamp=now,
                    )
                )
            monitor._handle_change(change)

            rows = [json.loads(line) for line in monitor.notifier.log_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([(row["turn_id"], row["state"]) for row in rows], [("current", "completed")])

    def test_fast_completed_turn_is_recovered_without_replaying_old_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex_home = root / ".codex"
            data_dir = root / "runtime"
            session_id = "33333333-3333-3333-3333-333333333333"
            transcript = codex_home / "sessions" / f"rollout-2026-08-23T00-00-00-{session_id}.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text(
                session_meta(session_id)
                + record({"type": "task_started", "turn_id": "old"})
                + record({"type": "task_complete", "turn_id": "old", "last_agent_message": "old"})
                + record({"type": "task_started", "turn_id": "current"})
                + record({"type": "task_complete", "turn_id": "current", "last_agent_message": "done"}),
                encoding="utf-8",
            )
            monitor = DesktopMonitor(MonitorConfig(codex_home=codex_home, data_dir=data_dir))
            event = {
                "format_version": 1,
                "event_id": "event",
                "observed_at": "2026-08-23T00:00:01+00:00",
                "hook_event_name": "UserPromptSubmit",
                "session_id": session_id,
                "turn_id": "current",
                "transcript_path": str(transcript),
                "project_name": "project",
            }
            spool = monitor.paths["incoming"] / "event.json"
            spool.write_text(json.dumps(event), encoding="utf-8")
            monitor._process_spool_file(spool)

            rows = [json.loads(line) for line in monitor.notifier.log_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["turn_id"], "current")
            self.assertEqual(rows[0]["state"], "completed")
            self.assertNotIn((session_id, "old"), monitor.machine.turns)

    def test_cli_rollout_and_its_hook_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex_home = root / ".codex"
            data_dir = root / "runtime"
            session_id = "44444444-4444-4444-4444-444444444444"
            transcript = codex_home / "sessions" / f"rollout-2026-08-23T00-00-00-{session_id}.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text(
                session_meta(session_id, source="exec")
                + record({"type": "task_started", "turn_id": "cli-turn"})
                + record({"type": "task_complete", "turn_id": "cli-turn", "last_agent_message": "done"}),
                encoding="utf-8",
            )
            monitor = DesktopMonitor(MonitorConfig(codex_home=codex_home, data_dir=data_dir))
            monitor.tailer.register_from_start(transcript)
            monitor._read_transcript(transcript)
            event = {
                "format_version": 1,
                "event_id": "cli-event",
                "observed_at": "2026-08-23T00:00:01+00:00",
                "hook_event_name": "UserPromptSubmit",
                "session_id": session_id,
                "turn_id": "cli-turn",
                "transcript_path": str(transcript),
                "project_name": "project",
            }
            spool = monitor.paths["incoming"] / "cli-event.json"
            spool.write_text(json.dumps(event), encoding="utf-8")
            monitor._process_spool_file(spool)

            self.assertFalse(monitor.machine.turns)
            self.assertFalse(monitor.notifier.log_path.exists())
            self.assertTrue((monitor.paths["processed"] / spool.name).exists())

    def test_profile_switch_maintenance_marker_is_time_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            monitor = DesktopMonitor(MonitorConfig(codex_home=root / ".codex", data_dir=root / "runtime"))
            marker = monitor.paths["root"] / "maintenance.json"
            marker.write_text(
                json.dumps({"expires_at": (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()}),
                encoding="utf-8",
            )
            monitor._sync_maintenance_marker()
            self.assertTrue(monitor.machine.maintenance)
            marker.write_text(
                json.dumps({"expires_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()}),
                encoding="utf-8",
            )
            monitor._sync_maintenance_marker()
            self.assertFalse(monitor.machine.maintenance)

    def test_app_server_restart_does_not_report_desktop_crash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            monitor = DesktopMonitor(MonitorConfig(codex_home=root / ".codex", data_dir=root / "runtime"))
            monitor.machine.apply(
                Signal(SignalKind.TURN_STARTED, session_id="s", turn_id="t", source="test")
            )
            monitor.last_process_status = ProcessStatus(desktop_alive=True, app_server_alive=True)
            with patch("monitor.get_codex_desktop_process_status", return_value=ProcessStatus(True, False)), patch(
                "monitor.time.monotonic", return_value=100.0
            ):
                monitor._check_processes()

            self.assertIsNone(monitor.process_missing_since)
            self.assertFalse(monitor.notifier.log_path.exists())
            self.assertFalse(monitor.machine.turns[("s", "t")].terminal)

    def test_desktop_exit_is_silent_and_clears_current_turn_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            monitor = DesktopMonitor(
                MonitorConfig(
                    codex_home=root / ".codex",
                    data_dir=root / "runtime",
                    process_exit_grace_seconds=10.0,
                )
            )
            monitor.machine.apply(
                Signal(SignalKind.TURN_STARTED, session_id="s", turn_id="old", source="test")
            )
            monitor.machine.apply(
                Signal(SignalKind.TURN_STARTED, session_id="s", turn_id="current", source="test")
            )
            monitor.last_process_status = ProcessStatus(desktop_alive=True, app_server_alive=True)
            missing = ProcessStatus(desktop_alive=False, app_server_alive=False)
            with patch("monitor.get_codex_desktop_process_status", return_value=missing), patch(
                "monitor.time.monotonic", side_effect=[100.0, 111.0, 111.0]
            ):
                monitor._check_processes()
                monitor._check_processes()

            self.assertFalse(monitor.notifier.log_path.exists())
            self.assertTrue(monitor.machine.turns[("s", "old")].terminal)
            self.assertTrue(monitor.machine.turns[("s", "current")].terminal)
            self.assertEqual(monitor.machine.active_runtimes(), [])
            self.assertEqual(monitor.control_status['s'], {'turn_id': 'current', 'state': 'disconnected'})
            self.assertFalse(monitor.exit_requested)


class SpoolRetentionTests(unittest.TestCase):
    def _monitor(self, root: Path) -> DesktopMonitor:
        return DesktopMonitor(MonitorConfig(codex_home=root / "codex", data_dir=root / "runtime"))

    def _write_event(self, monitor: DesktopMonitor, *, event_id: str, observed_at: datetime,
                     transcript: Path, body: str = "private payload") -> Path:
        path = monitor.paths["incoming"] / f"{event_id}.json"
        path.write_text(json.dumps({
            "event_id": event_id,
            "observed_at": observed_at.isoformat(),
            "hook_event_name": "Stop",
            "session_id": "session-test",
            "turn_id": "turn-test",
            "transcript_path": str(transcript),
            "assistant_text": body,
        }), encoding="utf-8")
        return path

    def test_recent_missing_transcript_stays_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            monitor = self._monitor(root)
            path = self._write_event(monitor, event_id="recent", observed_at=datetime.now(timezone.utc),
                                     transcript=root / "not-yet-created.jsonl")

            monitor._process_spool_file(path)

            self.assertTrue(path.exists())
            self.assertFalse((monitor.paths["root"] / "spool" / "orphaned.json").exists())
            self.assertFalse(list(monitor.paths["failed"].glob("*.json")))

    def test_expired_definitely_missing_transcript_is_archived_without_payload_or_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            monitor = self._monitor(root)
            transcript = root / "private" / "conversation.jsonl"
            path = self._write_event(monitor, event_id="expired", observed_at=datetime.now(timezone.utc) - timedelta(days=8),
                                     transcript=transcript, body="do not retain this prompt")

            monitor._process_spool_file(path)

            self.assertFalse(path.exists())
            index_path = monitor.paths["root"] / "spool" / "orphaned.json"
            index = json.loads(index_path.read_text(encoding="utf-8"))
            self.assertEqual(index["version"], 1)
            self.assertEqual(len(index["records"]), 1)
            row = index["records"][0]
            self.assertEqual(row["reason"], "transcript_missing_after_grace_period")
            self.assertNotIn("do not retain this prompt", index_path.read_text(encoding="utf-8"))
            self.assertNotIn(str(transcript), index_path.read_text(encoding="utf-8"))
            self.assertNotIn("assistant_text", row)

    def test_transient_transcript_permission_error_does_not_archive_or_fail_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            monitor = self._monitor(root)
            path = self._write_event(monitor, event_id="locked", observed_at=datetime.now(timezone.utc) - timedelta(days=30),
                                     transcript=root / "locked.jsonl")
            with patch("monitor.is_desktop_rollout", side_effect=PermissionError("locked")):
                monitor._process_spool_file(path)

            self.assertTrue(path.exists())
            self.assertFalse(list(monitor.paths["failed"].glob("*.json")))
            self.assertFalse((monitor.paths["root"] / "spool" / "orphaned.json").exists())

    def test_cleanup_runs_at_startup_and_applies_age_and_count_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            monitor = self._monitor(root)
            now = time.time()
            for name, age in (("old.json", 10 * 86400), ("new-a.json", 1),
                              ("new-b.json", 2), ("new-c.json", 3)):
                path = monitor.paths["processed"] / name
                path.write_text("{}", encoding="utf-8")
                os.utime(path, (now - age, now - age))
            expired_failure = monitor.paths["failed"] / "old-failure.json"
            expired_failure.write_text("{}", encoding="utf-8")
            os.utime(expired_failure, (now - 31 * 86400, now - 31 * 86400))

            with patch("monitor.MAX_PROCESSED_SPOOL_FILES", 2), patch("monitor.time.monotonic", return_value=100):
                monitor._cleanup_spool_retention()

            names = {path.name for path in monitor.paths["processed"].glob("*.json")}
            self.assertEqual(names, {"new-a.json", "new-b.json"})
            self.assertFalse(expired_failure.exists())
            self.assertEqual(monitor.last_spool_cleanup, 100)

    def test_cleanup_interval_prevents_repeated_directory_scans(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            monitor = self._monitor(root)
            monitor.last_spool_cleanup = 100
            with patch("monitor.time.monotonic", return_value=101), patch.object(
                    monitor, "_prune_spool_directory") as prune:
                monitor._cleanup_spool_retention()
            prune.assert_not_called()

    def test_cleanup_bounds_outbox_and_rotates_large_logs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            monitor = self._monitor(root)
            now = time.time()
            for name in ("sent", "failed", "obsolete"):
                folder = monitor.paths["root"] / "outbox" / name
                folder.mkdir(parents=True, exist_ok=True)
                old = folder / "old.json"
                old.write_text("{}", encoding="utf-8")
                os.utime(old, (now - 31 * 86400, now - 31 * 86400))
            shadow = monitor.paths["logs"] / "shadow-notifications.jsonl"
            shadow.write_text("x" * (8 * 1024 * 1024 + 1), encoding="utf-8")

            monitor._cleanup_spool_retention()

            self.assertFalse((monitor.paths["root"] / "outbox" / "sent" / "old.json").exists())
            self.assertFalse((monitor.paths["root"] / "outbox" / "failed" / "old.json").exists())
            self.assertFalse((monitor.paths["root"] / "outbox" / "obsolete" / "old.json").exists())
            self.assertTrue(shadow.exists())
            rotated = list(monitor.paths["logs"].glob("shadow-notifications-*.jsonl"))
            self.assertEqual(len(rotated), 1)
            self.assertEqual(shadow.stat().st_size, 0)

    def test_spool_archive_lock_keeps_event_retryable_and_does_not_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            monitor = self._monitor(Path(temporary))
            path = monitor.paths['incoming'] / 'bad.json'
            path.write_text('{bad json', encoding='utf-8')
            with patch('monitor.os.replace', side_effect=PermissionError('locked')):
                monitor._process_spool_file(path)
                monitor._process_all_spool()
            self.assertTrue(path.exists())
            self.assertFalse(list(monitor.paths['failed'].glob('*.json')))

    def test_external_append_logs_are_bounded_without_touching_pending_outbox(self):
        with tempfile.TemporaryDirectory() as temporary:
            monitor = self._monitor(Path(temporary))
            pending = monitor.paths['outbox'] / 'pending' / 'keep.json'
            pending.write_text('{"keep":true}', encoding='utf-8')
            logs = [monitor.paths['logs'] / name for name in
                    ('cc-connect.log', 'monitor-console.log', 'autostart-console.log')]
            for path in logs:
                path.write_bytes(b'x' * 300)
            with patch('monitor.MAX_ACTIVE_LOG_BYTES', 128):
                for path in logs:
                    monitor._bound_open_log(path)
            self.assertTrue(all(path.stat().st_size <= 64 for path in logs))
            self.assertEqual(pending.read_text(encoding='utf-8'), '{"keep":true}')


if __name__ == "__main__":
    unittest.main()
