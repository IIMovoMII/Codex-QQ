from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

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
        self.queue_guides = []

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

    def guide_saved_queue_item(self, thread, entry, catalog, connection=None):
        self.queue_guides.append((thread, entry['id'], connection))
        self.removed.append((thread, entry['id']))
        self.steered.append((thread, '原排队正文', connection, 'operation'))
        return {'state': 'confirmed', 'reply': '已先删除，再将相同正文引导给当前任务。'}

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

    def test_queue_selection_converts_or_deletes_exact_id_without_confirmation(self):
        card = self.send('/use ' + THREAD_A)
        listing = self.send('队列', quote=card)
        self.assertIn('可能不含服务端', listing)
        selected = self.send('1', quote=listing)
        self.assertIn('中文排队', self.send('状态', quote=selected))
        guided = self.send('引导', quote=selected, message_id='queue-guide')
        self.assertIn('先删除', guided)
        self.assertEqual(len(self.backend.steered), 1)
        self.assertEqual(self.backend.queue_guides, [(THREAD_A, 'message-1', 'same-process')])
        self.assertIn('只回复', self.send('引导 改成别的内容', quote=selected))
        self.assertIn('可以回复', self.send('中文新任务', quote=selected))
        self.assertIn('移除', self.send('删除', quote=selected))
        self.send('删除', quote=selected)
        self.assertEqual(self.backend.removed, [(THREAD_A, 'message-1'), (THREAD_A, 'message-1')])
        self.assertEqual(self.calls, [])

    def test_queue_cards_support_read_only_navigation_without_dispatch(self):
        card = self.send('/use ' + THREAD_A)
        selected = self.send('1', quote=self.send('队列', quote=card))
        for command in ('队列', '状态'):
            self.assertIn('中文排队', self.send(command, quote=selected))
        target = self.send('对话', quote=selected)
        self.assertIn('对话编号：' + THREAD_A, target)
        self.assertEqual(self.backend.removed, [])
        self.assertEqual(self.backend.steered, [])
        self.assertEqual(self.calls, [])

    def test_queue_conversion_is_deduplicated_and_old_connection_is_rejected(self):
        card = self.send('/use ' + THREAD_A)
        selected = self.send('1', quote=self.send('队列', quote=card))
        first = self.send('引导', quote=selected, message_id='queue-guide-once')
        self.assertEqual(self.send('引导', quote=selected, message_id='queue-guide-once'), first)
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

    def test_cancelled_permission_options_card_cannot_be_reused(self):
        card = self.send('/use ' + THREAD_A)
        options = self.send('权限', quote=card)
        self.assertIn('已取消', self.send('取消', quote=options))
        self.assertIn('选项卡及其确认卡不再执行', self.send('2', quote=options))
        self.assertEqual(self.backend.permission_updates, [])

    def test_cancelled_permission_confirmation_card_cannot_be_reused(self):
        card = self.send('/use ' + THREAD_A)
        options = self.send('权限', quote=card)
        confirm = self.send('2', quote=options)
        self.assertIn('已取消', self.send('取消', quote=confirm))
        self.assertIn('选项卡及其确认卡不再执行', self.send('确认 完全访问', quote=confirm))
        self.assertEqual(self.backend.permission_updates, [])

    def test_queue_guide_preflight_rejection_does_not_consume_one_shot(self):
        card = self.send('/use ' + THREAD_A)
        queue = self.send('队列', quote=card)
        selected = self.send('1', quote=queue)
        self.backend.preflight_guide_saved_queue_item = Mock(side_effect=[
            {'state': 'rejected', 'retryable': True, 'reply': '任务仍在初始化，稍后重试。'},
            {'state': 'confirmed', 'reply': '就绪。'},
        ])
        self.assertIn('初始化', self.send('引导', quote=selected))
        self.assertIn('已先删除', self.send('引导', quote=selected))
        self.assertEqual(self.backend.preflight_guide_saved_queue_item.call_count, 2)
        self.assertEqual(len(self.backend.queue_guides), 1)

    def test_permission_cancel_after_success_reports_applied_state(self):
        card = self.send('/use ' + THREAD_A)
        options = self.send('权限', quote=card)
        confirm = self.send('2', quote=options)
        applied = self.send('确认 完全访问', quote=confirm)
        cancelled = self.send('取消', quote=confirm)
        self.assertEqual(cancelled, applied)
        self.assertNotIn('已取消', cancelled)
        self.assertEqual(len(self.backend.permission_updates), 1)

    def test_permission_cancel_after_unknown_reports_unknown_without_claiming_cancel(self):
        card = self.send('/use ' + THREAD_A)
        options = self.send('权限', quote=card)
        confirm = self.send('2', quote=options)
        self.backend.update_permissions = lambda *a, **k: {'state': 'unknown', 'reply': '权限写入结果未知。'}
        self.assertIn('未知', self.send('确认 完全访问', quote=confirm))
        reply = self.send('取消', quote=confirm)
        self.assertIn('未知', reply)
        self.assertNotIn('已取消', reply)

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

    def test_control_store_reuses_thread_refs_and_expires_old_dispatch_tombstones_safely(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ControlStore(Path(temp) / 'state.sqlite')
            first = store.reference('scope', 'thread', {'thread_id': THREAD_A})
            second = store.reference('scope', 'thread', {'thread_id': THREAD_A})
            self.assertEqual(first, second)
            queue_ref = store.reference('scope', 'queued', {'thread_id': THREAD_A, 'id': 'old'}, ttl=None)
            store.begin('scope', 'message-old', time.time() - 40 * 86400)
            self.assertIsNone(store.claim_operation('scope', 'old-action'))
            with store.connect() as db:
                old = time.time() - 40 * 86400
                db.execute("UPDATE refs SET created=? WHERE id=?", (old, queue_ref))
                db.execute("UPDATE receipts SET updated=? WHERE message_id='message-old'", (old,))
                db.execute("UPDATE operations SET updated=? WHERE key='old-action'", (old,))
            store.prune()
            self.assertIsNone(store.get_reference('scope', queue_ref))
            self.assertIsNotNone(store.get_reference('scope', first))
            self.assertIn('未知', store.begin('scope', 'message-old', time.time()))
            self.assertEqual(store.operation('scope', 'old-action')['state'], 'unknown')

    def test_control_store_bounds_permanent_thread_refs_per_scope(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ControlStore(Path(temp) / 'state.sqlite')
            tokens = []
            for index in range(store.MAX_THREAD_REFS_PER_SCOPE + 8):
                tokens.append(store.reference('scope', 'thread', {'thread_id': f'thread-{index}'}))
            store.prune()
            with store.connect() as db:
                count = db.execute(
                    "SELECT COUNT(*) FROM refs WHERE scope=? AND kind='thread' AND expires IS NULL",
                    ('scope',),
                ).fetchone()[0]
            self.assertEqual(count, store.MAX_THREAD_REFS_PER_SCOPE)
            self.assertIsNone(store.get_reference('scope', tokens[0]))
            self.assertIsNotNone(store.get_reference('scope', tokens[-1]))


class NativeActionTests(unittest.TestCase):
    def setUp(self):
        self.pipe = FakePipe(result={})
        self.backend = NativeBackend(pipe_factory=lambda: self.pipe, contract_check=lambda: None)
        self.state = {'owner_id': 'desktop-owner', 'model': 'gpt-test', 'effort': 'max', 'settings': {},
                      'permissions': {}, 'requests': [], 'turn': {}, 'runtime': {}, 'compactions': []}
        self.backend.snapshot = lambda t, expected_connection=None: copy.deepcopy(self.state)

    def test_owner_change_before_dispatch_never_sends_mutation(self):
        result = self.backend.call(THREAD, 'mutation', {}, 1, expected_owner='old-owner')
        self.assertEqual(result['state'], 'rejected')
        self.assertFalse(any(x.get('method') == 'mutation' for x in self.pipe.writes))

    def test_non_object_control_status_is_reported_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); state = root / 'state'; state.mkdir()
            path = state / 'control-status.json'
            backend = NativeBackend()
            for payload in ('[]', json.dumps({'threads': []})):
                path.write_text(payload, encoding='utf-8')
                with patch('runtime_io.default_data_dir', return_value=root):
                    self.assertIn('状态暂不可用', backend.status(THREAD))
                self.assertEqual(path.read_text(encoding='utf-8'), payload)

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

    def test_queue_conversion_deletes_before_guiding_exact_text_and_stops_on_uncertain_delete(self):
        raw = saved_entry()
        entry = normalize_saved_queue([raw])[0]
        catalog = type('Catalog', (), {'saved_queue_rows': lambda _, t: [raw]})()
        self.state['turn'] = {'status': 'inProgress', 'turnId': 'running-turn'}
        self.state['cwd'] = 'C:\\workspace'
        events = []

        def remove(*args, **kwargs):
            events.append('delete')
            return {'state': 'confirmed', 'result': {'removed': {'message': raw}}}

        def steer(thread, text, connection, operation):
            events.append(('guide', thread, text))
            return {'state': 'confirmed', 'reply': 'accepted'}

        with patch.object(self.backend, 'call', side_effect=remove) as call, \
                patch.object(self.backend, 'steer', side_effect=steer):
            result = self.backend.guide_saved_queue_item(THREAD, entry, catalog, connection='conn')
        self.assertEqual(result['state'], 'confirmed')
        self.assertEqual(events, ['delete', ('guide', THREAD, raw['text'])])
        self.assertEqual(call.call_args.args[1:3],
                         ('thread-follower-remove-queued-message', {'messageId': entry['id']}))

        events.clear()
        with patch.object(self.backend, 'call', return_value={'state': 'unknown'}) as call, \
                patch.object(self.backend, 'steer') as guide:
            result = self.backend.guide_saved_queue_item(THREAD, entry, catalog, connection='conn')
        self.assertEqual(result['state'], 'unknown')
        self.assertIn('没有继续发送引导', result['reply'])
        call.assert_called_once()
        guide.assert_not_called()

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
