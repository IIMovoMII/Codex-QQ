from __future__ import annotations

import sys
import json
import io
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch


PACKAGE_DIR = Path(__file__).resolve().parents[1] / "src" / "codex_desktop_weixin"
sys.path.insert(0, str(PACKAGE_DIR))

from models import Confidence, Notification, TurnState
from notifier import CCConnectSender, DeliverySettings, NotificationDispatcher, format_notification, save_delivery_settings, is_local_api_delivery_error


class FakeSender:
    def __init__(self, success: bool = True) -> None:
        self.success = success
        self.messages: list[str] = []

    def send(self, message: str) -> tuple[bool, str]:
        self.messages.append(message)
        return self.success, "ok" if self.success else "offline"


class RecoveringSender:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, message: str) -> tuple[bool, str]:
        self.messages.append(message)
        if len(self.messages) == 1:
            return False, "failed to connect: dial unix api.sock: invalid argument"
        return True, "ok"


class NotificationDispatcherTests(unittest.TestCase):
    def note(self) -> Notification:
        return Notification(
            session_id="s",
            turn_id="t",
            state=TurnState.COMPLETED,
            confidence=Confidence.CERTAIN,
            title="【项目】Codex 任务已完成",
            message="完成内容",
            model="gpt-test",
            dedupe_key="s:t:completed",
        )

    def test_shadow_mode_never_calls_sender(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            sender = FakeSender()
            dispatcher = NotificationDispatcher(Path(temporary), sender=sender)
            dispatcher.emit(self.note())
            dispatcher.flush()
            self.assertEqual(sender.messages, [])
            self.assertTrue(dispatcher.log_path.exists())

    def test_corrupt_settings_are_visible_errors_not_silent_shadow(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = root / 'settings.json'
            settings.write_text('{broken', encoding='utf-8')
            with self.assertRaises(ValueError):
                DeliverySettings.load(settings)
            settings.write_text(json.dumps({'mode': 'mystery'}), encoding='utf-8')
            with self.assertRaises(ValueError):
                DeliverySettings.load(settings)

    def test_doctor_reports_invalid_delivery_configuration_as_unhealthy(self):
        import cli
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'settings.json').write_text('{broken', encoding='utf-8')
            output = io.StringIO()
            with patch('cli.hook_install_status', return_value={}), patch('sys.stdout', output):
                result = cli.command_doctor(root / '.codex', root)
            self.assertEqual(result, 1)
            self.assertEqual(json.loads(output.getvalue())['delivery_mode'], 'invalid')

    def test_successful_delivery_is_not_resent_if_sent_archive_move_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            save_delivery_settings(root / 'settings.json', DeliverySettings(mode='cc-connect'))
            sender = FakeSender()
            dispatcher = NotificationDispatcher(root, sender=sender)
            note = self.note(); dispatcher.emit(note)
            original_replace = __import__('notifier').os.replace
            def fail_sent(source, destination):
                if Path(destination).parent == dispatcher.sent:
                    raise PermissionError('locked archive')
                return original_replace(source, destination)
            with patch('notifier.os.replace', side_effect=fail_sent):
                self.assertEqual(dispatcher.flush(), 1)
            pending = list(dispatcher.pending.glob('*.json'))
            self.assertEqual(len(sender.messages), 1)
            self.assertTrue((dispatcher.sent_state / pending[0].name).exists())
            restarted_sender = FakeSender()
            restarted = NotificationDispatcher(root, sender=restarted_sender)
            self.assertEqual(restarted.flush(), 0)
            self.assertEqual(restarted_sender.messages, [])
            self.assertEqual(list(restarted.sent.glob('*.json'))[0].name, pending[0].name)

    def test_pending_notices_flush_in_created_time_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            save_delivery_settings(root / 'settings.json', DeliverySettings(mode='cc-connect'))
            sender = FakeSender(); dispatcher = NotificationDispatcher(root, sender=sender)
            now = self.note().created_at
            notes = [replace(self.note(), turn_id=f't{i}', title=f'notice-{i}',
                             dedupe_key=f'event-{i}', created_at=now - timedelta(seconds=5 - i))
                     for i in range(6)]
            for note in reversed(notes):
                dispatcher.emit(note)
            self.assertEqual(dispatcher.flush(max_items=10), 6)
            self.assertEqual([next(f'notice-{i}' for i in range(6) if f'notice-{i}' in message)
                              for message in sender.messages], [f'notice-{i}' for i in range(6)])

    def test_process_exit_notices_are_disabled_and_legacy_pending_is_retired(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            save_delivery_settings(root / 'settings.json', DeliverySettings(mode='cc-connect'))
            sender = FakeSender()
            dispatcher = NotificationDispatcher(root, sender=sender)
            crash = replace(self.note(), state=TurnState.CRASHED, dedupe_key='s:t:crashed')
            dispatcher.emit(crash)
            self.assertEqual(list(dispatcher.pending.glob('*.json')), [])
            self.assertFalse(dispatcher.log_path.exists())
            # Simulate a notice persisted by the old implementation.
            name = dispatcher._item_name(crash)
            (dispatcher.pending / name).write_text(json.dumps({
                'notification': crash.to_dict(), 'message': '不应发出的旧退出提醒',
                'attempts': 0, 'next_attempt_at': crash.created_at.isoformat()}), encoding='utf-8')
            dispatcher.emit(self.note())
            self.assertEqual(dispatcher.flush(max_items=10), 1)
            self.assertEqual(sender.messages, [format_notification(self.note())])
            self.assertFalse((dispatcher.pending / name).exists())
            retired = json.loads((root / 'outbox' / 'obsolete' / name).read_text(encoding='utf-8'))
            self.assertEqual(retired['reason'], 'process-exit-notifications-disabled')

    def test_notification_contains_full_thread_reference_for_qq_quote(self) -> None:
        rendered = format_notification(self.note())
        self.assertIn("对话编号：s", rendered)

    def test_long_notification_keeps_full_route_footer(self) -> None:
        note = replace(self.note(), message="完整中文😀" * 1000, session_id="00000000-0000-7000-8000-000000000001")
        rendered = format_notification(note)
        self.assertLessEqual(len(rendered), 2000)
        self.assertTrue(rendered.endswith("对话编号：" + note.session_id))

    def test_long_waiting_notice_retains_action_hint_and_route(self):
        note = replace(self.note(), state=TurnState.WAITING_INPUT, message='长问题😀' * 1000)
        rendered = format_notification(note)
        self.assertIn('发送“待办”', rendered)
        self.assertLessEqual(len(rendered), 2000)
        self.assertTrue(rendered.endswith('对话编号：s'))

    def test_redacted_missing_socket_error_still_allows_one_local_recovery(self):
        self.assertTrue(is_local_api_delivery_error('Error: cc-connect is not running (socket not found: <本地路径>'))
        self.assertFalse(is_local_api_delivery_error('qqbot api returned 400'))

    def test_cc_connect_sender_sends_plain_text_without_internal_marker(self) -> None:
        settings = DeliverySettings(mode="cc-connect", executable="cc-connect")
        sender = CCConnectSender(settings)
        completed = type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        with patch("notifier.subprocess.run", return_value=completed) as run:
            ok, _detail = sender.send("【测试】提醒")
        self.assertTrue(ok)
        self.assertEqual(run.call_args.kwargs["input"], "【测试】提醒")

    def test_cc_connect_mode_queues_delivers_and_deduplicates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            save_delivery_settings(root / "settings.json", DeliverySettings(mode="cc-connect"))
            sender = FakeSender()
            dispatcher = NotificationDispatcher(root, sender=sender)
            dispatcher.emit(self.note())
            dispatcher.emit(self.note())
            self.assertEqual(len(list(dispatcher.pending.glob("*.json"))), 1)
            self.assertEqual(dispatcher.flush(), 1)
            self.assertEqual(len(sender.messages), 1)
            self.assertIn("完成内容", sender.messages[0])
            self.assertEqual(len(list(dispatcher.sent.glob("*.json"))), 1)

    def test_second_waiting_request_in_same_turn_has_a_distinct_delivery_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            save_delivery_settings(root / 'settings.json', DeliverySettings(mode='cc-connect'))
            sender = FakeSender(); dispatcher = NotificationDispatcher(root, sender=sender)
            first = replace(self.note(), state=TurnState.WAITING_APPROVAL,
                            dedupe_key='s:t:waiting_approval:0')
            second = replace(first, dedupe_key='s:t:waiting_approval:1')
            dispatcher.emit(first); self.assertEqual(dispatcher.flush(), 1)
            dispatcher.emit(second); self.assertEqual(dispatcher.flush(), 1)
            dispatcher.emit(first)
            self.assertEqual(dispatcher.flush(), 0)
            self.assertEqual(len(sender.messages), 2)

    def test_same_event_key_is_not_resent_when_rendered_text_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            save_delivery_settings(root / "settings.json", DeliverySettings(mode="cc-connect"))
            sender = FakeSender()
            dispatcher = NotificationDispatcher(root, sender=sender)
            original = self.note()
            rerendered = Notification(
                session_id=original.session_id,
                turn_id=original.turn_id,
                state=original.state,
                confidence=original.confidence,
                title="【稍后解析出的标题】Codex 任务已完成",
                message="同一事件在重启后得到更完整的摘要",
                model=original.model,
                dedupe_key=original.dedupe_key,
            )

            dispatcher.emit(original)
            self.assertEqual(dispatcher.flush(), 1)
            dispatcher.emit(rerendered)
            self.assertEqual(dispatcher.flush(), 0)
            self.assertEqual(len(sender.messages), 1)
            self.assertEqual(len(list(dispatcher.sent.glob("*.json"))), 1)

    def test_local_api_failure_recovers_once_and_retries_same_message(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            save_delivery_settings(root / "settings.json", DeliverySettings(mode="cc-connect"))
            sender = RecoveringSender()
            dispatcher = NotificationDispatcher(root, sender=sender)
            dispatcher.emit(self.note())

            with patch("notifier.recover_local_cc_connect", return_value=(True, "restarted")) as recover:
                self.assertEqual(dispatcher.flush(), 1)

            recover.assert_called_once_with(root)
            self.assertEqual(len(sender.messages), 2)
            self.assertEqual(len(list(dispatcher.pending.glob("*.json"))), 0)
            self.assertEqual(len(list(dispatcher.sent.glob("*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
