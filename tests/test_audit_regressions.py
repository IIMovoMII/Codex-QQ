from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

PACKAGE = Path(__file__).resolve().parents[1] / 'src' / 'codex_desktop_weixin'
sys.path.insert(0, str(PACKAGE))

import cli
from delivery_ledger import DeliveryLedger
from models import Confidence, Notification, Signal, SignalKind, TurnState
from monitor import DesktopMonitor, MonitorConfig
from notifier import DeliverySettings, NotificationDispatcher, save_delivery_settings
from runtime_io import acquire_file_lock, atomic_write_json
from state_machine import StateMachine
from tailer import IncrementalTailer

BASE = datetime(2026, 10, 26, tzinfo=timezone.utc)
SESSION = '11111111-1111-4111-8111-111111111111'


def rollout_line(payload: dict, timestamp: str = '2026-10-26T00:00:00Z') -> str:
    return json.dumps({'timestamp': timestamp, 'type': 'event_msg', 'payload': payload}) + '\n'


def session_meta() -> str:
    return json.dumps({
        'timestamp': '2026-10-26T00:00:00Z',
        'type': 'session_meta',
        'payload': {'id': SESSION, 'source': 'vscode', 'cwd': 'C:/audit'},
    }) + '\n'


def signal(kind: SignalKind, seconds: int = 0, turn: str = 'new', **data) -> Signal:
    return Signal(kind, BASE + timedelta(seconds=seconds), SESSION, turn, data=data)


def notification(key: str = 'audit') -> Notification:
    return Notification(SESSION, key, TurnState.COMPLETED, Confidence.CERTAIN,
                        '测试完成', '隔离测试', dedupe_key=key)


class DeliveryRegressionTests(unittest.TestCase):
    def make_dispatcher(self, root: Path, sender: object) -> NotificationDispatcher:
        save_delivery_settings(root / 'settings.json', DeliverySettings(mode='cc-connect'))
        return NotificationDispatcher(root, sender=sender)

    def make_due(self, dispatcher: NotificationDispatcher) -> None:
        for path in dispatcher.pending.glob('*.json'):
            item = json.loads(path.read_text(encoding='utf-8'))
            item['next_attempt_at'] = datetime.now(timezone.utc).isoformat()
            atomic_write_json(path, item)

    def test_recovery_success_is_not_delivery_success_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sender = Mock()
            sender.send.side_effect = [
                (False, 'dial unix api.sock failed'),
                (False, 'QQ still unavailable'),
            ]
            dispatcher = self.make_dispatcher(root, sender)
            dispatcher.emit(notification())
            with patch('notifier.recover_local_cc_connect', return_value=(True, 'restarted')):
                self.assertEqual(dispatcher.flush(), 0)
            self.make_due(dispatcher)
            restarted_sender = Mock()
            restarted_sender.send.return_value = (True, 'ok')
            restarted = self.make_dispatcher(root, restarted_sender)
            self.assertEqual(restarted.flush(), 1)
            self.assertEqual(restarted_sender.send.call_count, 1)
            rows = [json.loads(row) for row in dispatcher.delivery_log.read_text(encoding='utf-8').splitlines()]
            recovery = next(row for row in rows if row.get('event') == 'recovery')
            self.assertNotIn('success', recovery)

    def test_legacy_recovery_row_does_not_create_delivery_tombstone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dispatcher = self.make_dispatcher(root, Mock())
            dispatcher.emit(notification())
            name = dispatcher._item_name(notification())
            with dispatcher.delivery_log.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps({'success': True, 'item': name,
                                         'recovery': 'cc_connect_local_api'}) + '\n')
            sender = Mock()
            restarted = self.make_dispatcher(root, sender)
            self.make_due(restarted)
            restarted.flush()
            sender.send.assert_called_once()

    def test_delivery_ledger_prevents_duplicate_when_archive_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sender = Mock()
            sender.send.return_value = (True, 'ok')
            dispatcher = self.make_dispatcher(root, sender)
            dispatcher.emit(notification())
            with patch.object(dispatcher, '_archive_sent', return_value=False):
                self.assertEqual(dispatcher.flush(), 1)
            self.assertEqual(len(list(dispatcher.pending.glob('*.json'))), 1)
            restarted_sender = Mock()
            restarted = self.make_dispatcher(root, restarted_sender)
            restarted.flush()
            restarted_sender.send.assert_not_called()

    def test_ledger_is_bounded_and_keeps_pending_fact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch('delivery_ledger.MAX_LEDGER_ROWS', 20):
            root = Path(tmp)
            pending = root / 'pending'
            pending.mkdir()
            ledger = DeliveryLedger(root / 'delivery.sqlite', pending=pending)
            (pending / 'keep.json').write_text('{}', encoding='utf-8')
            ledger.record('keep.json', delivered=True)
            for index in range(100):
                ledger.record(f'{index}.json')
            ledger.prune(pending)
            with ledger.connect() as db:
                self.assertLessEqual(db.execute('SELECT COUNT(*) FROM notices').fetchone()[0], 21)
            self.assertTrue(ledger.contains('keep.json', delivered=True))


class ReducerRegressionTests(unittest.TestCase):
    def test_late_start_and_session_end_cannot_retire_newer_turn(self) -> None:
        for late in (
            signal(SignalKind.TURN_STARTED, -10, 'old'),
            signal(SignalKind.SESSION_ENDED, -10, 'old'),
            signal(SignalKind.SESSION_ENDED, 10, 'old'),
        ):
            with self.subTest(kind=late.kind):
                machine = StateMachine()
                machine.apply(signal(SignalKind.TURN_STARTED, 10, 'new'))
                machine.apply(late)
                self.assertEqual(machine.active_turn_by_session.get(SESSION), 'new')
                self.assertEqual(machine.apply(signal(SignalKind.TURN_COMPLETED, 20, 'new'))[0].state,
                                 TurnState.COMPLETED)

    def test_checkpoint_restores_error_and_waiting_dedupe(self) -> None:
        machine = StateMachine()
        machine.apply(signal(SignalKind.TURN_STARTED))
        machine.apply(signal(SignalKind.ERROR_OBSERVED, 1, error={'message': 'HTTP 401 Unauthorized'}))
        checkpoint = json.loads(json.dumps(machine.checkpoint()))
        recovered = StateMachine.from_checkpoint(checkpoint)
        self.assertEqual(recovered.apply(signal(SignalKind.TURN_COMPLETED, 2))[0].state, TurnState.FAILED)

        waiting = StateMachine()
        waiting.apply(signal(SignalKind.TURN_STARTED))
        first = waiting.apply(signal(SignalKind.USER_INPUT_REQUESTED, 1))[0]
        waiting_again = StateMachine.from_checkpoint(json.loads(json.dumps(waiting.checkpoint())))
        self.assertEqual(waiting_again.apply(signal(SignalKind.USER_INPUT_REQUESTED, 1)), [])
        self.assertEqual(first.dedupe_key,
                         next(iter(waiting_again.turns.values())).session_id + ':new:waiting_input:' +
                         (BASE + timedelta(seconds=1)).isoformat())

    def test_corrupt_checkpoint_is_rejected_without_rewriting_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / 'data' / 'state' / 'cursors.json'
            state.parent.mkdir(parents=True)
            original = json.dumps({'version': 2, 'cursors': {}, 'reducer': {'version': 99}})
            state.write_text(original, encoding='utf-8')
            with self.assertRaises(ValueError):
                DesktopMonitor(MonitorConfig(root / 'codex', root / 'data'))
            self.assertEqual(state.read_text(encoding='utf-8'), original)

    def test_poison_json_record_advances_and_marks_result_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / 'rollout.jsonl'
            poison = json.dumps({'type': 'response_item', 'payload': {
                'type': 'message', 'role': 'assistant', 'content': 1,
            }}) + '\n'
            transcript.write_text(
                session_meta()
                + rollout_line({'type': 'task_started', 'turn_id': 't'})
                + poison
                + rollout_line({'type': 'task_complete', 'turn_id': 't'})
                + rollout_line({'type': 'task_started', 'turn_id': 'next'})
                + rollout_line({'type': 'task_complete', 'turn_id': 'next'}),
                encoding='utf-8',
            )
            monitor = DesktopMonitor(MonitorConfig(root / 'codex', root / 'data'))
            with patch('sys.stdout', io.StringIO()):
                monitor._read_transcript(transcript)
            self.assertEqual(monitor.machine.turns[(SESSION, 't')].state, TurnState.RESULT_UNKNOWN)
            self.assertEqual(monitor.machine.turns[(SESSION, 'next')].state, TurnState.COMPLETED)
            self.assertEqual(monitor.tailer.cursor_for(transcript).offset, transcript.stat().st_size)
            diagnostic = (root / 'data' / 'logs' / 'tailer-errors.jsonl').read_text(encoding='utf-8')
            self.assertNotIn('content', diagnostic)
            self.assertNotIn(str(transcript), diagnostic)

    def test_cursor_checkpoint_write_failure_replays_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / 'rollout.jsonl'
            transcript.write_text(session_meta() + rollout_line({'type': 'task_started', 'turn_id': 't'})
                                  + rollout_line({'type': 'task_complete', 'turn_id': 't'}), encoding='utf-8')
            monitor = DesktopMonitor(MonitorConfig(root / 'codex', root / 'data'))
            monitor.tailer.register_from_start(transcript)
            with patch('tailer.atomic_write_json', side_effect=OSError('disk')), patch('sys.stdout', io.StringIO()):
                with self.assertRaises(OSError):
                    monitor._read_transcript(transcript)
            self.assertEqual(monitor.tailer.cursor_for(transcript).offset, 0)
            self.assertEqual(monitor.machine.turns, {})


class WorkerRegressionTests(unittest.TestCase):
    def test_cli_rejects_second_worker_without_write_pid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock = acquire_file_lock(root / 'data' / 'state' / 'app.lock')
            self.assertIsNotNone(lock)
            try:
                with patch('cli.DesktopMonitor') as monitor, patch('sys.stdout', io.StringIO()):
                    result = cli.command_monitor(root / 'codex', root / 'data', 0,
                        exit_with_desktop=False, write_pid=False, manage_cc_connect=False)
                self.assertEqual(result, 3)
                monitor.assert_not_called()
            finally:
                os.close(lock[0])

    def test_cli_lock_is_released_when_pid_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch('cli.read_monitor_pid', return_value=None), patch('cli.write_monitor_pid',
                                                                         side_effect=OSError('disk')):
                with self.assertRaises(OSError):
                    cli.command_monitor(root / 'codex', root / 'data', 0,
                        exit_with_desktop=False, write_pid=True, manage_cc_connect=False)
            lock = acquire_file_lock(root / 'data' / 'state' / 'app.lock')
            self.assertIsNotNone(lock)
            os.close(lock[0])


if __name__ == '__main__':
    unittest.main()
