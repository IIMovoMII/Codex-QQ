"""Explicit QQ creation wizard; selecting/filling/cancelling never creates a task."""
from __future__ import annotations

import uuid
from typing import Any

from control_commands import ActionCommands

PROJECTLESS = {'id': None, 'name': '无项目', 'path': None}
KINDS = frozenset({'creation_projects', 'creation_prompt', 'creation_confirm'})


class CreationCommands:
    def __init__(self, router: Any, scope: str, connection: str) -> None:
        self.r, self.scope, self.connection = router, scope, connection

    def card(self, kind: str, data: dict, text: str) -> str:
        return self.r._card(self.scope, kind, data, text, ttl=600)

    def begin(self, source_thread_id: str, title: str) -> tuple[str, str]:
        if title and (len(title) > 120 or any(ord(c) < 32 for c in title)):
            return '可选标题须为不超过 120 字的单行文字。', 'rejected'
        entries = [dict(PROJECTLESS)] + self.r.backend.creation_projects(source_thread_id, self.connection)
        data = {'thread_id': source_thread_id, 'connection': self.connection,
                'title': title or None, 'entries': entries, 'offset': 0, 'wizard_id': uuid.uuid4().hex}
        return self._projects(data), 'confirmed'

    def _projects(self, data: dict) -> str:
        lines, shown = ['为新对话选择项目（此步不会创建或执行任务）'], []
        for row in data['entries']:
            name = row['name'].replace('\n', ' ')[:80]
            line = f"{data['offset'] + len(shown) + 1}. {name}"
            if len('\n'.join(lines)) + len(line) > 1350:
                break
            lines.append(line)
            shown.append(row)
        if not shown:
            raise ValueError('creation-project-list-too-large')
        remaining = data['entries'][len(shown):]
        lines.extend(['引用回复序号；取消则回复“取消”。', '已有对话只作原生调用来源，不会在里面执行新任务。'])
        if remaining:
            lines.append('引用回复“下一页”继续。')
        return self.card('creation_projects', {**data, 'entries': shown, 'remaining': remaining}, '\n'.join(lines))

    def quoted(self, ref: dict, text: str) -> tuple[str, str] | None:
        kind, data = ref['kind'], ref['data']
        if kind not in KINDS:
            return None
        if data.get('connection') != self.connection:
            return '创建向导来自旧连接，已过期。请重新开始；不会补执行。', 'rejected'
        operation_key = 'create:' + data['wizard_id']
        previous = self.r.store.operation(self.scope, operation_key)
        if previous is not None:
            return previous['reply'], previous['state']
        if text == '取消':
            return ActionCommands(self.r, self.scope, self.connection)._once(operation_key,
                    lambda: {'reply': '已取消向导，没有创建对话或发送任务。旧向导卡片不再执行，请重新发送“新建对话”开始。', 'state': 'confirmed'})
        source = data.get('thread_id')
        if not source or not self.r.catalog.thread(source):
            return '原生调用来源已不可用，未创建。请引用一个已有对话重新开始。', 'rejected'
        if kind == 'creation_projects':
            if text == '下一页':
                if not data['remaining']:
                    return '这份项目列表没有下一页。', 'confirmed'
                return self._projects({**data, 'entries': data['remaining'],
                                       'offset': data['offset'] + len(data['entries'])}), 'confirmed'
            index = int(text) - 1 - data['offset'] if text.isdigit() else -1
            if not 0 <= index < len(data['entries']):
                return '请引用这份项目列表回复序号，或回复“取消”。', 'rejected'
            project = data['entries'][index]
            content = f"新对话项目：{project['name']}\n引用本条填写首条任务（最多 4,000 字）。\n还未创建；下一步会展示确认。回复“取消”退出。"
            return self.card('creation_prompt', {'thread_id': source, 'connection': self.connection,
                             'title': data['title'], 'project': project, 'wizard_id': data['wizard_id']}, content), 'confirmed'
        if kind == 'creation_prompt':
            if not text or len(text) > 4000:
                return '首条任务不能为空，最多 4,000 字；引用本条重新填写。', 'rejected'
            text.encode('utf-8', errors='strict')
            content = f"准备创建新对话\n项目：{data['project']['name']}\n标题：{data['title'] or '由 Codex 生成'}\n首条任务{'摘要' if len(text) > 850 else ''}：\n{text[:850]}"
            content += ('\n（这里只裁剪显示，提交时保留你填写的完整正文。）' if len(text) > 850 else '')
            content += '\n\n确认后立即创建并执行首条任务，会消耗模型额度。模型/档位/权限由原生入口解析，可能包含项目默认与来源权限继承；本程序不额外覆盖。\n引用回复“确认创建”才执行；回复“取消”不执行。'
            return self.card('creation_confirm', {**data, 'prompt': text,
                             'operation_id': data['wizard_id']}, content), 'confirmed'
        if text != '确认创建':
            return '还未创建。请引用确认卡片回复完整的“确认创建”，或回复“取消”。', 'rejected'
        actions = ActionCommands(self.r, self.scope, self.connection)
        return actions._once(operation_key, lambda: self._create(data))

    def _create(self, data: dict) -> dict:
        result = self.r.backend.create_thread(data['thread_id'], data['project'], data['prompt'],
                                             data['title'], self.connection, data['operation_id'])
        ident = result.get('thread_id')
        if not ident:
            return result
        # Never fabricate a title/project from a path. The confirmed native target
        # and user selection stay visible even if the local list has not refreshed.
        row = self.r.catalog.thread(ident)
        if row is None:
            row = {'id': ident, 'name': result.get('title') or data['title'] or '新对话（标题待刷新）',
                   'project_name': data['project']['name'] if data['project']['id'] else None}
        elif row.get('project_id') != data['project']['id']:
            result = {**result, 'state': 'unknown', 'reply': '新对话已创建，但本地项目归属还未核实一致。请在电脑核对，不要重复创建。'}
        body = self.r.catalog.label(row) + '\n' + result['reply']
        settings = result.get('settings')
        if settings and settings.get('model') and settings.get('effort'):
            body += f"\n原生模型：{settings['model']}\n思考档位：{settings['effort']}"
        else:
            body += '\n设置尚未回读；引用本条回复“设置”核对实际模型、档位和权限。'
        body += '\n每次引用本条发送消息，操作的是新对话，不是原来的调用来源。'
        return {**result, 'reply': self.r._card(self.scope, 'thread', {'thread_id': ident, 'connection': self.connection}, body)}
