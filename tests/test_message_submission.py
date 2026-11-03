from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src' / 'codex_desktop_weixin'))
from control_backend import NativeBackend
from test_desktop_control import FakePipe, THREAD, TURN
from test_steer_fork_backend import idle_state
from desktop_control import DesktopControlError

START = 'thread-follower-start-turn'
CLIENT = '00000000-0000-4000-8000-000000000005'


class StartPipe(FakePipe):
    def write(self, message):
        super().write(message)
        if message.get('method') == START and self.pending[-1].get('resultType') == 'success':
            self.pending[-1]['result'] = self.result


class MessageSubmissionTests(unittest.TestCase):
    def setUp(self):
        self.pipe = StartPipe(result={'result': {'turn': {'id': TURN, 'status': 'inProgress'}}}, fail_method=START)
        self.state = {**idle_state(), 'resumeState': 'resumed', 'mode': 'default', 'thread_start_kind': 'default'}
        self.backend = NativeBackend(pipe_factory=lambda: self.pipe, contract_check=lambda: None)
        self.backend.snapshot = lambda _, expected_connection=None: copy.deepcopy(self.state)
        self.backend.probe = lambda: ('same-desktop', 1)
        self.queue = Mock(return_value=(True, 'queue acknowledgement'))

    def send(self, text='暂停后正常发送中文😀\n第二行'):
        with patch('control_backend.pipe_server_identity', return_value=('same-desktop', 1, 'Codex.exe')):
            return self.backend.submit_message(THREAD, text, 'same-desktop', CLIENT, self.queue)

    def starts(self):
        return [x for x in self.pipe.writes if x.get('method') == START]

    def test_paused_then_new_text_starts_next_turn_and_never_uses_queue_or_steer(self):
        self.state['turn']['status'] = 'interrupted'
        result = self.send()
        self.assertEqual(result['state'], 'confirmed')
        self.assertEqual(result['mode'], 'started')
        self.assertEqual(result['turn_id'], TURN)
        self.assertEqual(len(self.starts()), 1)
        self.queue.assert_not_called()
        self.assertFalse(any(x.get('method') == 'thread-follower-steer-turn' for x in self.pipe.writes))

    def test_idle_completed_and_failed_also_use_normal_start(self):
        for status in ('completed', 'failed'):
            self.state['turn']['status'] = status
            self.assertEqual(self.send()['mode'], 'started')
        self.queue.assert_not_called()

    def test_bounded_idle_long_conversation_can_start_after_owner_and_state_reread(self):
        self.state.update({'snapshot_complete': False, 'bounded_idle_safe': True})
        self.backend.snapshot = Mock(return_value=copy.deepcopy(self.state))
        result = self.send()
        self.assertEqual(result['state'], 'confirmed')
        self.assertEqual(result['mode'], 'started')
        self.assertEqual(self.backend.snapshot.call_count, 2)
        self.assertEqual(len(self.starts()), 1)
        self.queue.assert_not_called()

    def test_incomplete_non_idle_snapshot_never_starts_a_message(self):
        self.state.update({'snapshot_complete': False, 'runtime': {'type': 'idle'}})
        result = self.send()
        self.assertEqual(result['state'], 'rejected')
        self.assertIn('完整状态帧超过 8 MiB', result['reply'])
        self.assertIn('未同时确认空闲、终态和已恢复', result['reply'])
        self.assertEqual(self.starts(), [])
        self.queue.assert_not_called()

    def test_missing_requests_field_is_not_treated_as_empty_when_starting(self):
        self.state.update({'snapshot_complete': True, 'requests_known': False})
        result = self.send()
        self.assertEqual(result['state'], 'rejected')
        self.assertEqual(self.starts(), [])
        self.queue.assert_not_called()

    def test_bounded_state_that_changes_before_start_fails_closed(self):
        idle = {**self.state, 'snapshot_complete': False, 'bounded_idle_safe': True}
        unknown = {**idle, 'bounded_idle_safe': False, 'runtime': {'type': 'notLoaded'}}
        self.backend.snapshot = Mock(side_effect=[idle, unknown])
        result = self.send()
        self.assertEqual(result['state'], 'rejected')
        self.assertIn('提交前', result['reply'])
        self.assertEqual(self.starts(), [])
        self.queue.assert_not_called()

    def test_start_input_ui_restore_and_settings_inheritance(self):
        self.send()
        native = self.starts()[0]
        self.assertEqual(native['version'], 2)
        self.assertEqual(native['targetClientId'], 'desktop-owner')
        operation = native['params']['turnStart']
        request, context = operation['request'], operation['context']
        self.assertEqual(request['threadId'], THREAD)
        self.assertEqual(request['clientUserMessageId'], CLIENT)
        self.assertEqual(request['input'], [{'type': 'text', 'text': '暂停后正常发送中文😀\n第二行', 'text_elements': []}])
        self.assertEqual(request['turnTrigger'], 'composer')
        self.assertTrue(context['inheritThreadSettings'])
        self.assertEqual(context['restoreMessage']['id'], CLIENT)
        self.assertEqual(context['restoreMessage']['context']['prompt'], request['input'][0]['text'])
        self.assertNotIn('model', request)
        self.assertNotIn('effort', request)
        for key in ('sandboxPolicy', 'approvalPolicy', 'permissions', 'collaborationMode', 'serviceTier', 'expectedTurnId'):
            self.assertNotIn(key, request)

    def test_active_message_is_appended_to_native_queue(self):
        queue_id = '00000000-0000-4000-8000-000000000009'
        self.queue.return_value = (True, f'Queued message {queue_id} for thread {THREAD}.')
        for turn, runtime in (({'turnId': TURN, 'status': 'inProgress'}, {'type': 'active'}),
                              ({'turnId': None, 'status': 'inProgress'}, {'type': 'active'})):
            self.state['turn'], self.state['runtime'] = turn, runtime
            result = self.send()
            self.assertEqual(result['state'], 'confirmed')
            self.assertEqual(result['mode'], 'queued')
        self.assertEqual(self.queue.call_count, 2)
        self.assertEqual(self.starts(), [])

    def test_pending_unknown_unloaded_and_unresolved_idle_never_submits(self):
        for changes in ({'pending': True}, {'resumeState': 'needs_resume'},
                        {'requests': [{'id': 1}]}, {'runtime': {'type': 'notLoaded'}},
                        {'turn': {'status': 'mystery'}}):
            self.state = {**idle_state(), 'resumeState': 'resumed', **changes}
            with patch('control_backend.time.sleep'):
                self.assertEqual(self.send()['state'], 'rejected')
        self.queue.assert_not_called()
        self.assertEqual(self.starts(), [])

    def test_transient_resume_state_is_reread_before_rejecting(self):
        loading = {**self.state, 'resumeState': 'needs_resume'}
        ready = copy.deepcopy(self.state)
        self.backend.snapshot = Mock(side_effect=[loading, ready, ready])
        with patch('control_backend.time.sleep') as sleep:
            result = self.send()
        self.assertEqual(result['state'], 'confirmed')
        self.assertEqual(result['mode'], 'started')
        self.assertEqual(len(self.starts()), 1)
        self.assertEqual(sleep.call_count, 1)
        self.queue.assert_not_called()

    def test_resume_state_that_never_finishes_stays_fail_closed(self):
        loading = {**self.state, 'resumeState': 'needs_resume'}
        self.backend.snapshot = Mock(side_effect=[loading] * 8)
        with patch('control_backend.time.sleep') as sleep:
            result = self.send()
        self.assertEqual(result['state'], 'rejected')
        self.assertIn('resumeState=needs_resume', result['reply'])
        self.assertIn('未发送、未排队', result['reply'])
        self.assertEqual(sleep.call_count, 5)
        self.queue.assert_not_called()
        self.assertEqual(self.starts(), [])

    def test_pending_submission_stops_resume_retry(self):
        pending = {**self.state, 'resumeState': 'needs_resume', 'pending': True}
        self.backend.snapshot = Mock(return_value=pending)
        with patch('control_backend.time.sleep') as sleep:
            result = self.send()
        self.assertEqual(result['state'], 'rejected')
        self.assertIn('上一条提交尚无 Desktop 回执', result['reply'])
        self.assertEqual(sleep.call_count, 0)
        self.queue.assert_not_called()
        self.assertEqual(self.starts(), [])

    def test_native_stop_still_settling_does_not_put_new_message_in_paused_queue(self):
        self.state['turn']['status'] = 'interrupted'
        self.state['runtime'] = {'type': 'active'}
        result = self.send()
        self.assertEqual(result['state'], 'rejected')
        self.assertIn('收尾', result['reply'])
        self.queue.assert_not_called()
        self.assertEqual(self.starts(), [])

    def test_unverified_start_contract_does_not_dispatch(self):
        self.backend.contract_check = None
        with patch.object(self.backend, 'control_state', return_value=self.state), \
             patch('control_contract.check_queue_contract', side_effect=DesktopControlError('changed-contract')):
            with self.assertRaises(DesktopControlError):
                self.send()
        self.queue.assert_not_called()
        self.assertEqual(self.starts(), [])

    def test_task_changes_to_active_before_idle_start_uses_native_queue(self):
        idle = copy.deepcopy(self.state)
        active = {**idle, 'turn': {'turnId': 'new-active', 'status': 'inProgress'}, 'runtime': {'type': 'active'}}
        self.backend.snapshot = Mock(side_effect=[idle, active])
        queue_id = '00000000-0000-4000-8000-000000000009'
        self.queue.return_value = (True, f'Queued message {queue_id} for thread {THREAD}.')
        result = self.send()
        self.assertEqual(result['state'], 'confirmed')
        self.assertEqual(result['mode'], 'queued')
        self.queue.assert_called_once()
        self.assertEqual(self.starts(), [])

    def test_lost_start_response_never_falls_back_to_queue_or_steer(self):
        self.pipe.timeout = True
        result = self.send()
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual(len(self.starts()), 1)
        self.queue.assert_not_called()

    def test_bad_response_or_explicit_native_error_never_replays(self):
        for value in ({}, {'result': {}}, {'result': {'turn': {'id': None}}}):
            self.pipe.result = value
            self.assertEqual(self.send()['state'], 'unknown')
        self.pipe.failure = 'backend refused request'
        self.assertEqual(self.send()['state'], 'unknown')
        self.queue.assert_not_called()

    def test_changed_owner_or_connection_before_dispatch_does_not_write(self):
        self.state['owner_id'] = 'old-owner'
        self.assertEqual(self.send()['state'], 'rejected')
        self.state['owner_id'] = 'desktop-owner'
        self.backend.probe = lambda: ('restarted-desktop', 2)
        self.assertEqual(self.send()['state'], 'rejected')
        self.assertEqual(self.starts(), [])
        self.queue.assert_not_called()

    def test_invalid_text_never_opens_pipe_or_launches_subprocess(self):
        for text in ('', 'x' * 4001, '\udc80'):
            self.assertEqual(self.send(text)['state'], 'rejected')
        self.assertEqual(self.pipe.writes, [])
        self.queue.assert_not_called()

    def test_busy_message_is_appended_to_native_queue_with_exact_receipt(self):
        self.state['turn']['status'] = 'inProgress'
        self.state['runtime'] = {'type': 'active'}
        queue_id = '00000000-0000-4000-8000-000000000009'
        self.queue.return_value = (True, f'Queued message {queue_id} for thread {THREAD}.')
        result = self.send()
        self.assertEqual(result['state'], 'confirmed')
        self.assertEqual(result['mode'], 'queued')
        self.assertEqual(result['queue_id'], queue_id)
        self.queue.assert_called_once_with(THREAD, '暂停后正常发送中文😀\n第二行')
        self.assertEqual(self.starts(), [])
        self.assertEqual(self.pipe.writes, [])

    def test_busy_queue_timeout_is_unknown_and_not_retried(self):
        self.state['turn']['status'] = 'inProgress'
        self.state['runtime'] = {'type': 'active'}
        self.queue.return_value = (None, 'codex queue 命令超时')
        result = self.send()
        self.assertEqual(result['state'], 'unknown')
        self.assertIn('可能已经排队', result['reply'])
        self.assertIn('不会重发', result['reply'])
        self.queue.assert_called_once()
        self.assertEqual(self.starts(), [])

if __name__ == '__main__':
    unittest.main()
