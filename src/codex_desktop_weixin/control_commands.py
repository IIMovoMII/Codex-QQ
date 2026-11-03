"""Quoted native actions separated from ordinary text submission.

The router owns identity/freshness/deduplication; this layer never guesses a
target, launches a process, rewrites history, or retries a native mutation.
"""
from __future__ import annotations

from typing import Any
import uuid
import inspect

from control_actions import SUPPORTED_REQUESTS, describe_request, request_reference, signature
from desktop_control import DesktopControlError, public_error_code


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
            if result.get('retryable') and state == 'rejected':
                self.r.store.release_operation(self.scope, key)
                return reply, state
        except Exception as error:
            action = key.split(':', 1)[0]
            label = {'submission-delete': '删除已发送回执对应的队列项',
                     'queue-delete': '删除已选队列项', 'queue-guide': '先删除再引导',
                     'request': '回答原生问题或审批', 'fork': '创建分支',
                     'permission-flow': '修改当前对话权限'}.get(action, '执行这张操作卡')
            reply = (f'{label}时本机发生 {public_error_code(error)}；原生请求是否已到达无法从回执判定。'
                     '不会自动重试。请先在目标对话核对实际状态，再决定是否发起新操作。')
            state = 'unknown'
        self.r.store.finish_operation(self.scope, key, state, reply)
        return reply, state

    def _backend_call(self, name: str, *args: Any, connection: str | None = None) -> Any:
        """Keep small test/delivery adapters source-compatible while the
        production backend receives the connection generation at the IPC
        boundary."""
        method = getattr(self.r.backend, name)
        parameters = {}
        try:
            parameters = inspect.signature(method).parameters
            supports_connection = ('connection' in parameters or 'expected_connection' in parameters
                                   or any(p.kind is inspect.Parameter.VAR_POSITIONAL
                                          for p in parameters.values()))
        except (TypeError, ValueError):
            supports_connection = True
        if connection is not None and supports_connection:
            if 'expected_connection' in parameters and 'connection' not in parameters:
                return method(*args, expected_connection=connection)
            return method(*args, connection)
        return method(*args)

    @staticmethod
    def _complete_snapshot_guard(state: dict, what: str = '操作') -> tuple[str, str] | None:
        if what == '权限':
            from control_actions import missing_permission_fields
            fields = missing_permission_fields(state)
            if (state.get('snapshot_complete') is False or fields
                    or not state.get('permissions_known', not fields)):
                names = ', '.join(fields) if fields else '权限基线完整性未确认'
                source = ('完整状态帧超过 8 MiB，read_thread 只返回最近一轮摘要'
                          if state.get('snapshot_complete') is False
                          else '完整状态缺少必需字段或未标记为完整权限基线')
                return (f'未生成权限修改卡：{source}；缺少/未确认 {names}。没有调用设置更新接口；请在目标对话设置页核对这些实际值。', 'rejected')
        if what == '待办' and (state.get('snapshot_complete') is False or not state.get('requests_known')):
            missing = ('完整状态帧超过 8 MiB；read_thread 摘要没有原生 request ID/参数'
                       if state.get('snapshot_complete') is False
                       else 'Desktop 状态没有原生 requests 列表')
            return (f'未提交答案：{missing}，无法确认卡片对应的请求仍有效。没有调用答复接口；请在 Desktop 处理该请求。', 'rejected')
        return None

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
            return self.r._card(self.scope, 'thread', {'thread_id': thread_id, 'connection': self.connection}, body), result['state']
        if command == '分支':
            if arg:
                return '本轮只支持最近已完成位置。请单独发送“分支”查看范围；不会忽略你指定的更早位置。', 'rejected'
            try:
                baseline = self.r.backend.fork_boundary(thread_id, self.connection)
            except (ValueError, OSError, TimeoutError):
                return '未创建分支：读取 Desktop 最近完成轮次时连接中断或没有返回数据；没有发送分支请求。检查 Desktop 连接后重新查看“分支”范围。', 'rejected'
            except DesktopControlError as error:
                replies = {
                    'fork-goal-and-request-metadata-unavailable': ('未创建分支：Desktop 完整状态帧超过 8 MiB；read_thread 能确认最近轮次，但不返回 threadGoal 和原生待处理请求列表，'
                                                                  '无法确认源对话没有自动目标或待处理请求。没有创建分支。'),
                    'fork-goal-metadata-unavailable': '未创建分支：Desktop 没有在完整状态中提供 threadGoal 字段，无法确认源对话没有自动目标。',
                    'fork-request-metadata-unavailable': '未创建分支：Desktop 没有返回 requests 列表，无法确认源对话没有待处理审批。没有调用 fork。',
                    'fork-source-has-active-goal': '未创建分支：源对话仍有关联的自动目标；没有创建新对话。',
                    'fork-source-has-unconfirmed-submission': '未创建分支：源对话存在尚未确认结果的提交；没有创建新对话。',
                    'fork-source-has-pending-request': '未创建分支：源对话存在待处理的问题或审批；没有创建新对话。',
                    'fork-source-not-idle-at-completed-turn': '未创建分支：源对话不是“空闲且最近一轮已完成”；没有创建新对话。',
                    'fork-latest-turn-changed-during-read': '未创建分支：读取期间最近完成轮次发生变化；没有创建新对话。请重新查看分支范围。',
                    'latest-fork-source-not-idle-or-has-goal': '未创建分支：源对话仍在运行、存在自动目标或有待处理请求。',
                }
                return replies.get(str(error), f'未创建分支：Desktop 没有确认源对话空闲且最近一轮已完成（校验码 {public_error_code(error)}）。没有调用 fork；请打开目标对话完成恢复后重新查看分支范围。'), 'rejected'
            except Exception as error:
                return f'未创建分支：本机分支预检失败（{type(error).__name__}）；没有创建新对话。请检查助手日志后再试。', 'rejected'
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
            return self.r._card(self.scope, 'thread', {'thread_id': thread_id, 'connection': self.connection},
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
            state = self._backend_call('control_state', thread_id, connection=self.connection)
            incomplete = state.get('snapshot_complete') is False or not state.get('requests_known')
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
                if incomplete:
                    reason = ('read_thread 最近轮次摘要没有 Desktop 原生待办/审批 request ID 和参数'
                              if state.get('snapshot_complete') is False
                              else 'Desktop 完整状态没有原生 requests 列表')
                    return (f'没有发现可显示的异步问题；{reason}，因此不能判断待办为空。没有提交答案；请在 Desktop 查看该对话的待处理请求。', 'rejected')
                if unsupported:
                    return '当前没有可由 QQ 答复的待办；Desktop 另有未支持的请求或无法安全显示的问题内容。没有提交答案，请在电脑查看该对话的待办/审批列表。', 'confirmed'
                return '当前 Desktop 原生 requests 列表为空，没有待答问题或审批。', 'confirmed'
            note = '当前待办；引用回复序号打开问题或审批。'
            if unsupported:
                note += '\n另有不支持的请求，请在电脑处理。'
            if incomplete:
                reason = ('read_thread 摘要未提供' if state.get('snapshot_complete') is False
                          else '完整状态未包含')
                note += f'\n以上只包含最近轮次中可验证的异步问题；{reason} Desktop 原生 requests 列表，不能据此认定没有其他待办。'
            return self._list('request_list', thread_id, entries, note), 'confirmed'
        if command == '权限':
            state = self._backend_call('control_state', thread_id, connection=self.connection)
            guarded = self._complete_snapshot_guard(state, '权限')
            if guarded:
                return guarded
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
            data['operation_id'] = uuid.uuid4().hex
            return self.card('permission_options', data, '\n'.join(lines)), 'confirmed'
        if command == '压缩上下文':
            if arg:
                return '请单独发送“压缩上下文”。', 'rejected'
            result = self._backend_call('compact', thread_id, connection=self.connection)
            if result['state'] == 'rejected':
                return result['reply'], 'rejected'
            data = {'thread_id': thread_id, 'baseline': result.get('baseline', []),
                    'owner_id': result.get('owner_id'), 'connection': self.connection}
            return self.card('compaction', data, result['reply']), result['state']
        return None

    @staticmethod
    def _permissions(state: dict) -> dict:
        from control_actions import permission_values
        return permission_values(state)

    @staticmethod
    def permission_label(value: dict) -> str:
        sandbox = value.get('sandboxPolicy') or {}
        label = {'readOnly': '只读', 'workspaceWrite': '工作区写入', 'dangerFullAccess': '完全访问'}.get(
            sandbox.get('type'), '未返回 sandboxPolicy' if 'sandboxPolicy' not in value else '未识别的 sandboxPolicy.type=' + str(sandbox.get('type')))
        return label + '；审批：' + str(value.get('approvalPolicy') or '未返回 approvalPolicy')

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
                              'offset': offset, 'title': title, 'connection': self.connection}, '\n'.join(lines))

    def quoted(self, ref: dict, text: str) -> tuple[str, str] | None:
        kind, data = ref['kind'], ref['data']
        thread_id = data.get('thread_id')
        if kind not in ('queue_list', 'queued', 'submission', 'request_list', 'request', 'permission_options', 'permission_confirm', 'compaction', 'fork_confirm', 'async_notice', 'async_question'):
            return None
        # A notification footer predates control-card connection tokens.  It
        # is read-only and routes to a freshly generated current-connection
        # “待办” card; all mutating/action cards must carry the token.
        if kind != 'async_notice' and data.get('connection') != self.connection:
            return '这张操作卡片来自旧连接，已过期。请重新查看目标对话；不会补执行。', 'rejected'
        # The catalog is a display/index view. A durable, scoped card may
        # outlive its row; native owner and request checks below remain the
        # authority for every operation.
        row = self.r.catalog.thread(thread_id) or self.r._reference_thread_row(thread_id)
        if kind == 'submission':
            if text == '对话':
                return self.r._thread_card(self.scope, row, connection=self.connection), 'confirmed'
            if text in ('状态', '队列'):
                from submission_receipts import status_text
                observed = self.r.backend.submission_status(data, self.r.catalog, self.connection)
                updated = {**data, 'client_id': observed.get('client_id')}
                return self.card('submission', updated, status_text(observed)
                                 + '\n引用回复“状态”继续查看本条，或“删除”移除本条排队消息。'
                                 + '\n引用回复“对话”取得普通对话回执；此卡片不发送正文或转引导。', ttl=30 * 86400), 'confirmed'
            if text == '删除':
                key = 'submission-delete:' + data['connection'] + ':' + thread_id + ':' + data['queue_id'] + ':' + data['text_digest']
                return self._once(key, lambda: self.r.backend.remove_submission(data, self.r.catalog, self.connection))
            return '本条发送回执不能直接转引导；请回复“对话”取得目标卡片，再发“队列”选择仍可见的条目。没有再次提交正文。', 'rejected'
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
                return self.card('queued', {'thread_id': thread_id, 'entry': entry,
                                            'connection': self.connection,
                                            'operation_id': uuid.uuid4().hex},
                                 f"待发送记录：{entry['summary']}\n状态：{self.queue_status(entry['status'])}\n回复“引导”会先删除这条排队消息，再把相同正文引导给当前运行任务；回复“删除”只移除它。"), 'confirmed'
            if entry.get('kind') == 'async_question':
                state = self._backend_call('control_state', thread_id, connection=self.connection)
                if state.get('owner_id') != entry.get('owner_id'):
                    return (f'未显示异步问题：提醒卡绑定 owner={entry.get("owner_id")}，当前 owner={state.get("owner_id")}。'
                            '没有回答或发送任务；请在当前对话重新查看待办。', 'rejected')
                return self._async_card(entry, state)
            state = self._backend_call('control_state', thread_id, connection=self.connection)
            guarded = self._complete_snapshot_guard(state, '待办')
            if guarded:
                return guarded
            request = self._find_request(entry, state)
            if request is None:
                return (f'未显示待办 request ID={entry.get("request_id")}、method={entry.get("method")}：'
                        '当前 Desktop requests 列表中没有与卡片摘要完全匹配的记录。没有提交答案；请重新查看待办。', 'rejected')
            return self.card('request', entry, describe_request(request)), 'confirmed'
        if kind == 'queued':
            entry = data['entry']
            if text == '对话':
                return self.r._thread_card(self.scope, row, connection=self.connection), 'confirmed'
            if text in ('队列', '状态'):
                return self.command(thread_id, '队列', '')
            if text == '删除':
                return self._once('queue-delete:' + thread_id + ':' + entry['id'] + ':' + entry['digest'],
                                  lambda: self._backend_call('remove_saved_queue_item', thread_id, entry, self.r.catalog,
                                                              connection=self.connection))
            command, _, arg = text.partition(' ')
            if command in ('引导', '/引导', '/steer'):
                if arg.strip():
                    return '为避免排队正文和引导内容不一致，请只回复“引导”；系统会使用这条排队消息的原文。', 'rejected'
                key = ('queue-guide:' + str(data.get('operation_id') or entry['id']) + ':'
                       + self.connection + ':' + thread_id + ':' + entry['digest'])
                if hasattr(self.r.backend, 'preflight_guide_saved_queue_item'):
                    checked = self._backend_call('preflight_guide_saved_queue_item', thread_id, entry,
                                                 self.r.catalog, connection=self.connection)
                    if checked.get('state') == 'rejected' and checked.get('retryable'):
                        return checked['reply'], 'rejected'
                return self._once(key, lambda: self._backend_call(
                    'guide_saved_queue_item', thread_id, entry, self.r.catalog, connection=self.connection))
            return '这条卡片可以回复“引导”（删除后将原文引导给当前任务）或“删除”。', 'rejected'
        if data.get('connection') != self.connection:
            return '这项操作来自旧连接，已过期。请重新查看对应选项或待办。', 'rejected'
        if kind == 'async_question':
            state = self._backend_call('control_state', thread_id, connection=self.connection)
            current = self._async_find(data, state)
            if current is None:
                if state.get('async_questions_unverified') or state.get('snapshot_complete') is False:
                    return ('没有显示这张卡对应的异步问题：read_thread 只含最近一轮摘要，缺少完整请求历史或问题答案状态。'
                            '没有提交答案或发送任务；请在 Desktop 打开该对话查看原问题。', 'rejected')
                return (f'没有显示这张卡对应的问题 ID={data.get("message_id")}、轮次={data.get("turn_id")}；'
                        '该问题可能已回答、结束或被新一轮取代。没有提交答案；请重新查看待办。', 'rejected')
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
            state = self._backend_call('control_state', thread_id, connection=self.connection)
            guarded = self._complete_snapshot_guard(state, '待办')
            if guarded:
                return guarded
            if state.get('owner_id') != data.get('owner_id'):
                return (f'未提交答案：该卡绑定窗口={data.get("owner_id")}，当前窗口={state.get("owner_id")}。'
                        '请重新查看当前对话的待办。', 'rejected')
            request = self._find_request(data, state)
            if request is None:
                return (f'未提交答案：当前 requests 列表中没有唯一匹配 request ID={data.get("request_id")}、'
                        f'method={data.get("method")} 和卡片摘要的原请求；它可能已处理、过期或重复。没有发送新任务。', 'rejected')
            from control_actions import request_payload
            try:
                request_payload(request, text)
            except ValueError as error:
                return str(error), 'rejected'
            key = 'request:' + self.connection + ':' + data['owner_id'] + ':' + str(data['request_id']) + ':' + data['digest']
            return self._once(key, lambda: self.r.backend.respond_to_request(data, text))
        if kind == 'permission_options':
            flow_key = 'permission-flow:' + str(data.get('operation_id', ''))
            if data.get('operation_id'):
                previous = self.r.store.operation(self.scope, flow_key)
                if previous is not None:
                    return previous['reply'], previous['state']
            if text == '取消':
                if data.get('operation_id'):
                    return self._once(flow_key, lambda: {'state': 'cancelled', 'reply': '已取消权限修改，原选项卡及其确认卡不再执行。'})
                return '没有修改权限。', 'confirmed'
            index = int(text) - 1 if text.isdigit() else -1
            if not 0 <= index < len(data['choices']):
                return '请引用回复序号，或回复“取消”。', 'rejected'
            choice = data['choices'][index]
            body = f"将改为：{choice['name']}。仅影响此对话后续轮次，不改全局配置。\n"
            if choice['name'] == '完全访问':
                body += '完全访问允许模型操作本机文件并执行命令，不再等待普通审批。\n'
            body += f"引用回复“确认 {choice['name']}”才执行；回复“取消”不修改。"
            # All descendants share the parent flow id. Cancellation and
            # confirmation then contend on one durable SQLite claim.
            return self.card('permission_confirm', {**data, 'choice': choice}, body), 'confirmed'
        if kind == 'permission_confirm':
            choice = data['choice']
            flow_key = 'permission-flow:' + str(data.get('operation_id', ''))
            if data.get('operation_id'):
                previous = self.r.store.operation(self.scope, flow_key)
                if previous is not None:
                    return previous['reply'], previous['state']
            if text == '取消':
                if data.get('operation_id'):
                    return self._once(flow_key, lambda: {'state': 'cancelled', 'reply': '已取消权限修改，原选项卡及其确认卡不再执行。'})
                return '没有修改权限。', 'confirmed'
            if text != '确认 ' + choice['name']:
                return '未修改，请按卡片上的完整确认文字回复。', 'rejected'
            return self._once(flow_key,
                              lambda: self._backend_call('update_permissions', thread_id, choice['changes'], data['baseline'],
                                                         connection=self.connection))
        if kind == 'compaction':
            if text != '压缩状态':
                return '本条用于查询压缩结果，请引用回复“压缩状态”；不会自动重复压缩。', 'rejected'
            try:
                state = self._backend_call('control_state', thread_id, connection=self.connection)
            except (OSError, TimeoutError, ValueError, DesktopControlError) as error:
                return (f'未取得压缩状态：读取 Desktop follower/read_thread 失败（{type(error).__name__}）。'
                        '没有再次调用压缩；连接恢复后重新引用本卡查询。', 'unknown')
            if state['owner_id'] != data['owner_id']:
                return ('压缩状态不能沿用：查询卡属于旧 Desktop 窗口，当前窗口 owner 已变化。'
                        '没有再次压缩；请引用当前对话卡重新查询。', 'unknown')
            previous = {(x.get('id'), x.get('turn_id')) for x in data['baseline']}
            fresh = [x for x in state['compactions'] if x.get('id') and (x.get('id'), x.get('turn_id')) not in previous and x.get('source') == 'manual']
            if fresh and fresh[-1].get('turn_status') == 'completed':
                item = fresh[-1]
                return (f'Desktop 历史中出现新的手动压缩记录（item={item["id"]}，轮次={item.get("turn_id")}）且轮次已完成。'
                        '原生接口没有操作编号，无法确认是否由本张 QQ 卡触发；请以电脑中的压缩记录为准。', 'unknown')
            if fresh and fresh[-1].get('turn_status') in ('failed', 'interrupted'):
                item = fresh[-1]
                return (f'Desktop 手动压缩记录 item ID={item.get("id")} 对应轮次状态={fresh[-1].get("turn_status")}；'
                        '压缩没有完成。未修复历史或自动重试，请在电脑查看错误。', 'confirmed')
            if state.get('snapshot_complete') is False:
                return ('最近一轮摘要没有发现新的手动 contextCompaction 终态记录；摘要不含完整压缩历史，'
                        'Desktop 也没有返回可关联本次请求的操作编号。没有再次压缩；请在 Desktop 查看该对话当前轮次和压缩记录。', 'unknown')
            turn = state.get('turn') or {}
            runtime = (state.get('runtime') or {}).get('type') or '缺失'
            return (f'自查询卡片建立后，Desktop follower 状态没有新增手动 contextCompaction 终态记录；'
                    f'当前 thread={runtime}、turn={turn.get("status", "缺失")}。没有再次压缩；若轮次仍运行，等结束后重查；'
                    '若已空闲，请在电脑查看压缩错误。', 'unknown')
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
            return (f'没有显示问题 ID={data.get("message_id")}、轮次={data.get("turn_id")}：当前原生问题列表没有相同摘要的待答记录。'
                    '没有提交答案或发送任务，请重新查看待办。', 'rejected')
        body = '异步问题（仅查看，不会暂停当前任务）\n' + describe(batch['questions'], batch['indices'])
        body += '\n\n请回电脑回答，QQ 只提醒和查看。直接回复此卡片不会发送新任务；引用回复“待办”可重新核对。'
        return self.card('async_question', data, body), 'confirmed'

    def _fork(self, data: dict) -> dict:
        source = self.r.catalog.thread(data['thread_id'])
        if source is None:
            return {'state': 'rejected', 'reply': f'未创建分支：本地目录没有源对话 {data["thread_id"]} 的项目行，无法检查其项目归属。没有调用 fork；请刷新“对话”后重新查看分支范围。'}
        current_project = self._project_identity(source)
        if current_project != data['source_project']:
            return {'state': 'rejected', 'reply': f'未创建分支：源对话项目归属已从 {data["source_project"]} 变为 {current_project}。没有调用 fork；请重新查看分支范围。'}
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
        return {**result, 'reply': self.r._card(self.scope, 'thread', {'thread_id': ident, 'connection': self.connection}, body)}

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
                'outcome-unknown': f'提交结果待核实（本机队列状态={value!r}，未重发）'}.get(
                    value, f'未知本机队列状态={value!r}；没有据此重发')
