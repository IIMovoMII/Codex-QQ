from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src' / 'codex_desktop_weixin'))
from control_backend import NativeBackend
from desktop_control import DesktopControlError
from test_desktop_control import FakePipe, THREAD, TURN

CHILD = '00000000-0000-4000-8000-000000000003'
STEER = 'thread-follower-steer-turn'


def idle_state():
    return {'owner_id': 'desktop-owner', 'model': 'gpt-test', 'effort': 'max',
            'settings': {}, 'permissions': {'sandboxPolicy': {'type': 'readOnly'}},
            'requests': [], 'turn': {'turnId': TURN, 'status': 'completed'},
            'runtime': {'type': 'idle'}, 'pending': False, 'goal_known': True, 'goal': None,
            'cwd': 'D:/中文 空格', 'workspace_roots': ['D:/中文 空格'],
            'collaboration_mode': {'mode': 'default', 'settings': {'model': 'gpt-test'}}}


class SteerPipe(FakePipe):
    def write(self, message):
        super().write(message)
        if message.get('method') == STEER and self.pending[-1].get('resultType') == 'success':
            self.pending[-1]['result'] = self.result


class SteerBackendTests(unittest.TestCase):
    def setUp(self):
        self.pipe = SteerPipe(result={'result': {'turnId': 'active-at-execution'}}, fail_method=STEER)
        self.state = idle_state()
        self.state['turn']['status'] = 'inProgress'
        self.backend = NativeBackend(pipe_factory=lambda: self.pipe, contract_check=lambda: None)
        self.backend.snapshot = lambda _: copy.deepcopy(self.state)
        self.backend.probe = lambda: ('same-desktop', 1.0)

    def send(self, text='中文😀\n先检查日志'):
        return self.backend.steer(THREAD, text, 'same-desktop', 'qq-operation')

    def mutations(self):
        return [x for x in self.pipe.writes if x.get('method') == STEER]

    def test_active_at_execution_receipt_and_exact_owner_without_turn_override(self):
        result = self.send()
        self.assertEqual(result['state'], 'confirmed')
        self.assertEqual(result['turn_id'], 'active-at-execution')
        message = self.mutations()[0]
        self.assertEqual(message['targetClientId'], 'desktop-owner')
        self.assertEqual(message['version'], 1)
        params = message['params']
        self.assertEqual(params['conversationId'], THREAD)
        self.assertEqual(params['input'], [{'type': 'text', 'text': '中文😀\n先检查日志', 'text_elements': []}])
        self.assertEqual(params['restoreMessage']['input'], params['input'])
        self.assertEqual(params['clientUserMessageId'], 'qq-operation')
        self.assertNotIn('expectedTurnId', params)
        for key in ('model', 'reasoningEffort', 'approvalPolicy', 'sandboxPolicy', 'goal'):
            self.assertNotIn(key, params)
        self.assertEqual(len(self.mutations()), 1)

    def test_idle_initializing_and_invalid_text_never_dispatch_or_queue(self):
        for turn, cwd, text in (({'status': 'completed'}, 'D:/test', '内容'),
                                ({'status': 'inProgress'}, 'D:/test', '内容'),
                                ({'status': 'inProgress', 'turnId': TURN}, None, '内容'),
                                ({'status': 'inProgress', 'turnId': TURN}, 'D:/test', ''),
                                ({'status': 'inProgress', 'turnId': TURN}, 'D:/test', '\udced'),
                                ({'status': 'inProgress', 'turnId': TURN}, 'D:/test', 'x' * 4001)):
            self.state['turn'], self.state['cwd'] = turn, cwd
            self.assertEqual(self.send(text)['state'], 'rejected')
        self.assertEqual(self.mutations(), [])

    def test_connection_or_owner_change_blocks_before_mutation(self):
        self.backend.probe = lambda: ('different-desktop', 2.0)
        self.assertEqual(self.send()['state'], 'rejected')
        self.backend.probe = lambda: ('same-desktop', 1.0)
        self.state['owner_id'] = 'old-owner'
        self.assertEqual(self.send()['state'], 'rejected')
        self.assertEqual(self.mutations(), [])

    def test_fallback_workspace_is_same_thread_cwd_only(self):
        self.state['workspace_roots'] = None
        self.state['collaboration_mode'] = None
        self.assertEqual(self.send()['state'], 'confirmed')
        self.assertEqual(self.mutations()[0]['params']['restoreMessage']['context'],
                         {'workspaceRoots': ['D:/中文 空格'], 'commentAttachments': []})

    def test_native_idle_error_never_falls_back_to_ordinary_send(self):
        for error in ('no active turn to steer', 'thread is not being streamed', 'active turn already ended'):
            self.pipe.failure = error
            self.assertEqual(self.send()['state'], 'rejected')
        self.assertEqual(len(self.mutations()), 3)

    def test_lost_reply_or_unknown_failure_is_unknown_and_not_retried(self):
        self.pipe.timeout = True
        result = self.send()
        self.assertEqual(result['state'], 'unknown')
        self.assertIn('不会自动重试', result['reply'])
        self.assertEqual(len(self.mutations()), 1)

    def test_bad_native_acceptance_shape_does_not_claim_success(self):
        for payload in ({}, {'result': {}}, {'result': {'turnId': None}}):
            self.pipe.result = payload
            self.assertEqual(self.send()['state'], 'unknown')

    def test_unverified_steer_implementation_blocks_before_dispatch(self):
        self.backend.contract_check = None
        with patch.object(self.backend, 'control_state', return_value=self.state), \
             patch('control_contract.check_steer_contract', side_effect=DesktopControlError('changed-contract')):
            self.assertEqual(self.send()['state'], 'rejected')
        self.assertEqual(self.mutations(), [])


class ForkBackendTests(unittest.TestCase):
    def setUp(self):
        self.state = idle_state()
        self.child = idle_state()
        self.child['owner_id'] = 'child-owner'
        self.boundary = {'turn_id': TURN, 'completed_at': 100, 'updated_at': 100, 'title': '源对话'}
        self.tools = Mock()
        self.tools.boundary.side_effect = lambda thread, connection: {
            **self.boundary, 'title': '分支对话' if thread == CHILD else '源对话'}
        self.tools.fork_latest.return_value = {'state': 'confirmed', 'thread_id': CHILD}
        self.backend = NativeBackend(app_tools=self.tools, contract_check=lambda: None)
        self.backend.snapshot = lambda thread: copy.deepcopy(self.child if thread == CHILD else self.state)
        self.baseline = self.backend.fork_boundary(THREAD, 'connection')

    def fork(self):
        return self.backend.fork_latest(THREAD, self.baseline, 'connection', 'op')

    def test_latest_boundary_and_child_history_settings_are_verified_without_task(self):
        result = self.fork()
        self.assertEqual(result['state'], 'confirmed')
        self.assertEqual(result['thread_id'], CHILD)
        self.assertEqual(result['title'], '分支对话')
        self.assertEqual(result['settings'], {'model': 'gpt-test', 'effort': 'max'})
        self.tools.fork_latest.assert_called_once_with(THREAD, 'connection', 'op')
        self.assertFalse(self.tools.create.called)

    def test_running_pending_goal_unknown_goal_or_requests_refuse_before_fork(self):
        for changes in ({'turn': {'turnId': TURN, 'status': 'inProgress'}}, {'pending': True},
                        {'goal': {'status': 'paused'}}, {'goal_known': False},
                        {'requests': [{'id': 1}]}, {'runtime': {'type': 'active'}}):
            self.state = {**idle_state(), **changes}
            self.assertEqual(self.fork()['state'], 'rejected')
        self.tools.fork_latest.assert_not_called()

    def test_changed_title_history_owner_or_settings_invalidates_confirmation(self):
        for field, value in (('owner_id', 'new-owner'), ('model', 'new-model'), ('effort', 'low'),
                             ('cwd', 'D:/another'), ('permissions', {'different': True})):
            self.state = {**idle_state(), field: value}
            self.assertEqual(self.fork()['state'], 'rejected')
        self.state = idle_state()
        self.boundary['updated_at'] = 101
        self.assertEqual(self.fork()['state'], 'rejected')
        self.tools.fork_latest.assert_not_called()

    def test_snapshot_and_native_boundary_must_match(self):
        self.boundary['turn_id'] = 'different-turn'
        with self.assertRaises(DesktopControlError):
            self.backend.fork_boundary(THREAD, 'connection')
        self.assertEqual(self.fork()['state'], 'rejected')
        self.tools.fork_latest.assert_not_called()

    def test_source_change_after_dispatch_is_unknown_with_new_identity(self):
        def mutate(*_):
            self.boundary['updated_at'] = 101
            return {'state': 'confirmed', 'thread_id': CHILD}
        self.tools.fork_latest.side_effect = mutate
        result = self.fork()
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual(result['thread_id'], CHILD)
        self.tools.fork_latest.assert_called_once()

    def test_child_mismatch_running_goal_or_requests_is_not_claimed_complete(self):
        for changes in ({'turn': {'turnId': 'wrong-turn', 'status': 'completed'}},
                        {'turn': {'turnId': TURN, 'status': 'inProgress'}}, {'goal_known': False},
                        {'goal': {'status': 'active'}}, {'pending': True},
                        {'requests': [{'id': 2}]}, {'runtime': {'type': 'active'}}):
            self.child = {**idle_state(), **changes}
            result = self.fork()
            self.assertEqual(result['state'], 'unknown')
            self.assertEqual(result['thread_id'], CHILD)

    def test_lost_reply_or_settings_sync_failure_is_not_repeated(self):
        self.tools.fork_latest.return_value = {'state': 'unknown', 'thread_id': CHILD,
                                              'reply': '同步未确认；不要重复创建'}
        result = self.fork()
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual(result['thread_id'], CHILD)
        self.tools.fork_latest.assert_called_once()

    def test_readback_failure_preserves_identity_without_second_mutation(self):
        before = self.backend.snapshot
        self.backend.snapshot = lambda t: (_ for _ in ()).throw(TimeoutError()) if t == CHILD else before(t)
        result = self.fork()
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual(result['thread_id'], CHILD)
        self.tools.fork_latest.assert_called_once()


if __name__ == '__main__':
    unittest.main()
