from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_control_router as fixtures
from test_control_router import FakeBackend, THREAD_A
from test_desktop_control import FakePipe, THREAD, TURN
from control_actions import describe_request, normalize_saved_queue, request_payload, request_reference, signature
from control_backend import NativeBackend
from control_store import ControlStore
from control_commands import ActionCommands
from desktop_control import DesktopControlError


def question(thread=THREAD_A):
    return {'id': 17, 'method': 'item/tool/requestUserInput', 'params': {'threadId': thread, 'turnId': TURN,
            'questions': [{'id': 'q', 'question': '是否保留现状？', 'options': [{'label': '保留'}, {'label': '修改'}]}]}}


def saved_entry(status='queued'):
    return {'id': 'message-1', 'text': '中文排队😀', 'submission': {'hostId': 'local', 'status': status}}


class ActionBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.requests = [question()]
        self.responded = []
        self.removed = []
        self.permission_updates = []
        self.compact_calls = 0
        self.compactions = []
        self.permissions = {'approvalPolicy': 'on-request', 'sandboxPolicy': {'type': 'readOnly'}}
        self.steered = []

    def require_extended_contract(self):
        pass

    def control_state(self, thread_id):
        return {**self.settings, 'owner_id': 'owner', 'requests': copy.deepcopy(self.requests),
                'permissions': self.permissions, 'settings': {}, 'compactions': self.compactions, 'turn': {}}

    def respond_to_request(self, data, text):
        self.responded.append((data['thread_id'], data['request_id'], text))
        return {'state': 'confirmed', 'reply': '窗口已受理本次答复。'}

    def remove_saved_queue_item(self, thread, entry, catalog):
        self.removed.append((thread, entry['id']))
        return {'state': 'confirmed', 'reply': '已移除这一条待发送消息。'}

    def update_permissions(self, thread, changes, baseline):
        self.permission_updates.append((thread, changes))
        return {'state': 'confirmed', 'reply': '权限已保存并回读确认。'}

    def compact(self, thread):
        self.compact_calls += 1
        return {'state': 'confirmed', 'reply': '窗口已受理压缩，不等于压缩完成。', 'baseline': [], 'owner_id': 'owner'}

    def steer(self, thread, text, connection, operation):
        self.steered.append((thread, text, connection, operation))
        return {'state': 'confirmed', 'reply': '引导已接受。'}


class ActionRouterTests(unittest.TestCase):
    request = fixtures.ControlRouterTests.request
    send = fixtures.ControlRouterTests.send

    def setUp(self):
        fixtures.ControlRouterTests.setUp(self)
        self.backend = ActionBackend()
        self.controller.backend = self.backend
        self.controller.catalog.saved_queue = lambda t: normalize_saved_queue([saved_entry()])

    def test_question_answer_stop_is_not_pause_or_new_model_message(self):
        card = self.send('/use ' + THREAD_A)
        listing = self.send('待办', quote=card)
        detail = self.send('1', quote=listing)
        reply = self.send('停止', quote=detail)
        self.assertIn('受理', reply)
        self.assertEqual(self.backend.responded, [(THREAD_A, 17, '停止')])
        self.assertEqual(self.calls, [])

    def test_old_request_resolved_on_pc_and_connection_changed_are_rejected(self):
        card = self.send('/use ' + THREAD_A)
        detail = self.send('1', quote=self.send('待办', quote=card))
        self.backend.requests = []
        self.assertIn('过期', self.send('允许', quote=detail))
        self.backend.requests = [question()]
        self.backend.probe = lambda: ('new-process', 1.0)
        self.assertIn('旧连接', self.send('1', quote=detail))
        self.assertEqual(self.backend.responded, [])

    def test_duplicate_request_cards_and_new_platform_messages_only_reply_once(self):
        card = self.send('/use ' + THREAD_A)
        one = self.send('1', quote=self.send('待办', quote=card))
        two = self.send('1', quote=self.send('待办', quote=card))
        self.send('1', quote=one)
        self.send('1', quote=two)
        self.assertEqual(len(self.backend.responded), 1)

    def test_wrong_question_number_does_not_consume_request(self):
        card = self.send('/use ' + THREAD_A)
        detail = self.send('1', quote=self.send('待办', quote=card))
        self.assertIn('不在', self.send('9', quote=detail))
        self.send('1', quote=detail)
        self.assertEqual(len(self.backend.responded), 1)

    def test_queue_selection_guides_or_deletes_exact_id(self):
        card = self.send('/use ' + THREAD_A)
        listing = self.send('队列', quote=card)
        self.assertIn('可能不含服务端', listing)
        selected = self.send('1', quote=listing)
        self.assertIn('提供引导', self.send('引导', quote=selected))
        guided = self.send('引导 检查当前任务', quote=selected, message_id='queue-guide')
        self.assertIn('当前正在运行的任务', guided)
        self.assertEqual(len(self.backend.steered), 1)
        self.assertEqual(self.backend.steered[0][1], '检查当前任务')
        self.assertIn('可以回复', self.send('中文新任务', quote=selected))
        self.assertIn('移除', self.send('删除', quote=selected))
        self.send('删除', quote=selected)
        self.assertEqual(self.backend.removed, [(THREAD_A, 'message-1')])
        self.assertEqual(self.calls, [])

    def test_queue_steer_is_deduplicated_and_old_connection_is_rejected(self):
        card = self.send('/use ' + THREAD_A)
        selected = self.send('1', quote=self.send('队列', quote=card))
        first = self.send('引导 保持当前方向', quote=selected, message_id='queue-guide-once')
        self.assertEqual(self.send('引导 保持当前方向', quote=selected, message_id='queue-guide-once'), first)
        self.assertEqual(len(self.backend.steered), 1)
        with patch.object(self.backend, 'probe', return_value=('new-process', 1.0)):
            self.assertIn('旧连接', self.controller.handle(self.request('删除', quote=selected)))

    def test_empty_saved_queue_never_claims_server_queue_empty(self):
        card = self.send('/use ' + THREAD_A)
        self.controller.catalog.saved_queue = lambda t: []
        self.assertIn('不能据此认定', self.send('队列', quote=card))

    def test_permission_choice_requires_complete_explicit_confirmation(self):
        card = self.send('/use ' + THREAD_A)
        options = self.send('权限', quote=card)
        confirm = self.send('2', quote=options)
        self.assertIn('执行命令', confirm)
        self.assertIn('未修改', self.send('确认', quote=confirm))
        self.assertEqual(self.backend.permission_updates, [])
        self.send('确认 完全访问', quote=confirm)
        self.send('确认 完全访问', quote=confirm)
        self.assertEqual(len(self.backend.permission_updates), 1)
        self.assertNotIn('permissions', self.backend.permission_updates[0][1])

    def test_custom_permissions_cannot_be_reset_from_qq(self):
        card = self.send('/use ' + THREAD_A)
        self.backend.permissions['activePermissionProfile'] = {'id': 'custom-profile'}
        self.assertIn('不会', self.send('权限', quote=card))
        self.assertEqual(self.backend.permission_updates, [])

    def test_compression_acceptance_is_not_completion_and_cannot_be_repeated_on_card(self):
        card = self.send('/use ' + THREAD_A)
        reply = self.send('压缩上下文', quote=card)
        self.assertIn('不等于', reply)
        self.assertIn('不会自动重复', self.send('压缩上下文', quote=reply))
        self.assertIn('尚未取得', self.send('压缩状态', quote=reply))
        self.backend.compactions = [{'id': 'compact-1', 'source': 'manual', 'turn_status': 'completed'}]
        self.assertIn('已完成', self.send('压缩状态', quote=reply))
        self.assertEqual(self.backend.compact_calls, 1)

    def test_unsupported_features_never_fall_through_to_model(self):
        card = self.send('/use ' + THREAD_A)
        for text in ('分支 更早的第3条', '新建项目'):
            reply = self.send(text, quote=card)
            self.assertTrue(any(word in reply for word in ('本轮只支持', '暂不能')), reply)
        self.assertEqual(self.calls, [])

    def test_invalid_unicode_never_reaches_any_native_action(self):
        card = self.send('/use ' + THREAD_A)
        for text in ('发送 \udced', '权限 \udced', '压缩上下文 \udced'):
            self.assertIn('损坏字符', self.send(text, quote=card))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.backend.compact_calls, 0)

    def test_duplicate_native_request_ids_are_not_selected_arbitrarily(self):
        card = self.send('/use ' + THREAD_A)
        listing = self.send('待办', quote=card)
        self.backend.requests.append(question())
        self.assertIn('过期', self.send('1', quote=listing))
        self.assertEqual(self.backend.responded, [])


class ActionPayloadTests(unittest.TestCase):
    def test_multi_question_mapping_is_exact_and_invalid_input_rejected(self):
        request = question()
        request['params']['questions'].append({'id': 'q2', 'question': '输入正文', 'options': []})
        payload = request_payload(request, '1=2\n2=停止😀')
        self.assertEqual(payload['response']['answers'], {'q': {'answers': ['修改']}, 'q2': {'answers': ['停止😀']}})
        with self.assertRaises(ValueError):
            request_payload(request, '2=错误顺序\n1=1')

    def test_unknown_login_or_secret_requests_stay_on_pc(self):
        request = question()
        request['params']['questions'][0]['isSecret'] = True
        with self.assertRaises(ValueError):
            describe_request(request)
        request['method'] = 'account/chatgptAuthTokens/refresh'
        with self.assertRaises(ValueError):
            request_reference(THREAD_A, request, 'owner', 'connection')

    def test_native_request_ids_keep_integer_type_and_no_other_thread(self):
        ref = request_reference(THREAD_A, question(), 'owner', 'connection')
        self.assertIsInstance(ref['request_id'], int)
        with self.assertRaises(ValueError):
            request_reference(THREAD, question(), 'owner', 'connection')

    def test_duplicate_queue_id_and_cross_host_rows_are_not_misrouted(self):
        with self.assertRaises(ValueError):
            normalize_saved_queue([saved_entry(), saved_entry()])
        entry = saved_entry()
        entry['submission']['hostId'] = 'another-host'
        self.assertEqual(normalize_saved_queue([entry]), [])

    def test_control_store_migrates_v1_and_keeps_unknown_action_claim(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'state.sqlite'
            store = ControlStore(path)
            with store.connect() as db:
                db.execute('PRAGMA user_version=1')
            upgraded = ControlStore(path)
            self.assertIsNone(upgraded.claim_operation('scope', 'action'))
            self.assertEqual(ControlStore(path).claim_operation('scope', 'action')['state'], 'unknown')
            upgraded.finish_operation('scope', 'action', 'unknown', '结果未知')
            self.assertEqual(store.claim_operation('scope', 'action')['reply'], '结果未知')


class NativeActionTests(unittest.TestCase):
    def setUp(self):
        self.pipe = FakePipe(result={})
        self.backend = NativeBackend(pipe_factory=lambda: self.pipe, contract_check=lambda: None)
        self.state = {'owner_id': 'desktop-owner', 'model': 'gpt-test', 'effort': 'max', 'settings': {},
                      'permissions': {}, 'requests': [], 'turn': {}, 'runtime': {}, 'compactions': []}
        self.backend.snapshot = lambda t: copy.deepcopy(self.state)

    def test_owner_change_before_dispatch_never_sends_mutation(self):
        result = self.backend.call(THREAD, 'mutation', {}, 1, expected_owner='old-owner')
        self.assertEqual(result['state'], 'rejected')
        self.assertFalse(any(x.get('method') == 'mutation' for x in self.pipe.writes))

    def test_queue_delete_checks_removed_identity_and_consumption_race(self):
        entry = normalize_saved_queue([saved_entry()])[0]
        catalog = type('Catalog', (), {'saved_queue': lambda _, t: [entry]})()
        method = 'thread-follower-remove-queued-message'
        for removed, state in (({'message': saved_entry()}, 'confirmed'), (None, 'rejected'),
                               ({'message': saved_entry('sending')}, 'unknown'),
                               ({'message': {**saved_entry(), 'id': 'another'}}, 'unknown')):
            with patch.object(self.backend, 'call', return_value={'state': 'confirmed', 'result': {'removed': removed}}) as call:
                self.assertEqual(self.backend.remove_saved_queue_item(THREAD, entry, catalog)['state'], state)
                self.assertEqual(call.call_args.args, (THREAD, method, {'messageId': 'message-1'}, 1))

    def test_changed_queue_or_started_entry_is_not_deleted(self):
        entry = normalize_saved_queue([saved_entry()])[0]
        catalog = type('Catalog', (), {'saved_queue': lambda _, t: normalize_saved_queue([saved_entry('sending')])})()
        with patch.object(self.backend, 'call') as call:
            self.assertEqual(self.backend.remove_saved_queue_item(THREAD, entry, catalog)['state'], 'rejected')
            call.assert_not_called()

    def test_request_payload_targets_same_request_and_lost_reply_never_repeated(self):
        request = question(THREAD)
        self.state['requests'] = [request]
        ref = request_reference(THREAD, request, 'desktop-owner', 'conn')
        with patch.object(self.backend, 'call', return_value={'state': 'unknown'}) as call:
            result = self.backend.respond_to_request(ref, '停止')
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual(call.call_args.args[2], {'requestId': 17, 'response': {'answers': {'q': {'answers': ['停止']}}}})
        call.assert_called_once()

    def test_compact_busy_rejected_and_acceptance_not_completion(self):
        self.state['turn'] = {'status': 'inProgress'}
        with patch.object(self.backend, 'call') as call:
            self.assertEqual(self.backend.compact(THREAD)['state'], 'rejected')
            call.assert_not_called()
        self.state['turn'] = {}
        with patch.object(self.backend, 'call', return_value={'state': 'confirmed', 'result': {'ok': True}}) as call:
            result = self.backend.compact(THREAD)
        self.assertIn('不等于', result['reply'])
        self.assertEqual(call.call_args.args, (THREAD, 'thread-follower-compact-thread', {}, 1))

    def test_unverified_new_contract_never_dispatches(self):
        self.backend.contract_check = lambda: (_ for _ in ()).throw(DesktopControlError('version-changed'))
        with patch.object(self.backend, 'call') as call, self.assertRaises(DesktopControlError):
            self.backend.compact(THREAD)
        call.assert_not_called()

    def test_permissions_readback_partial_write_and_stale_confirmation(self):
        changes = {'sandboxPolicy': {'type': 'readOnly'}, 'approvalPolicy': 'on-request',
                   'approvalsReviewer': 'user', 'activePermissionProfile': None}
        baseline = {'digest': signature({}), 'owner_id': 'desktop-owner'}
        def respond(*args, **kwargs):
            self.state['settings'] = copy.deepcopy(changes)
            return {'state': 'confirmed', 'result': {'applied': True}}
        with patch.object(self.backend, 'call', side_effect=respond) as call:
            result = self.backend.update_permissions(THREAD, changes, baseline)
            self.assertEqual(result['state'], 'confirmed')
            params = call.call_args.args[2]
            self.assertEqual(params['threadSettings'], changes)
            self.assertNotIn('activeTurnId', params)
            self.assertNotIn('model', params['threadSettings'])
            self.assertNotIn('permissions', params['threadSettings'])
        with patch.object(self.backend, 'call') as call:
            self.assertEqual(self.backend.update_permissions(THREAD, changes, baseline)['state'], 'rejected')
            call.assert_not_called()

    def test_unknown_queue_delete_and_no_permission_readback_never_repeat(self):
        entry = normalize_saved_queue([saved_entry()])[0]
        catalog = type('Catalog', (), {'saved_queue': lambda _, t: [entry]})()
        with patch.object(self.backend, 'call', return_value={'state': 'unknown'}) as call:
            self.assertEqual(self.backend.remove_saved_queue_item(THREAD, entry, catalog)['state'], 'unknown')
            call.assert_called_once()
        changes = {'sandboxPolicy': {'type': 'dangerFullAccess'}, 'approvalPolicy': 'never',
                   'approvalsReviewer': 'user', 'activePermissionProfile': None}
        with patch.object(self.backend, 'call', return_value={'state': 'confirmed', 'result': {'applied': True}}) as call:
            self.assertEqual(self.backend.update_permissions(THREAD, changes, {'digest': signature({}), 'owner_id': 'desktop-owner'})['state'], 'unknown')
            call.assert_called_once()


if __name__ == '__main__':
    unittest.main()
