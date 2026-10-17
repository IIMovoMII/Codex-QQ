from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src' / 'codex_desktop_weixin'))
import test_control_router as fixture
from test_control_router import FakeBackend, THREAD_A, THREAD_B

CHILD = '01a10000-0000-7000-8000-000000000003'


class CreateBackend(FakeBackend):
    def __init__(self, catalog):
        super().__init__()
        self.catalog = catalog
        self.created, self.renamed = [], []
        self.create_state = 'confirmed'
        self.projects = [{'id': 'project-a', 'name': '甲', 'path': 'D:/test'}]

    def creation_projects(self, source, connection):
        return copy.deepcopy(self.projects)

    def create_thread(self, source, project, prompt, title, connection, operation):
        self.created.append((source, project, prompt, title, connection, operation))
        if self.create_state == 'unknown':
            return {'state': 'unknown', 'reply': '结果未知，可能已创建；不会重试。'}
        self.catalog.rows[CHILD] = {'id': CHILD, 'name': title or '新标题',
                                   'project_id': project['id'], 'project_name': project['name'] if project['id'] else None}
        return {'state': 'confirmed', 'thread_id': CHILD, 'title': title or '新标题',
                'reply': '已创建并接受首条任务', 'settings': {'model': 'gpt-test', 'effort': 'max'}}

    def rename(self, thread, title, connection, operation):
        self.renamed.append((thread, title, connection))
        self.catalog.rows[thread]['name'] = title
        return {'state': 'confirmed', 'reply': '已改名', 'title': title}


class CreationTests(unittest.TestCase):
    request = fixture.ControlRouterTests.request
    send = fixture.ControlRouterTests.send

    def setUp(self):
        fixture.ControlRouterTests.setUp(self)
        self.backend = CreateBackend(self.controller.catalog)
        self.controller.backend = self.backend

    def wizard(self, prompt='首条任务', index='2'):
        source = self.send('/use ' + THREAD_A)
        projects = self.send('新建对话 手机新任务', quote=source)
        entry = self.send(index, quote=projects)
        confirm = self.send(prompt, quote=entry)
        return source, projects, entry, confirm

    def test_new_conversation_requires_explicit_real_origin_not_old_selection(self):
        self.send('/use ' + THREAD_A)
        self.assertIn('请引用', self.send('新建对话'))
        self.assertEqual(self.backend.created, [])

    def test_wizard_is_readonly_until_full_confirmation_then_new_card_targets_child(self):
        source, projects, entry, confirm = self.wizard('暂停😀\n不把这句当成命令')
        self.assertEqual(self.backend.created, [])
        self.assertIn('还未创建', self.send('确认', quote=confirm))
        reply = self.send('确认创建', quote=confirm)
        self.assertIn(CHILD, reply)
        self.assertIn('原生模型', reply)
        self.assertEqual(len(self.backend.created), 1)
        self.assertEqual(self.backend.created[0][:4], (THREAD_A, self.backend.projects[0], '暂停😀\n不把这句当成命令', '手机新任务'))
        self.send('继续', quote=reply)
        self.assertEqual(self.calls, [(CHILD, '继续')])
        self.assertNotIn(CHILD, source)

    def test_same_confirmation_and_multiple_prompt_cards_create_only_once(self):
        _, _, entry, confirm = self.wizard()
        another = self.send('修改后的首条任务', quote=entry)
        first = self.send('确认创建', quote=confirm)
        self.assertEqual(self.send('确认创建', quote=confirm), first)
        self.assertEqual(self.send('确认创建', quote=another), first)
        self.assertEqual(len(self.backend.created), 1)

    def test_cancel_from_any_wizard_card_invalidates_other_old_cards(self):
        for place in ('projects', 'entry', 'confirm'):
            with self.subTest(place=place):
                _, projects, entry, confirm = self.wizard()
                card = {'projects': projects, 'entry': entry, 'confirm': confirm}[place]
                self.assertIn('已取消', self.send('取消', quote=card))
                self.assertIn('已取消', self.send('确认创建', quote=confirm))
        self.assertEqual(self.backend.created, [])

    def test_unknown_creation_cannot_be_repeated_on_the_same_wizard(self):
        self.backend.create_state = 'unknown'
        _, _, _, confirm = self.wizard()
        self.assertIn('未知', self.send('确认创建', quote=confirm))
        self.send('确认创建', quote=confirm)
        self.assertEqual(len(self.backend.created), 1)

    def test_connection_generation_and_source_disappearance_reject_old_confirmation(self):
        _, _, _, confirm = self.wizard()
        with patch.object(self.backend, 'probe', return_value=('new-desktop', 1.0)):
            self.assertIn('旧连接', self.send('确认创建', quote=confirm))
        self.controller.catalog.rows.pop(THREAD_A)
        self.assertIn('来源已不可用', self.send('确认创建', quote=confirm))
        self.assertEqual(self.backend.created, [])

    def test_projectless_and_long_first_prompt_preserve_text_without_losing_footer(self):
        prompt = '中文😀\n' * 400
        _, _, _, confirm = self.wizard(prompt, index='1')
        self.assertLessEqual(len(confirm), 2000)
        self.assertIn('完整正文', confirm)
        self.assertIn('引用标识：', confirm)
        self.send('确认创建', quote=confirm)
        self.assertIsNone(self.backend.created[0][1]['id'])
        self.assertEqual(self.backend.created[0][2], prompt.strip())

    def test_project_pages_keep_snapshot_numbers_and_same_wizard_identity(self):
        self.backend.projects = [{'id': f'p-{n}', 'name': f'长项目名{n}' + '中' * 70, 'path': f'D:/p{n}'} for n in range(45)]
        source = self.send('/use ' + THREAD_A)
        listing = self.send('新建对话', quote=source)
        self.assertIn('下一页', listing)
        next_page = self.send('下一页', quote=listing)
        self.assertIn('引用标识：', next_page)
        self.assertEqual(self.backend.created, [])

    def test_wrong_quote_type_and_new_project_never_send_to_a_model(self):
        source, projects, entry, confirm = self.wizard()
        for text, card in (('新建对话', projects), ('改名 错误', confirm), ('新建项目', source)):
            self.send(text, quote=card)
        self.assertEqual(self.backend.created, [])
        self.assertEqual(self.backend.renamed, [])
        self.assertEqual(self.calls, [])

    def test_confirmation_on_plain_thread_card_is_not_a_new_task(self):
        card = self.send('/use ' + THREAD_A)
        for text in ('确认创建', '确认分支'):
            self.assertIn('确认卡片', self.send(text, quote=card))
        self.assertEqual(self.backend.created, [])
        self.assertEqual(self.calls, [])

    def test_rename_uses_only_quoted_thread_and_deduplicates_platform_event(self):
        card = self.send('/use ' + THREAD_B)
        self.assertIn('没有引用目标', self.send('改名 手机中文😀'))
        request = self.request('改名 手机中文😀', quote=card, message_id='same-rename')
        first = self.controller.handle(request)
        self.assertEqual(self.controller.handle(request), first)
        self.assertEqual(self.backend.renamed, [(THREAD_B, '手机中文😀', 'same-process')])
        self.send('继续', quote=first)
        self.assertEqual(self.calls, [(THREAD_B, '继续')])


if __name__ == '__main__':
    unittest.main()
