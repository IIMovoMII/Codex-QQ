from __future__ import annotations

import sys
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "codex_desktop_weixin"))
from control_backend import NativeBackend
from desktop_control import DesktopControlError
from test_desktop_control import FakePipe, THREAD

UPDATE = "thread-follower-update-thread-settings"


class SettingsPipe(FakePipe):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.settings = {"model": "gpt-test", "reasoningEffort": "max"}
        self.reject_update = False
        self.wrong_readback = False

    def write(self, message):
        super().write(message)
        if message.get("type") != "request":
            return
        method = message["method"]
        response = self.pending[-1]
        if response.get("resultType") != "success":
            return
        if method == "thread-follower-read-model-settings":
            response["result"] = {"settings": dict(self.settings)}
        elif method == UPDATE:
            params = message["params"]
            condition = params["condition"]
            applied = not self.reject_update and condition["ifModelEquals"] == self.settings["model"] and condition["ifEffortEquals"] == self.settings["reasoningEffort"]
            response["result"] = {"applied": applied}
            if applied and not self.wrong_readback:
                changes = params["threadSettings"]
                if "model" in changes:
                    self.settings["model"] = changes["model"]
                if "effort" in changes:
                    self.settings["reasoningEffort"] = changes["effort"]


class BackendTests(unittest.TestCase):
    def backend(self, **kwargs):
        pipe = SettingsPipe(**kwargs)
        return NativeBackend(pipe_factory=lambda: pipe, contract_check=lambda: None), pipe

    def test_conditional_partial_setting_write_and_readback_preserve_other_fields(self):
        b, pipe = self.backend()
        result = b.update_model_settings(THREAD, {"effort": "high"}, {"model": "gpt-test", "effort": "max"})
        self.assertEqual(result['state'], 'confirmed')
        self.assertEqual(result['settings']['effort'], 'high')
        writes = [m for m in pipe.writes if m.get('method') == UPDATE]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0]['version'], 2)
        self.assertEqual(writes[0]['params']['threadSettings'], {'effort': 'high'})
        self.assertEqual(writes[0]['params']['condition'], {'ifModelEquals': 'gpt-test', 'ifEffortEquals': 'max'})
        self.assertNotIn('activeTurnId', writes[0]['params'])

    def test_stale_expected_values_and_noop_do_not_write(self):
        b, pipe = self.backend()
        self.assertEqual(b.update_model_settings(THREAD, {'effort': 'high'}, {'model': 'gpt-test', 'effort': 'high'})['state'], 'rejected')
        self.assertEqual(b.update_model_settings(THREAD, {'effort': 'max'}, {'model': 'gpt-test', 'effort': 'max'})['state'], 'confirmed')
        self.assertFalse(any(m.get('method') == UPDATE for m in pipe.writes))

    def test_lost_reply_is_unknown_and_never_repeated(self):
        b, pipe = self.backend(timeout=True, fail_method=UPDATE)
        self.assertEqual(b.update_model_settings(THREAD, {'effort': 'high'}, {'model': 'gpt-test', 'effort': 'max'})['state'], 'unknown')
        self.assertEqual(pipe.settings['reasoningEffort'], 'high')
        self.assertEqual(sum(m.get('method') == UPDATE for m in pipe.writes), 1)

    def test_owner_rejection_or_mismatched_readback_is_not_success(self):
        b, pipe = self.backend()
        pipe.reject_update = True
        self.assertEqual(b.update_model_settings(THREAD, {'effort': 'high'}, {'model': 'gpt-test', 'effort': 'max'})['state'], 'rejected')
        pipe.reject_update, pipe.wrong_readback = False, True
        self.assertEqual(b.update_model_settings(THREAD, {'effort': 'high'}, {'model': 'gpt-test', 'effort': 'max'})['state'], 'unknown')

    def test_method_capability_is_checked_on_actual_server_before_any_write(self):
        b, pipe = self.backend()
        b.contract_check = None
        with patch('control_backend.pipe_server_identity', return_value=('current', 1.0, 'actual-owner.exe')), \
             patch('control_contract.check_method_contract', side_effect=DesktopControlError('method-contract-changed')) as check:
            result = b.call(THREAD, UPDATE, {'threadSettings': {'effort': 'high'}}, 2)
        self.assertEqual(result['state'], 'rejected')
        check.assert_called_once_with('actual-owner.exe', UPDATE, 2)
        self.assertFalse(any(x.get('method') == UPDATE for x in pipe.writes))

    def test_canonical_history_extracts_latest_real_turn_without_retaining_content(self):
        state = {'turnHistory': {'kind': 'canonical', 'history': {
            'entitiesByKey': {'old': {'turnId': 'old', 'status': 'completed', 'items': [{'text': 'secret'}]}, 'new': {'turnId': 'new', 'status': 'inProgress', 'items': [{'text': 'secret'}]}},
            'islands': [{'entries': [{'value': 'old'}, {'value': 'new'}]}],
        }}, 'turns': []}
        view = NativeBackend._view(state)
        self.assertEqual(view['turn']['turnId'], 'new')
        self.assertEqual(view['turn']['status'], 'inProgress')
        self.assertNotIn('secret', str(view))

    def test_snapshot_only_accepts_same_owner_host_thread_and_contract_version(self):
        class SnapshotPipe(FakePipe):
            def write(self, message):
                super().write(message)
                if message.get('method') != 'thread-follower-read-model-settings':
                    return
                event = {'type': 'broadcast', 'method': 'thread-stream-state-changed', 'sourceClientId': 'desktop-owner',
                         'version': 11, 'params': {'hostId': 'local', 'conversationId': THREAD,
                         'change': {'type': 'snapshot', 'conversationState': {'id': THREAD, 'latestModel': 'gpt-test'}}}}
                self.pending.append({**event, 'sourceClientId': 'wrong-owner'})
                self.pending.append(event)
            def read(self):
                if not self.pending:
                    raise TimeoutError
                return super().read()
        pipe = SnapshotPipe()
        view = NativeBackend(pipe_factory=lambda: pipe).snapshot(THREAD)
        self.assertEqual(view['owner_id'], 'desktop-owner')
        self.assertEqual(view['model'], 'gpt-test')
        self.assertTrue(any(m.get('type') == 'broadcast' and not m['params']['following'] for m in pipe.writes))


if __name__ == '__main__':
    unittest.main()
