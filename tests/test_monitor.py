from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


PACKAGE_DIR = Path(__file__).resolve().parents[1] / "src" / "codex_desktop_weixin"
sys.path.insert(0, str(PACKAGE_DIR))

from monitor import DesktopMonitor, MonitorConfig
from models import Signal, SignalKind
from process_watch import ProcessStatus
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


if __name__ == "__main__":
    unittest.main()
