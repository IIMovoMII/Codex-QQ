from __future__ import annotations

import json
import sys
import unittest
import copy
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src' / 'codex_desktop_weixin'))
from models import SignalKind
from rollout import parse_rollout_line
from async_questions import pending_batches, parse_answers, parse_call, notification
from models import Signal, TurnState
from notifier import NotificationDispatcher, DeliverySettings, format_notification
from monitor import DesktopMonitor, MonitorConfig
from tailer import IncrementalTailer


class AsyncQuestionReproTests(unittest.TestCase):
    def parse(self, payload, outer='response_item'):
        return parse_rollout_line(json.dumps({'timestamp': '2026-10-13T01:02:03Z',
                                 'type': outer, 'payload': payload}), session_id='session')

    def test_real_async_name_is_a_question_not_only_an_ordinary_tool(self):
        signals = self.parse({'type': 'function_call', 'name': 'request_user_input_async',
                              'call_id': 'call_test', 'arguments': json.dumps({'questions': [
                              {'title': '是否采用本机规则？', 'options': ['接受', '先不要']} ]})})
        self.assertIn('async_question_created', [s.kind.value for s in signals])

    def test_immediate_tool_output_does_not_resolve_a_question(self):
        signals = self.parse({'type': 'function_call_output', 'call_id': 'call_test',
                              'output': 'Questions shown; the user will answer asynchronously.'})
        self.assertNotIn('async_question_answered', [s.kind.value for s in signals])

    def test_user_native_answer_is_identified_by_original_question_id(self):
        text = answer_text()
        signals = self.parse({'type': 'message', 'role': 'user',
                              'content': [{'type': 'input_text', 'text': text}]})
        self.assertIn('async_question_answered', [s.kind.value for s in signals])
        signals = self.parse({'type': 'message', 'role': 'assistant',
                              'content': [{'type': 'output_text', 'text': text}]})
        self.assertNotIn('async_question_answered', [s.kind.value for s in signals])


def answer_text(call='call_test', index=0):
    return '<send_user_message_question_reply>\n' + json.dumps([
        {'questionItemId': json.dumps(['request_user_input_async', call, index]),
         'question': '题目', 'answer': '不应保存的答案'}]) + '\n</send_user_message_question_reply>'


def native_turn():
    return {'turnId': 'current-turn', 'status': 'inProgress', 'items': [
        {'type': 'agentMessage', 'id': 'call_test', 'delivery': 'async', 'questions': [
         {'title': '题目一', 'options': ['接受', '先不要']}, {'title': '题目二', 'options': []}]}]}


class ShapeTests(unittest.TestCase):
    def test_question_field_is_preserved_and_rendered_without_accepting_unknown_fields(self):
        parsed = parse_call({'call_id': 'call_test', 'arguments': json.dumps({'questions': [
            {'title': '标题', 'question': '真实题目正文？', 'options': ['甲', '乙']} ]})})
        self.assertEqual(parsed['questions'][0]['question'], '真实题目正文？')
        self.assertIn('真实题目正文？', notification(
            Signal(SignalKind.ASYNC_QUESTION_CREATED, session_id='session',
                   data={'call_id': 'call_test', **parsed}), turn_id='turn').message)
        rejected = parse_call({'call_id': 'call_test', 'arguments': json.dumps({'questions': [
            {'title': '标题', 'question': '正文', 'options': [], 'secret': '不要转发'}]})})
        self.assertEqual(rejected['questions'], [])
        self.assertNotIn('不要转发', str(rejected))

    def test_pending_native_questions_use_exact_ids_and_partial_native_answers(self):
        turn = native_turn()
        self.assertEqual(pending_batches(turn)[0]['indices'], [0, 1])
        turn['items'].append({'type': 'userMessage', 'content': [{'type': 'text', 'text': answer_text()}]})
        self.assertEqual(pending_batches(turn)[0]['indices'], [1])
        turn['items'].append({'type': 'steeringUserMessage', 'status': 'pending',
                              'input': [{'type': 'text', 'text': answer_text(index=1)}]})
        self.assertEqual(pending_batches(turn)[0]['indices'], [1])
        turn['items'][-1]['status'] = 'accepted'
        self.assertEqual(pending_batches(turn), [])

    def test_closed_turn_unknown_shape_duplicates_and_secrets_are_not_actionable(self):
        for status in ('completed', 'failed', 'interrupted'):
            turn = native_turn(); turn['status'] = status
            self.assertEqual(pending_batches(turn), [])
        turn = native_turn(); turn['items'].append(copy.deepcopy(turn['items'][0]))
        with self.assertRaises(ValueError):
            pending_batches(turn)
        turn = native_turn(); turn['items'][0]['questions'][0]['isSecret'] = True
        with self.assertRaises(ValueError):
            pending_batches(turn)
        call = parse_call({'call_id': 'call_test', 'arguments': json.dumps({'questions': [
                           {'title': '不要转发此秘密', 'options': [], 'isSecret': True}]})})
        self.assertEqual(call['questions'], [])
        self.assertNotIn('不要转发', str(call))

    def test_malformed_answer_identity_or_quoted_text_does_not_resolve(self):
        for text in ('引用的旧文：' + answer_text(), answer_text() + '\n尾部其他内容',
                     answer_text().replace('call_test', '../wrong'),
                     answer_text().replace(', 0]', ', true]'),
                     answer_text().replace('request_user_input_async', 'another-tool')):
            self.assertEqual(parse_answers(text), [])
        self.assertEqual(parse_answers(answer_text()), [{'call_id': 'call_test', 'index': 0}])
        self.assertNotIn('不应保存', str(parse_answers(answer_text())))

    def test_backend_view_only_retains_current_pending_questions(self):
        from control_backend import NativeBackend
        state = {'id': 's', 'turns': [native_turn()]}
        self.assertEqual(NativeBackend._view(state)['async_questions'][0]['message_id'], 'call_test')
        state['turns'][0]['items'][0]['questions'] = []
        result = NativeBackend._view(state)
        self.assertTrue(result['async_questions_unverified'])
        self.assertEqual(result['async_questions'], [])


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dispatcher = NotificationDispatcher(self.root)
        self.dispatcher.settings = DeliverySettings(mode='cc-connect')
        self.signal = Signal(SignalKind.ASYNC_QUESTION_CREATED, session_id='session', turn_id='turn',
                             data={'call_id': 'call_test', 'questions': native_turn()['items'][0]['questions'], 'question_count': 2})
        self.note = notification(self.signal, turn_id='turn')

    def test_partial_answers_remain_queued_all_answers_retire_exact_notice(self):
        self.dispatcher.emit(self.note)
        self.assertEqual(self.dispatcher.resolve_async_answers('other-session', [{'call_id': 'call_test', 'index': 0}]), 0)
        self.assertEqual(self.dispatcher.resolve_async_answers('session', [{'call_id': 'call_test', 'index': 0}]), 0)
        self.assertEqual(len(list(self.dispatcher.pending.glob('*.json'))), 1)
        self.assertEqual(self.dispatcher.resolve_async_answers('session', [{'call_id': 'call_test', 'index': 1}]), 1)
        self.assertEqual(list(self.dispatcher.pending.glob('*.json')), [])
        self.assertEqual(len(list((self.root / 'outbox' / 'obsolete').glob('*.json'))), 1)
        self.dispatcher.emit(self.note)
        self.assertEqual(list(self.dispatcher.pending.glob('*.json')), [])
        tombstone = next((self.root / 'outbox' / 'obsolete').glob('*.json')).read_text(encoding='utf-8')
        self.assertNotIn('题目', tombstone)
        self.assertLess(len(tombstone), 500)

    def test_native_end_retires_only_matching_questions_and_preserves_completion(self):
        from dataclasses import replace
        self.dispatcher.emit(self.note)
        self.dispatcher.emit(replace(self.note, turn_id='other-turn', question_call_id='another-call',
                                     dedupe_key='another-event'))
        self.dispatcher.emit(replace(self.note, state=TurnState.COMPLETED, dedupe_key='completion',
                                     question_call_id='', question_count=0))
        self.assertEqual(self.dispatcher.close_async_turn('session', 'turn'), 1)
        rows = [json.loads(p.read_text(encoding='utf-8'))['notification'] for p in self.dispatcher.pending.glob('*.json')]
        self.assertEqual(len(rows), 2)
        self.assertTrue(any(x['state'] == 'completed' for x in rows))
        self.assertTrue(any(x['turn_id'] == 'other-turn' for x in rows))

    def test_obsolete_tombstone_prevents_send_after_deletion_crash(self):
        from unittest.mock import Mock
        self.dispatcher.emit(self.note)
        path = next(self.dispatcher.pending.glob('*.json'))
        obsolete = self.root / 'outbox' / 'obsolete'; obsolete.mkdir()
        (obsolete / path.name).write_text('{}', encoding='utf-8')
        self.dispatcher.sender = Mock()
        self.assertEqual(self.dispatcher.flush(), 0)
        self.dispatcher.sender.send.assert_not_called()
        self.assertFalse(path.exists())

    def test_async_notice_has_separate_route_and_chinese_body_without_false_pause(self):
        text = format_notification(self.note)
        self.assertIn('异步问题对话：session', text)
        self.assertTrue(text.endswith('提问编号：call_test'))
        self.assertNotIn('\n对话编号：', text)
        self.assertIn('不是暂停通知', text)
        self.assertNotIn('等待输入', text)
        from dataclasses import replace
        long = format_notification(replace(self.note, message='中文😀' * 2000))
        self.assertLessEqual(len(long), 2000)
        self.assertTrue(long.endswith('提问编号：call_test'))


class IncrementalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'rollout-00000000-0000-4000-8000-000000000001.jsonl'
        self.monitor = DesktopMonitor(MonitorConfig(codex_home=self.root / 'codex', data_dir=self.root / 'runtime'))
        self.monitor.notifier.settings = DeliverySettings(mode='cc-connect')

    def record(self, payload, outer='response_item', at=None):
        return json.dumps({'timestamp': (at or datetime.now(timezone.utc)).isoformat(),
                           'type': outer, 'payload': payload}) + '\n'

    def call(self, call='call_test'):
        return {'type': 'function_call', 'name': 'request_user_input_async', 'call_id': call,
                'arguments': json.dumps({'questions': native_turn()['items'][0]['questions']})}

    def test_cursor_restores_turn_for_response_items_and_target_filter_after_reload(self):
        self.path.write_text(self.record({'type': 'task_started', 'turn_id': 'turn'}, 'event_msg'), encoding='utf-8')
        tailer = IncrementalTailer(self.root / 'cursors.json')
        tailer.read_new_signals(self.path)
        tailer = IncrementalTailer(self.root / 'cursors.json')
        with self.path.open('a', encoding='utf-8') as stream:
            stream.write(self.record(self.call()))
        signals = tailer.read_new_signals(self.path)
        self.assertEqual({s.turn_id for s in signals}, {'turn'})
        self.assertIn(SignalKind.ASYNC_QUESTION_CREATED, [s.kind for s in signals])

    def test_multiple_batches_same_turn_notify_without_changing_running_status(self):
        signals = []
        for payload, outer in (({'type': 'task_started', 'turn_id': 'turn'}, 'event_msg'),
                               (self.call(), 'response_item'), (self.call('call_second'), 'response_item'),
                               ({'type': 'function_call_output', 'call_id': 'call_test', 'output': 'Displayed'}, 'response_item')):
            signals.extend(parse_rollout_line(self.record(payload, outer), session_id='session'))
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            self.monitor._apply(signals)
        self.assertEqual(len(list(self.monitor.notifier.pending.glob('*.json'))), 2)
        self.assertEqual(self.monitor.machine.turns[('session', 'turn')].state, TurnState.RUNNING)

    def test_actual_answers_cancel_pending_but_old_history_is_not_announced(self):
        signals = parse_rollout_line(self.record(self.call()), session_id='session')
        signals += parse_rollout_line(self.record({'type': 'message', 'role': 'user', 'content': [
                   {'type': 'input_text', 'text': answer_text()}]}), session_id='session')
        signals += parse_rollout_line(self.record({'type': 'user_message', 'message': answer_text(index=1)},
                                                 'event_msg'), session_id='session')
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            self.monitor._apply(signals)
            old = parse_rollout_line(self.record(self.call('old_call'), at=datetime.now(timezone.utc)-timedelta(days=1)), session_id='session')
            self.monitor._apply(old)
        self.assertEqual(list(self.monitor.notifier.pending.glob('*.json')), [])
        self.assertEqual(self.monitor.machine.turns, {})

    def test_native_completion_expires_unsent_question_but_keeps_final_reply(self):
        self.path.write_text(self.record({'type': 'task_started', 'turn_id': 'turn'}, 'event_msg')
                             + self.record(self.call())
                             + self.record({'type': 'task_complete', 'turn_id': 'turn',
                                            'last_agent_message': '任务已经完成'}, 'event_msg'), encoding='utf-8')
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            self.monitor._apply(self.monitor.tailer.read_new_signals(self.path))
        rows = [json.loads(p.read_text(encoding='utf-8'))['notification'] for p in self.monitor.notifier.pending.glob('*.json')]
        self.assertEqual([r['state'] for r in rows], ['completed'])
        self.assertEqual(rows[0]['message'], '任务已经完成')


class RouterTests(unittest.TestCase):
    def setUp(self):
        import test_control_router as fixture
        self.fixture = fixture
        fixture.ControlRouterTests.setUp(self)
        self.backend.control_state = lambda _: {'owner_id': 'owner', 'requests': [], 'async_questions': pending_batches(native_turn())}

    def send(self, text, **kwargs):
        return self.fixture.ControlRouterTests.send(self, text, **kwargs)

    def request(self, *args, **kwargs):
        return self.fixture.ControlRouterTests.request(self, *args, **kwargs)

    def test_async_notification_and_question_card_cannot_turn_answer_into_model_task(self):
        from unittest.mock import patch
        signal = Signal(SignalKind.ASYNC_QUESTION_CREATED, session_id=self.fixture.THREAD_A,
                        data={'call_id': 'call_test', 'questions': native_turn()['items'][0]['questions'], 'question_count': 2})
        notice = format_notification(notification(signal, turn_id='turn'))
        listing = self.send('待办', quote=notice)
        detail = self.send('1', quote=listing)
        self.assertIn('题目一', detail)
        with patch.object(self.backend, 'pause') as pause:
            for card in (notice, detail):
                for answer in ('同意', '停止', '1', '继续', '发送 答案'):
                    self.assertIn('电脑', self.send(answer, quote=card))
            pause.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_native_question_resolution_or_connection_change_expires_card(self):
        from unittest.mock import patch
        card = self.send('/use ' + self.fixture.THREAD_A)
        detail = self.send('1', quote=self.send('待办', quote=card))
        with patch.object(self.backend, 'probe', return_value=('new-generation', 1.0)):
            self.assertIn('旧连接', self.send('回答', quote=detail))
        self.backend.control_state = lambda _: {'owner_id': 'owner', 'requests': [], 'async_questions': []}
        self.assertIn('已回答', self.send('回答', quote=detail))
        self.assertEqual(self.calls, [])

if __name__ == '__main__':
    unittest.main()
