from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

import test_control_router as fixture
from test_control_router import FakeBackend, THREAD_A, THREAD_B
from desktop_control import DesktopControlError

CHILD = '01a10000-0000-7000-8000-000000000003'


class Backend(FakeBackend):
    def __init__(self, catalog):
        super().__init__()
        self.catalog, self.steered, self.forked = catalog, [], []
        self.steer_state, self.fork_state = 'confirmed', 'confirmed'
        self.baseline = {'boundary': {'turn_id': 'turn', 'completed_at': 100, 'updated_at': 100},
                         'owner_id': 'owner', 'settings_digest': 'digest'}

    def steer(self, thread, text, connection, operation):
        self.steered.append((thread, text, connection, operation))
        return {'state': self.steer_state, 'reply': '引导已接受' if self.steer_state == 'confirmed' else '引导未知；不重试'}

    def fork_boundary(self, thread, connection):
        return copy.deepcopy(self.baseline)

    def fork_latest(self, thread, baseline, connection, operation):
        self.forked.append((thread, baseline, connection, operation))
        if self.fork_state == 'confirmed':
            self.catalog.rows[CHILD] = {'id': CHILD, 'name': '新分支', 'project_name': '乙'}
        return {'state': self.fork_state, 'thread_id': CHILD,
                'reply': '已分支，没有发送新任务', 'settings': self.settings, 'title': '新分支'}


class CommandTests(unittest.TestCase):
    request = fixture.ControlRouterTests.request
    send = fixture.ControlRouterTests.send

    def setUp(self):
        fixture.ControlRouterTests.setUp(self)
        self.backend = Backend(self.controller.catalog)
        self.controller.backend = self.backend

    def test_steer_requires_quote_and_nonempty_body_without_ordinary_send(self):
        card = self.send('/use ' + THREAD_B)
        self.assertIn('没有引用目标', self.send('引导 继续检查'))
        self.assertIn('请提供', self.send('引导', quote=card))
        reply = self.send('引导 中文😀\n先看接口', quote=card)
        self.assertIn('执行时', reply)
        self.assertEqual(self.backend.steered[0][:3], (THREAD_B, '中文😀\n先看接口', 'same-process'))
        self.assertEqual(self.calls, [])

    def test_unknown_steer_and_duplicate_event_do_not_repeat_or_queue(self):
        self.backend.steer_state = 'unknown'
        card = self.send('/use ' + THREAD_B)
        request = self.request('引导 中文', quote=card, message_id='steer-event')
        reply = self.controller.handle(request)
        self.assertEqual(self.controller.handle(request), reply)
        self.assertIn('不重试', reply)
        self.assertEqual(len(self.backend.steered), 1)
        self.assertEqual(self.calls, [])

    def test_fork_requires_confirmation_and_distinct_child_quote_without_task(self):
        card = self.send('/use ' + THREAD_B)
        confirm = self.send('分支', quote=card)
        self.assertIn('保留当前全部历史', confirm)
        self.assertIn('不自动发送任务', confirm)
        self.assertEqual(self.backend.forked, [])
        self.assertIn('完整', self.send('确认', quote=confirm))
        reply = self.send('确认分支', quote=confirm)
        self.assertIn('对话编号：' + CHILD, reply)
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.backend.forked), 1)
        self.assertEqual(self.backend.forked[0][0], THREAD_B)
        self.send('继续', quote=reply)
        self.assertEqual(self.calls, [(CHILD, '继续')])

    def test_cancel_closes_confirmation_and_repeated_confirm_returns_same_receipt(self):
        card = self.send('/use ' + THREAD_B)
        cancelled = self.send('分支', quote=card)
        self.send('取消', quote=cancelled)
        self.assertIn('已取消', self.send('确认分支', quote=cancelled))
        self.assertEqual(self.backend.forked, [])
        confirm = self.send('分支', quote=card)
        first = self.send('确认分支', quote=confirm)
        self.assertEqual(self.send('确认分支', quote=confirm), first)
        self.assertEqual(len(self.backend.forked), 1)

    def test_unknown_fork_does_not_open_send_card_or_repeat(self):
        self.backend.fork_state = 'unknown'
        confirm = self.send('分支', quote=self.send('/use ' + THREAD_B))
        reply = self.send('确认分支', quote=confirm)
        self.assertIn('分支编号：', reply)
        self.assertNotIn('对话编号：', reply)
        self.assertIn('不会猜测', self.send('继续', quote=reply))
        self.send('确认分支', quote=confirm)
        self.assertEqual(len(self.backend.forked), 1)
        self.assertEqual(self.calls, [])

    def test_unavailable_boundary_earlier_position_and_wrong_connection_never_fork(self):
        card = self.send('/use ' + THREAD_B)
        self.assertIn('本轮只支持', self.send('分支 第3条', quote=card))
        confirm = self.send('分支', quote=card)
        with patch.object(self.backend, 'probe', return_value=('changed-process', 1.0)):
            self.assertIn('旧连接', self.send('确认分支', quote=confirm))
        with patch.object(self.backend, 'fork_boundary', side_effect=DesktopControlError('unavailable')):
            self.assertIn('空闲', self.send('分支', quote=card))
        self.assertEqual(self.backend.forked, [])

    def test_changed_source_project_rejects_confirmation_without_fork(self):
        confirm = self.send('分支', quote=self.send('/use ' + THREAD_B))
        self.controller.catalog.rows[THREAD_B]['project_name'] = '改了归属'
        self.assertIn('项目归属已变化', self.send('确认分支', quote=confirm))
        self.assertEqual(self.backend.forked, [])

    def test_child_project_mismatch_after_creation_is_unknown_without_send_card(self):
        confirm = self.send('分支', quote=self.send('/use ' + THREAD_B))
        fork = self.backend.fork_latest
        def mismatch(*args):
            result = fork(*args)
            self.controller.catalog.rows[CHILD]['project_name'] = '错误归属'
            return result
        with patch.object(self.backend, 'fork_latest', side_effect=mismatch):
            reply = self.send('确认分支', quote=confirm)
        self.assertIn('归属没有核对一致', reply)
        self.assertNotIn('对话编号：', reply)
        self.assertEqual(len(self.backend.forked), 1)
        self.assertEqual(self.calls, [])

    def test_offline_steer_and_fork_are_discarded_not_replayed(self):
        card = self.send('/use ' + THREAD_A)
        self.backend.online = False
        for text in ('引导 内容', '分支'):
            event = self.request(text, quote=card)
            self.assertIn('作废', self.controller.handle(event))
            self.backend.online = True
            self.assertIn('作废', self.controller.handle(event))
            self.backend.online = False
        self.assertEqual(self.backend.steered, [])
        self.assertEqual(self.backend.forked, [])
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
