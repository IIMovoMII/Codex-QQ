from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/codex_desktop_weixin'))
from mobile_copy import HELP_PAGES, help_text, notification_heading
from models import Confidence, Notification, TurnState
from notifier import format_notification
import test_control_router as router_tests


class MobileCopyTests(unittest.TestCase):
    def test_pages_fit_a_mobile_message_without_markdown_or_ids(self):
        for topic, text in HELP_PAGES.items():
            with self.subTest(topic=topic):
                self.assertLessEqual(len(text), 480)
                self.assertLessEqual(max(map(len, text.splitlines())), 70)
                self.assertNotIn('**', text)
                self.assertNotIn('对话编号：', text)
                self.assertEqual(text.encode('utf-8').decode('utf-8'), text)
        self.assertIn('①', help_text())
        self.assertIn('首页', help_text('未知'))

    def test_notification_only_decorates_heading_not_model_reply_or_footer(self):
        body = '原始回答：\n请运行 example --help。\n😀 已有表情原样保留。'
        for state, icon in ((TurnState.COMPLETED, '✅'), (TurnState.FAILED, '⚠️'),
                            (TurnState.INTERRUPTED, '⏸️')):
            note = Notification('synthetic-thread-0001', 'turn', state, Confidence.CERTAIN,
                                '【示例】任务状态', body)
            rendered = format_notification(note)
            self.assertTrue(rendered.startswith(icon + ' '))
            self.assertIn('\n\n' + body + '\n\n', rendered)
            self.assertTrue(rendered.endswith('对话编号：synthetic-thread-0001'))
        self.assertEqual(notification_heading('✅ 已完成', 'completed'), '✅ 已完成')


class HelpRoutingTests(unittest.TestCase):
    setUp = router_tests.ControlRouterTests.setUp
    request = router_tests.ControlRouterTests.request
    send = router_tests.ControlRouterTests.send

    def test_help_topics_and_quoted_help_never_submit_a_task(self):
        card = self.send('1', quote=self.send('对话'))
        for text in ('帮助', '帮助 任务', '帮助\t设置', '/help 对话', '帮助 不存在'):
            answer = self.send(text, quote=card)
            self.assertLess(len(answer), 550)
            self.assertIn('帮助', answer)
        self.assertEqual(self.calls, [])

    def test_help_on_an_operation_card_does_not_confirm_or_answer_it(self):
        card = self.controller._card('test|qqbot:user|user', 'creation_prompt', {}, '测试卡片')
        self.assertIn('QQ 控制', self.send('帮助', quote=card))
        self.assertEqual(self.calls, [])
