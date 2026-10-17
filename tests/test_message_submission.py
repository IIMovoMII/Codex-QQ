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
        self.backend.snapshot = lambda _: copy.deepcopy(self.state)
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

    def test_active_or_initializing_never_starts_or_steers_and_queues_once(self):
        for turn, runtime in (({'turnId': TURN, 'status': 'inProgress'}, {'type': 'active'}),
                              ({'turnId': None, 'status': 'inProgress'}, {'type': 'active'})):
            self.state['turn'], self.state['runtime'] = turn, runtime
            result = self.send()
            self.assertEqual(result['mode'], 'queued')
            self.assertEqual(result['detail'], 'queue acknowledgement')
        self.assertEqual(self.queue.call_count, 2)
        self.assertEqual(self.starts(), [])

    def test_pending_unknown_unloaded_and_unresolved_idle_never_submits(self):
        for changes in ({'pending': True}, {'resumeState': 'needs_resume'},
                        {'requests': [{'id': 1}]}, {'runtime': {'type': 'notLoaded'}},
                        {'turn': {'status': 'mystery'}}):
            self.state = {**idle_state(), 'resumeState': 'resumed', **changes}
            self.assertEqual(self.send()['state'], 'rejected')
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

    def test_task_changes_to_active_before_idle_start_reclassifies_as_queue(self):
        idle = copy.deepcopy(self.state)
        active = {**idle, 'turn': {'turnId': 'new-active', 'status': 'inProgress'}, 'runtime': {'type': 'active'}}
        self.backend.snapshot = Mock(side_effect=[idle, active])
        self.assertEqual(self.send()['mode'], 'queued')
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

    def test_queue_loss_remains_unknown_and_native_start_is_not_fallback(self):
        self.state['turn']['status'] = 'inProgress'
        self.state['runtime'] = {'type': 'active'}
        self.queue.return_value = (False, 'codex queue 超时。')
        self.assertEqual(self.send()['state'], 'unknown')
        self.assertEqual(self.starts(), [])
        self.queue.assert_called_once()


if __name__ == '__main__':
    unittest.main()
