"""Quoted native actions separated from ordinary text submission.

The router owns identity/freshness/deduplication; this layer never guesses a
target, launches a process, rewrites history, or retries a native mutation.
"""
from __future__ import annotations

from typing import Any
import uuid

from control_actions import SUPPORTED_REQUESTS, describe_request, request_reference, signature


class ActionCommands:
    def __init__(self, router: Any, scope: str, connection: str) -> None:
        self.r, self.scope, self.connection = router, scope, connection

    def card(self, kind: str, data: dict, text: str, ttl: float | None = 600) -> str:
        return self.r._card(self.scope, kind, data, text, ttl=ttl)

    def _once(self, key: str, action: Any) -> tuple[str, str]:
        old = self.r.store.claim_operation(self.scope, key)
        if old is not None:
            return old['reply'], old['state']
        try:
            result = action()
            reply, state = result['reply'], result['state']
        except Exception:
            reply, state = '操作处理结果未知，不会自动重发。请重新查询或在电脑核对。', 'unknown'
        self.r.store.finish_operation(self.scope, key, state, reply)
        return reply, state

    def command(self, thread_id: str, command: str, arg: str) -> tuple[str, str] | None:
        if command == '引导':
            if not arg:
                return '请提供引导正文，例如“引导 先检查登录问题”。没有活动任务时不会自动排队。', 'rejected'
            result = self.r.backend.steer(thread_id, arg, self.connection, str(uuid.uuid4()))
            if result['state'] == 'rejected':
                return result['reply'], 'rejected'
            row = self.r.catalog.thread(thread_id)
            body = (self.r.catalog.label(row) if row else '原对话') + '\n' + result['reply']
            body += '\n按你选择的原生规则，引导执行时的活动任务；不锁定先前查询到的轮次。'
            return self.r._card(self.scope, 'thread', {'thread_id': thread_id}, body), result['state']
        if command == '分支':
            if arg:
                return '本轮只支持最近已完成位置。请单独发送“分支”查看范围；不会忽略你指定的更早位置。', 'rejected'
            try:
                baseline = self.r.backend.fork_boundary(thread_id, self.connection)
            except (ValueError, OSError, TimeoutError):
                return '当前对话或分支边界无法核实，未分支；请恢复连接后重试。', 'rejected'
            except Exception:
                return '目前只允许空闲、最近一轮已完成且无自动目标的对话分支。原生边界尚无法核实时，请先在电脑打开并核对对话。', 'rejected'
            row = self.r.catalog.thread(thread_id)
            stamp = baseline['boundary'].get('completed_at')
            from datetime import datetime
            try:
                when = datetime.fromtimestamp(stamp).astimezone().strftime('%Y-%m-%d %H:%M:%S') if isinstance(stamp, (int, float)) and not isinstance(stamp, bool) and 0 < stamp < 100_000_000_000 else '原生未提供时间'
            except (OSError, OverflowError, ValueError):
                when = '原生未提供有效时间'
            body = f"{self.r.catalog.label(row) if row else '原对话'}\n分支范围：保留当前全部历史，直到最近已完成的一轮（{when}）。\n目前不能选择更早位置；原对话不变，分支不自动发送任务。\n引用回复“确认分支”执行；回复“取消”不创建。"
            return self.card('fork_confirm', {'thread_id': thread_id, 'baseline': baseline,
                            'source_project': self._project_identity(row),
                            'connection': self.connection, 'operation_id': uuid.uuid4().hex}, body), 'confirmed'
        if command == '改名':
            if not arg:
                return '请提供新标题，例如“改名 检查登录”；标题最多 120 字。', 'rejected'
            result = self.r.backend.rename(thread_id, arg, self.connection, uuid.uuid4().hex)
            if result['state'] == 'rejected':
                return result['reply'], 'rejected'
            row = self.r.catalog.thread(thread_id)
            prefix = self.r.catalog.label({**row, 'name': result.get('title') or row['name']}) if row else '原对话'
            return self.r._card(self.scope, 'thread', {'thread_id': thread_id},
                                prefix + '\n' + result['reply']), result['state']
        if command == '队列':
            self.r.backend.require_extended_contract()
            self.r.backend.ensure_owner(thread_id)
            entries = self.r.catalog.saved_queue(thread_id)
            if not entries:
                return 'Desktop 已保存的本地排队记录为空。服务端队列尚不能完整查询，不能据此认定所有队列都为空。', 'confirmed'
            return self._list('queue_list', thread_id, entries,
                              '本地已保存的排队记录（可能不含服务端队列；状态以原生处理为准）'), 'confirmed'
        if command in ('待办', '问题', '审批'):
            state = self.r.backend.control_state(thread_id)
            entries, unsupported = [], int(bool(state.get('async_questions_unverified')))
            for request in state['requests'][:30]:
                try:
                    data = request_reference(thread_id, request, state['owner_id'], self.connection)
                    label = SUPPORTED_REQUESTS[request['method']]
                    detail = describe_request(request)
                except (ValueError, KeyError, TypeError):
                    unsupported += 1
                    continue
                entries.append({**data, 'summary': label + '：' + detail.splitlines()[0][:100]})
            for batch in state.get('async_questions', []):
                entries.append({'kind': 'async_question', 'thread_id': thread_id,
                                'owner_id': state['owner_id'], 'connection': self.connection,
                                **{k: batch[k] for k in ('message_id', 'turn_id', 'digest')},
                                'summary': '异步问题（仅查看）：' + batch['questions'][batch['indices'][0]]['title'][:90]})
            if not entries:
                return '没有可通过 QQ 回答的待办。' + ('未知请求、身份验证、权限授权或登录问题请在电脑处理。' if unsupported else '原请求可能已经处理或结束。'), 'confirmed'
            note = '当前待办；引用回复序号打开问题或审批。'
            if unsupported:
                note += '\n另有不支持的请求，请在电脑处理。'
            return self._list('request_list', thread_id, entries, note), 'confirmed'
        if command == '权限':
            state = self.r.backend.control_state(thread_id)
            effective = self._permissions(state)
            profile = effective.get('activePermissionProfile')
            if profile and (not isinstance(profile, dict) or not str(profile.get('id', '')).startswith(':')):
                return '此对话使用自定义权限预设，请在电脑修改；QQ 不会用通用设置覆盖它。', 'rejected'
            choices = [
                {'name': '只读', 'changes': {'sandboxPolicy': {'type': 'readOnly'}, 'approvalPolicy': 'on-request', 'approvalsReviewer': 'user', 'activePermissionProfile': None}},
                {'name': '完全访问', 'changes': {'sandboxPolicy': {'type': 'dangerFullAccess'}, 'approvalPolicy': 'never', 'approvalsReviewer': 'user', 'activePermissionProfile': None}},
            ]
            # Reuse actual native workspace policy; never invent writable roots.
            sandbox = effective.get('sandboxPolicy') or {}
            if sandbox.get('type') == 'workspaceWrite':
                choices.insert(1, {'name': '保留当前工作区写入范围', 'changes': {
                    'sandboxPolicy': sandbox, 'approvalPolicy': 'on-request',
                    'approvalsReviewer': 'user', 'activePermissionProfile': None}})
            lines = ['权限选项（仅此对话后续轮次）', '当前：' + self.permission_label(effective)]
            lines.extend(f"{i}. {x['name']}" for i, x in enumerate(choices, 1))
            lines.append('引用回复序号；下一步会展示改动并要求明确确认。')
            data = {'thread_id': thread_id, 'choices': choices, 'baseline': {
                'digest': signature(effective), 'owner_id': state['owner_id']}, 'connection': self.connection}
            return self.card('permission_options', data, '\n'.join(lines)), 'confirmed'
        if command == '压缩上下文':
            if arg:
                return '请单独发送“压缩上下文”。', 'rejected'
            result = self.r.backend.compact(thread_id)
            if result['state'] == 'rejected':
                return result['reply'], 'rejected'
            data = {'thread_id': thread_id, 'baseline': result.get('baseline', []),
                    'owner_id': result.get('owner_id'), 'connection': self.connection}
            return self.card('compaction', data, result['reply']), result['state']
        return None

    @staticmethod
    def _permissions(state: dict) -> dict:
        fields = ('sandboxPolicy', 'approvalPolicy', 'permissions', 'activePermissionProfile', 'approvalsReviewer')
        return {**(state.get('permissions') or {}), **{k: v for k, v in (state.get('settings') or {}).items() if k in fields}}

    @staticmethod
    def permission_label(value: dict) -> str:
        label = {'readOnly': '只读', 'workspaceWrite': '工作区写入', 'dangerFullAccess': '完全访问'}.get((value.get('sandboxPolicy') or {}).get('type'), '当前原生权限')
        return label + '；审批：' + str(value.get('approvalPolicy') or '原生默认')

    def _list(self, kind: str, thread_id: str, entries: list[dict], title: str, offset: int = 0) -> str:
        lines, shown = [title], []
        for entry in entries:
            suffix = ' · ' + self.queue_status(entry['status']) if kind == 'queue_list' else ''
            line = f"{offset + len(shown) + 1}. {entry['summary']}{suffix}"
            if len('\n'.join(lines)) + len(line) > 1450:
                break
            lines.append(line)
            shown.append(entry)
        if not shown:
            raise ValueError('action-list-too-large')
        lines.append('引用回复序号选择；需要目标对话时仍引用原对话卡片。')
        remaining = entries[len(shown):]
        if remaining:
            lines.append('引用回复“下一页”继续。')
        return self.card(kind, {'thread_id': thread_id, 'entries': shown, 'remaining': remaining,
                              'offset': offset, 'title': title}, '\n'.join(lines))

    def quoted(self, ref: dict, text: str) -> tuple[str, str] | None:
        kind, data = ref['kind'], ref['data']
        thread_id = data.get('thread_id')
        if kind not in ('queue_list', 'queued', 'submission', 'request_list', 'request', 'permission_options', 'permission_confirm', 'compaction', 'fork_confirm', 'async_notice', 'async_question'):
            return None
        if not self.r.catalog.thread(thread_id):
            return '目标对话已不在可控制列表；未操作。', 'rejected'
        if kind == 'submission':
            if text == '对话':
                return self.r._thread_card(self.scope, self.r.catalog.thread(thread_id)), 'confirmed'
            if text in ('状态', '队列'):
                from submission_receipts import status_text
                observed = self.r.backend.submission_status(data, self.r.catalog, self.connection)
                updated = {**data, 'client_id': observed.get('client_id')}
                return self.card('submission', updated, status_text(observed)
                                 + '\n引用回复“状态”继续查看本条，或“删除”移除本条排队消息。'
                                 + '\n引用回复“对话”取得普通对话回执；此卡片不发送正文或转引导。', ttl=None), 'confirmed'
            if text == '删除':
                key = 'submission-delete:' + data['connection'] + ':' + thread_id + ':' + data['queue_id'] + ':' + data['text_digest']
                return self._once(key, lambda: self.r.backend.remove_submission(data, self.r.catalog, self.connection))
            return '本条回执请回复“状态”“删除”或“对话”。排队转引导尚未开放，原消息保留；没有再次提交正文。', 'rejected'
        if kind == 'async_notice':
            if text in ('待办', '问题', '审批'):
                return self.command(thread_id, '待办', '')
            return '这条是异步提问提醒，不是普通任务回执；答案请在电脑提交。引用回复“待办”可查看仍有效的问题，没有发送模型任务。', 'rejected'
        if kind in ('queue_list', 'request_list'):
            if text == '下一页':
                if not data['remaining']:
                    return '这份列表没有下一页。', 'confirmed'
                return self._list(kind, thread_id, data['remaining'], data['title'], data['offset'] + len(data['entries'])), 'confirmed'
            index = int(text) - 1 - data['offset'] if text.isdigit() else -1
            if not 0 <= index < len(data['entries']):
                return '请引用这份列表回复其中一个序号。', 'rejected'
            entry = data['entries'][index]
            if kind == 'queue_list':
                return self.card('queued', {'thread_id': thread_id, 'entry': entry},
                                 f"待发送记录：{entry['summary']}\n状态：{self.queue_status(entry['status'])}\n引用回复“删除”移除这一条。转引导尚无安全原子通路，未开放；不支持修改。"), 'confirmed'
            state = self.r.backend.control_state(thread_id)
            if entry.get('kind') == 'async_question':
                return self._async_card(entry, state)
            request = self._find_request(entry, state)
            if request is None:
                return '原问题或审批已过期，请重新查看待办。', 'rejected'
            return self.card('request', entry, describe_request(request)), 'confirmed'
        if kind == 'queued':
            if text != '删除':
                return '这条卡片只可回复“删除”。转引导没有可靠通路，未开放，原消息保留；正文请引用对话卡片发送。', 'rejected'
            entry = data['entry']
            return self._once('queue-delete:' + thread_id + ':' + entry['id'] + ':' + entry['digest'],
                              lambda: self.r.backend.remove_saved_queue_item(thread_id, entry, self.r.catalog))
        if data.get('connection') != self.connection:
            return '这项操作来自旧连接，已过期。请重新查看对应选项或待办。', 'rejected'
        if kind == 'async_question':
            state = self.r.backend.control_state(thread_id)
            current = self._async_find(data, state)
            if current is None:
                return '这批异步问题已回答、结束或发生变化，请重新查看待办；未发送新任务。', 'rejected'
            if text in ('待办', '问题'):
                return self._async_card(data, state)
            return '这批是异步问题，请在电脑回答；按你的选择，QQ 只提醒和查看。没有当作正文、引导或新任务发送。', 'rejected'
        if kind == 'fork_confirm':
            key = 'fork:' + data['operation_id']
            if text == '取消':
                return self._once(key, lambda: {'state': 'confirmed', 'reply': '已取消分支，旧确认卡片不再执行。原对话未修改。'})
            if text != '确认分支':
                return '请引用卡片回复完整的“确认分支”，或回复“取消”。', 'rejected'
            return self._once(key, lambda: self._fork(data))
        if kind == 'request':
            # Validate input before claiming: a typo must not consume the request.
            state = self.r.backend.control_state(thread_id)
            request = self._find_request(data, state)
            if request is None:
                return '原问题或审批已处理、过期或变化，未发起新任务。', 'rejected'
            from control_actions import request_payload
            try:
                request_payload(request, text)
            except ValueError as error:
                return str(error), 'rejected'
            key = 'request:' + self.connection + ':' + data['owner_id'] + ':' + str(data['request_id']) + ':' + data['digest']
            return self._once(key, lambda: self.r.backend.respond_to_request(data, text))
        if kind == 'permission_options':
            if text == '取消':
                return '没有修改权限。', 'confirmed'
            index = int(text) - 1 if text.isdigit() else -1
            if not 0 <= index < len(data['choices']):
                return '请引用回复序号，或回复“取消”。', 'rejected'
            choice = data['choices'][index]
            body = f"将改为：{choice['name']}。仅影响此对话后续轮次，不改全局配置。\n"
            if choice['name'] == '完全访问':
                body += '完全访问允许模型操作本机文件并执行命令，不再等待普通审批。\n'
            body += f"引用回复“确认 {choice['name']}”才执行；回复“取消”不修改。"
            return self.card('permission_confirm', {**data, 'choice': choice, 'operation_id': uuid.uuid4().hex}, body), 'confirmed'
        if kind == 'permission_confirm':
            choice = data['choice']
            if text == '取消':
                return '没有修改权限。', 'confirmed'
            if text != '确认 ' + choice['name']:
                return '未修改，请按卡片上的完整确认文字回复。', 'rejected'
            return self._once('permission:' + data['operation_id'],
                              lambda: self.r.backend.update_permissions(thread_id, choice['changes'], data['baseline']))
        if kind == 'compaction':
            if text != '压缩状态':
                return '本条用于查询压缩结果，请引用回复“压缩状态”；不会自动重复压缩。', 'rejected'
            state = self.r.backend.control_state(thread_id)
            if state['owner_id'] != data['owner_id']:
                return '窗口所有者已变化，压缩结果未确认。请在电脑检查。', 'unknown'
            previous = {(x.get('id'), x.get('turn_id')) for x in data['baseline']}
            fresh = [x for x in state['compactions'] if x.get('id') and (x.get('id'), x.get('turn_id')) not in previous and x.get('source') == 'manual']
            if fresh and fresh[-1].get('turn_status') == 'completed':
                return '看到新的原生手动压缩记录且对应轮次已完成；原生入口没有操作编号，不能严格证明是本次 QQ 压缩的结果。请结合电脑状态确认。', 'unknown'
            if fresh and fresh[-1].get('turn_status') in ('failed', 'interrupted'):
                return '原生压缩失败或被中断，请在电脑查看错误；未自动修历史或重试。', 'confirmed'
            return '尚未取得新的手动压缩终态记录；不把等待时间当作成功。请稍后查询或查看电脑。', 'unknown'
        return None

    def _async_find(self, data: dict, state: dict) -> dict | None:
        if data.get('connection') != self.connection or state.get('owner_id') != data.get('owner_id'):
            return None
        found = [x for x in state.get('async_questions', []) if all(x.get(k) == data.get(k)
                 for k in ('message_id', 'turn_id', 'digest'))]
        return found[0] if len(found) == 1 else None

    def _async_card(self, data: dict, state: dict) -> tuple[str, str]:
        from async_questions import describe
        batch = self._async_find(data, state)
        if batch is None:
            return '原异步问题已回答、结束或变化，请重新查看待办。', 'rejected'
        body = '异步问题（仅查看，不会暂停当前任务）\n' + describe(batch['questions'], batch['indices'])
        body += '\n\n请回电脑回答，QQ 只提醒和查看。直接回复此卡片不会发送新任务；引用回复“待办”可重新核对。'
        return self.card('async_question', data, body), 'confirmed'

    def _fork(self, data: dict) -> dict:
        source = self.r.catalog.thread(data['thread_id'])
        if source is None or self._project_identity(source) != data['source_project']:
            return {'state': 'rejected', 'reply': '源对话的项目归属已变化或无法核实，未分支。请重新查看分支范围。'}
        result = self.r.backend.fork_latest(data['thread_id'], data['baseline'],
                                            self.connection, data['operation_id'])
        ident = result.get('thread_id')
        if ident and result['state'] == 'confirmed':
            row = self.r.catalog.thread(ident)
            after_source = self.r.catalog.thread(data['thread_id'])
            if (row is None or after_source is None or self._project_identity(row) != data['source_project']
                    or self._project_identity(after_source) != data['source_project']):
                result = {**result, 'state': 'unknown',
                          'reply': '分支已创建，但新旧对话的项目归属没有核对一致。请先在电脑核对，不要重复分支。'}
        if not ident or result['state'] != 'confirmed':
            return {**result, 'reply': result['reply'] + (f'\n分支编号：{ident}\n请先在电脑核对，再从“对话”列表选择；本条不自动开放任务发送。' if ident else '')}
        body = self.r.catalog.label(row) + '\n' + result['reply']
        settings = result.get('settings') or {}
        body += f"\n原生继承设置：{settings.get('model') or '待查看'} / {settings.get('effort') or '待查看'}"
        body += '\n引用本条发送首条新任务，操作的是分支对话。'
        return {**result, 'reply': self.r._card(self.scope, 'thread', {'thread_id': ident}, body)}

    @staticmethod
    def _project_identity(row: dict | None) -> dict:
        return {k: (row or {}).get(k) for k in ('project_id', 'project_name')}

    def _find_request(self, data: dict, state: dict) -> dict | None:
        if data.get('connection') != self.connection or state.get('owner_id') != data['owner_id']:
            return None
        found = [x for x in state['requests'] if isinstance(x, dict) and x.get('id') == data['request_id']
                 and x.get('method') == data['method']]
        return found[0] if len(found) == 1 and signature(found[0]) == data['digest'] else None

    @staticmethod
    def queue_status(value: str) -> str:
        return {'queued': '待发送', 'pending': '准备发送', 'sending': '正在提交',
                'outcome-unknown': '提交结果未知'}.get(value, '原生状态尚未识别')
