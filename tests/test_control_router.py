from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "codex_desktop_weixin"))
from control_router import ControlRouter
from control_store import ControlStore
from control_catalog import LocalCatalog
from desktop_control import DesktopControlError

THREAD_A = "00000000-0000-7000-8000-000000000001"
THREAD_B = "00000000-0000-7000-8000-000000000002"


class FakeCatalog:
    def __init__(self):
        self.rows = {t: {"id": t, "name": "同名对话", "project_name": p} for t, p in ((THREAD_A, "甲"), (THREAD_B, "乙"))}

    def thread(self, t):
        return self.rows.get(t)

    def threads(self, **kwargs):
        return list(self.rows.values())

    def projects(self):
        return [{"id": "project-a", "name": "甲", "cwd": "D:/test"}]

    label = staticmethod(LocalCatalog.label)

    def models(self):
        return {"source": "测试目录", "options": [
            {"model": "gpt-test", "name": "测试模型", "efforts": ["high", "max"]},
            {"model": "gpt-second", "name": "另一个模型", "efforts": ["low", "high"]},
        ]}


class FakeBackend:
    online = True

    def __init__(self):
        self.settings = {"model": "gpt-test", "effort": "max"}
        self.updates = []

    def probe(self):
        if not self.online:
            raise OSError("offline")
        return "same-process", 1.0

    def ensure_owner(self, t):
        self.probe()

    def open_thread(self, thread_id, connection, operation_id):
        return {'state': 'confirmed'}

    def submit_message(self, thread, text, connection, operation, queue_func):
        # Existing router tests isolate queue transport; native path cases live
        # in test_message_submission with framed owner responses.
        self.ensure_owner(thread)
        ok, detail = queue_func(thread, text)
        return {'state': 'confirmed', 'mode': 'queued', 'detail': detail} if ok else {
            'state': 'unknown' if '超时' in detail else 'rejected',
            'reply': '指令已发出，但没有收到确认，可能已经执行。不会自动重发，请先查看目标对话。'
                     if '超时' in detail else '未能提交：' + detail}

    def status(self, t):
        return "运行中" if t == THREAD_A else "空闲"

    def snapshot(self, t):
        return {**self.settings, "permissions": {"sandboxPolicy": {"type": "dangerFullAccess"}, "approvalPolicy": "never"}, "runtime": {"type": "active"}}

    read_settings = snapshot

    def update_model_settings(self, thread_id, changes, expected):
        if any(self.settings[k] != expected[k] for k in ("model", "effort")):
            return {"state": "rejected", "reply": "对话设置已发生变化，未修改。"}
        self.updates.append((thread_id, changes))
        self.settings.update(changes)
        return {"state": "confirmed", "reply": "设置已保存并回读确认。", "settings": dict(self.settings)}

    def pause(self, t):
        return {"state": "confirmed", "reply": "已暂停"}


class ControlRouterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {"CC_SESSION_KEY": "qqbot:user", "CC_PROJECT": "test"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.store = ControlStore(Path(self.tmp.name) / "control.sqlite")
        self.backend = FakeBackend()
        self.calls = []
        self.controller = ControlRouter(self.store, FakeCatalog(), self.backend,
                                        lambda t, text: (self.calls.append((t, text)) or True, ""))
        self.index = 0

    def test_orphan_gateway_rejects_before_claiming_or_probing_when_app_is_closed(self):
        self.controller.availability_check = lambda: False
        with patch.object(self.backend, 'probe') as probe:
            self.assertIn('助手未就绪或已退出', self.send('继续'))
            probe.assert_not_called()
        self.assertEqual(self.calls, [])

    def request(self, text, quote="", message_id=None, timestamp=None):
        self.index += 1
        return {"_meta": {"cc_connect": {"version": 1, "platform": "qqbot", "session_key": "qqbot:user", "user_id": "user", "message_id": message_id or str(self.index), "sent_at_ms": (timestamp or time.time()) * 1000, "connection_ready_at_ms": 1000, "text": text, "quoted_text": quote}}}

    def send(self, text, **kwargs):
        return self.controller.handle(self.request(text, **kwargs))

    def test_title_selection_uses_project_and_fixed_snapshot(self):
        listing = self.send("对话")
        self.assertIn("［甲］同名对话", listing)
        self.assertIn("空闲", listing)
        self.send("对话")
        card = self.send("2", quote=listing)
        self.assertIn("［乙］", card)
        self.send("你好😀", quote=card)
        self.assertEqual(self.calls, [(THREAD_B, "你好😀")])

    def test_explicit_selection_opens_unloaded_chat_once_and_keeps_quote_valid(self):
        loaded = set()
        opened = []
        def owner(thread):
            if thread not in loaded:
                raise DesktopControlError('no-client-found')
        def open_thread(thread, connection, operation):
            opened.append((thread, connection))
            loaded.add(thread)
            return {'state': 'confirmed'}
        self.backend.ensure_owner = owner
        self.backend.open_thread = open_thread
        listing = self.send('对话')
        self.assertEqual(opened, [])
        card = self.send('1', quote=listing)
        self.assertIn('已打开并连接', card)
        self.assertEqual(opened, [(THREAD_A, 'same-process')])
        self.assertIn('已选定', self.send('', quote=card))
        self.assertEqual(self.calls, [])

    def test_old_quote_selects_only_this_message_without_a_default_target(self):
        a = self.send("/use " + THREAD_A)
        self.send("/use " + THREAD_B)
        self.send("引号测试", quote=a)
        self.assertIn("没有引用目标对话", self.send("新消息"))
        self.assertEqual(self.calls, [(THREAD_A, "引号测试")])

    def test_all_thread_operations_require_a_quote_even_with_legacy_selection(self):
        self.store.select("test|qqbot:user|user", THREAD_A)
        self.send("/use " + THREAD_B)
        with patch.object(self.backend, "read_settings") as read, patch.object(self.backend, "pause") as pause:
            for text in ("设置", "/current", "正文", "继续", "/continue", "暂停", "/pause", "发送 正文", "引导 正文", "队列", "模型", "思考档位", "权限", "改名 新名字", "压缩上下文", "分支"):
                with self.subTest(text=text):
                    self.assertIn("没有引用目标对话", self.send(text))
            read.assert_not_called()
            pause.assert_not_called()
        self.assertIn("最近对话", self.send("对话"))
        self.assertIn("项目", self.send("项目"))
        self.assertIn("搜索结果", self.send("搜索 同名"))
        self.assertIn("QQ 控制", self.send("帮助"))
        self.assertEqual(self.calls, [])

    def test_duplicate_event_does_not_repeat_but_same_text_new_event_does(self):
        card = self.send("/use " + THREAD_A)
        first = self.send("继续", message_id="id-one", quote=card)
        self.assertEqual(self.send("继续", message_id="id-one", quote=card), first)
        self.send("继续", message_id="id-two", quote=card)
        self.assertEqual(len(self.calls), 2)

    def test_offline_reject_and_reconnect_cutoff_never_replays(self):
        card = self.send("/use " + THREAD_A)
        self.backend.online = False
        self.assertIn("作废", self.send("继续", message_id="offline", quote=card))
        self.backend.online = True
        self.assertIn("作废", self.send("继续", timestamp=time.time() - 10, quote=card))
        self.assertIn("作废", self.send("继续", message_id="offline", quote=card))
        self.assertEqual(self.calls, [])
        self.assertIn("已提交", self.send("继续", quote=card))

    def test_gateway_replay_uses_new_reply_time_not_old_quote_time(self):
        card = self.send("/use " + THREAD_A)
        params = self.request("继续", quote=card)
        params["_meta"]["cc_connect"]["connection_ready_at_ms"] = time.time() * 1000 + 2000
        self.assertIn("作废", self.controller.handle(params))
        self.assertIn("已提交", self.send("继续", quote=card))

    def test_bridge_restart_rejects_event_buffered_before_new_bridge_generation(self):
        card = self.send('/use ' + THREAD_A)
        restarted = ControlRouter(self.store, FakeCatalog(), self.backend,
                                   lambda t, text: (self.calls.append((t, text)) or True, ""))
        # Simulate an event buffered before this bridge instance started.
        buffered_at = restarted._process_started_at - 1.0
        params = self.request('继续', quote=card, message_id='before-bridge-restart',
                              timestamp=buffered_at)
        self.assertIn('作废', restarted.handle(params))
        self.assertEqual(self.calls, [])

    def test_bridge_state_store_failure_fails_closed_before_dispatch(self):
        with patch.object(self.store, 'begin_bridge_session', side_effect=sqlite3.OperationalError('locked')):
            with patch.object(self.backend, 'probe') as probe:
                self.assertIn('本地控制状态暂不可用', self.send('继续'))
                probe.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_backend_call_forwards_expected_connection_alias(self):
        observed = []
        class ExpectedConnectionBackend:
            def read_settings(self, thread_id, expected_connection=None):
                observed.append((thread_id, expected_connection))
                return {}
        router = ControlRouter(self.store, FakeCatalog(), ExpectedConnectionBackend(), lambda *_: (True, ''))
        self.assertEqual(router._backend_call('read_settings', THREAD_A, connection='generation-a'), {})
        self.assertEqual(observed, [(THREAD_A, 'generation-a')])

    def test_bridge_restart_for_one_qq_session_does_not_invalidate_another(self):
        with patch.dict(os.environ, {'CC_SESSION_KEY': 'qqbot:other'}, clear=False):
            other = ControlRouter(self.store, FakeCatalog(), self.backend,
                                  lambda t, text: (self.calls.append((t, text)) or True, ""))
            other_card_params = self.request('/use ' + THREAD_A, message_id='other-select')
            other_card_params['_meta']['cc_connect'].update({'session_key': 'qqbot:other', 'user_id': 'other'})
            other_card = other.handle(other_card_params)
            event_time = time.time()
            other_params = self.request('继续', quote=other_card, message_id='other-session-event',
                                        timestamp=event_time + 0.01)
            other_params['_meta']['cc_connect'].update({'session_key': 'qqbot:other', 'user_id': 'other'})
            other_reply = other.handle(other_params)
            self.assertIn('已提交', other_reply)
        self.assertEqual(self.calls, [(THREAD_A, '继续')])

    def test_missing_metadata_fake_body_identity_and_wrong_user_quote_rejected(self):
        self.assertIn("缺少", self.controller.handle({"prompt": [{"text": "/continue"}]}))
        card = self.send("/use " + THREAD_A)
        params = self.request("继续", quote=card)
        params["_meta"]["cc_connect"]["user_id"] = "other"
        self.assertIn("过期", self.controller.handle(params))
        self.assertEqual(self.calls, [])

    def test_malformed_metadata_and_quote_return_clear_rejection(self):
        for envelope in (None, [], "invalid"):
            self.assertIn("缺少", self.controller.handle({"_meta": envelope}))
        params = self.request("继续")
        params["_meta"]["cc_connect"]["quoted_text"] = ["fake"]
        self.assertIn("引用信息格式", self.controller.handle(params))
        self.assertEqual(self.calls, [])

    def test_unloaded_target_does_not_invalidate_another_online_owner(self):
        a = self.send("/use " + THREAD_A)
        b = self.send("/use " + THREAD_B)
        with patch.object(self.backend, "ensure_owner", side_effect=DesktopControlError("no-client-found")):
            self.assertIn("无法连接该对话", self.send("继续", quote=a))
        self.assertIn("已提交", self.send("继续", quote=b))
        self.assertEqual(self.calls, [(THREAD_B, "继续")])

    def test_timeout_is_unknown_without_retry(self):
        card = self.send("/use " + THREAD_A)
        self.controller.queue_func = lambda t, text: (False, "codex queue 超时。")
        reply = self.send("继续", message_id="uncertain", quote=card)
        self.assertIn("可能已经执行", reply)
        self.assertEqual(self.send("继续", message_id="uncertain", quote=card), reply)

    def test_native_idle_start_returns_immediate_receipt_and_duplicate_does_not_send(self):
        card = self.send('/use ' + THREAD_A)
        with patch.object(self.backend, 'submit_message', return_value={
                'state': 'confirmed', 'mode': 'started', 'turn_id': 'new-turn'}) as submit:
            reply = self.send('暂停后的新消息', quote=card, message_id='native-start')
            self.assertIn('这条新消息没有排队', reply)
            self.assertIn('模型、思考档位和权限', reply)
            self.assertEqual(self.send('暂停后的新消息', quote=card, message_id='native-start'), reply)
            submit.assert_called_once()
            self.assertEqual(self.calls, [])

    def test_native_busy_queue_returns_visible_queue_card(self):
        card = self.send('/use ' + THREAD_A)
        entry = {'id': '00000000-0000-4000-8000-000000000009', 'summary': '忙时消息',
                 'status': 'queued', 'digest': 'digest', 'paused': False}
        with patch.object(self.backend, 'submit_message', return_value={
                'state': 'confirmed', 'mode': 'queued', 'native': True,
                'queue_id': entry['id'], 'entry': entry}) as submit:
            reply = self.send('忙时消息', quote=card, message_id='native-queue')
            self.assertIn('可见待发送列表', reply)
            self.assertIn(entry['id'], reply)
            self.assertIn('队列', reply)
            submit.assert_called_once()
            self.assertEqual(self.calls, [])

    def test_window_error_does_not_mask_version_mismatch_or_unknown_native_state(self):
        card = self.send('/use ' + THREAD_A)
        for error, hint in (('no-client-found', '未载入'),
                            ('request-version-mismatch', '接口版本不匹配'),
                            ('invalid-response', '不能认定是对话未打开')):
            with self.subTest(error=error), patch.object(self.backend, 'read_settings', side_effect=DesktopControlError(error)):
                reply = self.send('设置', quote=card)
                self.assertIn(hint, reply)
        self.assertEqual(self.calls, [])

    def test_reserved_command_can_be_forced_to_normal_text(self):
        card = self.send("/use " + THREAD_A)
        self.assertIn("已暂停", self.send("暂停", quote=card))
        self.send("发送 暂停", quote=card)
        self.assertEqual(self.calls, [(THREAD_A, "暂停")])

    def test_settings_show_actual_values_and_never_use_global_defaults(self):
        card = self.send("/use " + THREAD_A)
        reply = self.send("设置", quote=card)
        self.assertIn("gpt-test", reply)
        self.assertIn("max", reply)
        self.assertIn("完全访问", reply)
        self.assertEqual(self.calls, [])

    def test_effort_options_and_direct_value_write_only_quoted_thread(self):
        card = self.send("/use " + THREAD_B)
        options = self.send("思考档位", quote=card)
        self.assertIn("1. high", options)
        reply = self.send("1", quote=options)
        self.assertIn("回读确认", reply)
        self.assertEqual(self.backend.updates, [(THREAD_B, {"effort": "high"})])
        self.assertIn("未修改", self.send("2", quote=options))
        self.assertIn("回读确认", self.send("思考档位 max", quote=card))
        self.assertEqual(self.backend.settings['effort'], 'max')
        self.assertEqual(self.calls, [])

    def test_model_change_requires_supported_effort_and_never_downgrades(self):
        card = self.send("/use " + THREAD_A)
        options = self.send("模型", quote=card)
        self.assertIn("不会自动降档", self.send("2", quote=options))
        self.assertEqual(self.backend.updates, [])
        reply = self.send("2 high", quote=options)
        self.assertIn("gpt-second", reply)
        self.assertEqual(self.backend.settings, {"model": "gpt-second", "effort": "high"})
        self.assertEqual(self.calls, [])

    def test_setting_option_cache_is_rechecked_before_modification(self):
        card = self.send("/use " + THREAD_A)
        options = self.send("思考档位", quote=card)
        with patch.object(self.controller.catalog, "models", side_effect=ValueError("stale")):
            self.assertIn("未修改", self.send("1", quote=options))
        self.assertEqual(self.backend.updates, [])

    def test_legacy_setting_options_without_connection_are_rejected(self):
        card = self.send('/use ' + THREAD_A)
        token = self.store.reference('test|qqbot:user|user', 'setting_options', {
            'thread_id': THREAD_A, 'field': 'effort', 'options': [{'effort': 'high'}],
            'remaining': [], 'offset': 0, 'source': 'legacy',
            'baseline': {'model': 'gpt-test', 'effort': 'max'},
        })
        quote = f'引用标识：{token}'
        self.assertIn('旧连接', self.send('1', quote=quote))
        self.assertEqual(self.backend.updates, [])

    def test_long_setting_options_use_fixed_pages_without_cutting_controls(self):
        card = self.send("/use " + THREAD_A)
        options = [{"model": f'model-{i}', "name": '较长的模型名称' * 9, "efforts": ['high', 'max']} for i in range(40)]
        catalog = {"source": "测试目录", "options": options}
        with patch.object(self.controller.catalog, "models", return_value=catalog):
            first = self.send("模型", quote=card)
            self.assertIn("下一页", first)
            self.assertTrue(first.rstrip().splitlines()[-1].startswith('引用标识：'))
            second = self.send("下一页", quote=first)
            import re
            number = re.search(r'(?m)^(\d+)\. ', second).group(1)
            reply = self.send(number, quote=second)
            self.assertIn("回读确认", reply)
            self.assertEqual(self.backend.settings['model'], f'model-{int(number)-1}')

    def test_reference_survives_new_store_instance_and_legacy_forget(self):
        route = Path(self.tmp.name) / "legacy.json"
        route.write_text(json.dumps({"qqbot:user": THREAD_A}), encoding="utf-8")
        store = ControlStore(Path(self.tmp.name) / "migrated.sqlite", legacy_routes=route)
        self.assertEqual(store.selection("scope", "qqbot:user"), THREAD_A)
        store.select("scope", None)
        reloaded = ControlStore(store.path, legacy_routes=route)
        self.assertIsNone(reloaded.selection("scope", "qqbot:user"))
        token = store.reference("scope", "thread", {"thread_id": THREAD_B})
        self.assertEqual(reloaded.get_reference("scope", token)["data"]["thread_id"], THREAD_B)

    def test_two_store_instances_do_not_overwrite_other_users(self):
        second = ControlStore(self.store.path)
        self.store.select("one", THREAD_A)
        second.select("two", THREAD_B)
        self.assertEqual(second.selection("one"), THREAD_A)
        self.assertEqual(self.store.selection("two"), THREAD_B)

    def test_post_write_card_failure_is_unknown_not_offline_rejection(self):
        card = self.send('/use ' + THREAD_A)
        with patch.object(self.store, 'reference', side_effect=OSError('disk-error')):
            reply = self.send('思考档位 high', quote=card)
        self.assertIn('可能已经执行', reply)
        self.assertEqual(len(self.backend.updates), 1)

    def test_state_store_failure_never_replays_native_operation(self):
        card = self.send('/use ' + THREAD_A)
        with patch.object(self.store, 'begin', side_effect=sqlite3.OperationalError('locked')):
            self.assertIn('未执行', self.send('继续', quote=card))
        self.assertEqual(self.calls, [])
        with patch.object(self.store, 'finish', side_effect=sqlite3.OperationalError('locked')):
            self.assertIn('未能保存', self.send('继续', quote=card, message_id='claimed'))
        self.send('继续', quote=card, message_id='claimed')
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
