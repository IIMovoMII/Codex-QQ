"""Bounded RPC to the already running Desktop's native app-tool service.

No MCP process, model router, Desktop launch, or general-purpose tool dispatch.
The source is an explicitly quoted real Codex thread. QQ calls have their own
IDs; they never impersonate an active model turn or a model tool call.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from contextlib import ExitStack, contextmanager
from itertools import islice
from pathlib import Path
from typing import Any

from desktop_control import DesktopControlError, WindowsDesktopPipe, pipe_server_identity
from control_contract import check_app_tools_contract
from tool_schema import validate_arguments
from endpoint_hint import PIPE_NAME, MAX_ENDPOINT_CANDIDATES, discover_native_endpoints

ALLOWED_TOOLS = frozenset({'read_thread', 'list_projects', 'set_thread_title', 'create_thread', 'fork_thread', 'navigate_to_codex_page'})
WRITE_TOOLS = frozenset({'set_thread_title', 'create_thread', 'fork_thread', 'navigate_to_codex_page'})
THREAD_ID = re.compile(r'^[A-Za-z0-9_-]{20,100}$')


class NativeToolError(DesktopControlError):
    pass


class NativeAppTools:
    def __init__(self, *, pipe_factory: Any = WindowsDesktopPipe,
                 identity_reader: Any = pipe_server_identity,
                 contract_check: Any = check_app_tools_contract,
                 endpoint: str | None = None, hint_path: Path | None = None,
                 endpoint_discovery: Any = discover_native_endpoints) -> None:
        self.pipe_factory, self.identity_reader = pipe_factory, identity_reader
        self.contract_check, self.endpoint = contract_check, endpoint
        self.hint_path = hint_path
        self.endpoint_discovery = endpoint_discovery
        self._resolved_endpoint: tuple[str, str] | None = None

    def _path(self) -> str:
        value = self.endpoint
        if self.endpoint is None:
            from runtime_io import default_data_dir
            path = self.hint_path or default_data_dir() / 'state' / 'native-app-tools.json'
            try:
                if not 0 < path.stat().st_size <= 1024:
                    raise ValueError('invalid-endpoint-hint')
                hint = json.loads(path.read_text(encoding='utf-8'))
                if isinstance(hint, dict) and hint.get('version') == 1:
                    value = hint.get('path') or ''
            except (OSError, ValueError):
                pass
            # A resident gateway may inherit yesterday's Desktop environment.
            # Prefer a refreshed hook hint initially, but it is never proof that
            # this path still exists or belongs to the current Desktop.
            value = value or os.environ.get('CODEX_APP_TOOLS_PIPE_PATH', '')
        if not isinstance(value, str):
            raise NativeToolError('native-app-tools-endpoint-unavailable')
        if not PIPE_NAME.fullmatch(value):
            raise NativeToolError('native-app-tools-endpoint-unavailable')
        return value

    def _candidate_paths(self, expected_identity: str):
        # An explicit override is strict: invalid/missing does not enable scan.
        if self.endpoint is not None:
            yield self._path()
            return
        seen: set[str] = set()
        cached = self._resolved_endpoint
        if cached is not None and cached[0] == expected_identity:
            seen.add(cached[1])
            yield cached[1]
        try:
            hinted = self._path()
        except NativeToolError:
            hinted = ''
        for value in (hinted, os.environ.get('CODEX_APP_TOOLS_PIPE_PATH', '')):
            if isinstance(value, str) and PIPE_NAME.fullmatch(value) and value not in seen:
                seen.add(value)
                yield value
        # Only reached if cached/hook/environment candidates could not connect.
        for value in islice(self.endpoint_discovery(), MAX_ENDPOINT_CANDIDATES):
            if len(seen) >= MAX_ENDPOINT_CANDIDATES:
                break
            if isinstance(value, str) and PIPE_NAME.fullmatch(value) and value not in seen:
                seen.add(value)
                yield value

    @contextmanager
    def _connection(self, expected_identity: str, timeout_seconds: float):
        deadline = time.monotonic() + timeout_seconds
        for path in self._candidate_paths(expected_identity):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            with ExitStack() as stack:
                try:
                    pipe = stack.enter_context(self.pipe_factory(timeout_seconds=min(2.0, remaining), path=path))
                    self._validate_server(pipe, expected_identity)
                    catalog = self._rpc(pipe, 'tools/list', {'threadStartKind': 'all'})
                    if not self._catalog(catalog):
                        raise NativeToolError('native-app-tools-invalid-catalog')
                except (OSError, TimeoutError, ValueError, KeyError, TypeError, DesktopControlError):
                    self._resolved_endpoint = None
                    continue
                self._resolved_endpoint = (expected_identity, path)
                if hasattr(pipe, 'deadline'):
                    pipe.deadline = deadline
                # No fallback may catch failures raised after this yield. The
                # caller can already have dispatched a mutation on this pipe.
                yield pipe, catalog
                return
        raise NativeToolError('native-app-tools-endpoint-unavailable')

    def _validate_server(self, pipe: Any, expected_identity: str) -> None:
        identity, _, image = self.identity_reader(pipe)
        if identity != expected_identity:
            raise NativeToolError('native-app-tools-desktop-identity-mismatch')
        self.contract_check(image)

    @staticmethod
    def _rpc(pipe: Any, method: str, params: dict) -> dict:
        request_id = 'qq-rpc-' + uuid.uuid4().hex
        pipe.write({'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params})
        response = pipe.read()
        if (not isinstance(response, dict) or response.get('jsonrpc') != '2.0' or response.get('id') != request_id
                or ('result' in response) == ('error' in response)):
            raise NativeToolError('native-app-tools-invalid-response')
        if 'error' in response:
            # A service error can follow a successful native mutation. Never use
            # its text to infer that nothing happened, or forward private paths.
            raise NativeToolError('native-app-tools-rpc-error')
        if not isinstance(response['result'], dict):
            raise NativeToolError('native-app-tools-invalid-response')
        return response['result']

    @staticmethod
    def _catalog(value: dict) -> set[str]:
        rows = value.get('tools')
        if not isinstance(rows, list) or len(rows) > 256:
            raise NativeToolError('native-app-tools-invalid-catalog')
        names: set[str] = set()
        for row in rows:
            if not isinstance(row, dict) or row.get('namespace') != 'codex_app':
                continue
            name = row.get('name')
            if not isinstance(name, str) or name in names:
                raise NativeToolError('native-app-tools-ambiguous-catalog')
            names.add(name)
        return names

    def capabilities(self, expected_identity: str) -> set[str]:
        with self._connection(expected_identity, 5.0) as (_, catalog):
            return self._catalog(catalog)

    def call(self, tool: str, arguments: dict, *, source_thread_id: str,
             expected_identity: str, operation_id: str, mutation: bool = False) -> dict:
        if (tool not in ALLOWED_TOOLS or not isinstance(source_thread_id, str)
                or not THREAD_ID.fullmatch(source_thread_id)):
            return {'state': 'rejected', 'reason': 'unsupported-native-tool'}
        if not isinstance(arguments, dict) or mutation != (tool in WRITE_TOOLS):
            return {'state': 'rejected', 'reason': 'invalid-native-tool-call'}
        if not self._arguments_valid(tool, arguments, source_thread_id):
            return {'state': 'rejected', 'reason': 'invalid-native-tool-arguments'}
        if not isinstance(operation_id, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,160}', operation_id):
            return {'state': 'rejected', 'reason': 'invalid-operation-id'}
        dispatched = False
        try:
            with self._connection(expected_identity, 10.0) as (pipe, catalog):
                available = self._catalog(catalog)
                if tool not in available:
                    return {'state': 'rejected', 'reason': 'native-tool-not-published'}
                row = next(x for x in catalog['tools']
                           if x.get('namespace') == 'codex_app' and x.get('name') == tool)
                if not validate_arguments(arguments, row.get('inputSchema')):
                    return {'state': 'rejected', 'reason': 'native-tool-schema-incompatible'}
                # Same server and same Desktop generation, including immediately
                # before writing. Endpoint recovery ends before tool dispatch.
                self._validate_server(pipe, expected_identity)
                dispatched = True
                result = self._rpc(pipe, 'tools/call', {
                    'arguments': arguments, 'callerSource': 'codex', 'hostId': 'local',
                    'namespace': 'codex_app', 'tool': tool, 'threadId': source_thread_id,
                    'callId': 'qq-control-' + operation_id,
                    'turnId': 'qq-control-context-' + operation_id,
                })
                if result.get('success') is not True:
                    # Some tools catch failures after partially creating an
                    # object. For writes, even an explicit tool failure is unknown.
                    return {'state': 'unknown' if mutation else 'rejected', 'reason': 'native-tool-failed'}
                items = result.get('contentItems')
                if (not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict)
                        or items[0].get('type') != 'inputText'):
                    raise NativeToolError('native-app-tools-invalid-content')
                data = json.loads(items[0]['text'])
                if not isinstance(data, dict):
                    raise NativeToolError('native-app-tools-invalid-content')
                return {'state': 'confirmed', 'data': data}
        except (OSError, TimeoutError, ValueError, KeyError, TypeError, DesktopControlError):
            return {'state': 'unknown' if mutation and dispatched else 'rejected',
                    'reason': 'native-app-tools-confirmation-lost' if dispatched else 'native-app-tools-unavailable'}

    def thread(self, thread_id: str, expected_identity: str) -> dict:
        result = self.call('read_thread', {'threadId': thread_id, 'turnLimit': 1,
                           'includeOutputs': False, 'maxOutputCharsPerItem': 0},
                           source_thread_id=thread_id, expected_identity=expected_identity,
                           operation_id=uuid.uuid4().hex)
        row = result.get('data', {}).get('thread')
        if result['state'] != 'confirmed':
            raise NativeToolError('native-app-tools-read-unavailable')
        if (not isinstance(row, dict) or row.get('id') != thread_id
                or row.get('kind') != 'codex' or row.get('hostId') != 'local'):
            raise NativeToolError('native-thread-target-unverified')
        return {k: row.get(k) for k in ('id', 'title', 'hostId', 'kind', 'status', 'cwd')}

    @staticmethod
    def _arguments_valid(tool: str, args: dict, source: str) -> bool:
        if tool == 'list_projects':
            return args == {}
        if tool == 'read_thread':
            return (args == {'threadId': source, 'turnLimit': 1, 'includeOutputs': False,
                             'maxOutputCharsPerItem': 0})
        if tool == 'fork_thread':
            return args == {'threadId': source, 'environment': {'type': 'same-directory'}}
        if tool == 'navigate_to_codex_page':
            return args == {'threadId': source}
        if tool == 'set_thread_title':
            title = args.get('title')
            return (set(args) == {'threadId', 'source', 'title'} and args['threadId'] == source
                    and args['source'] == 'codex' and isinstance(title, str) and 1 <= len(title) <= 120
                    and not any(ord(c) < 32 for c in title))
        if tool == 'create_thread':
            if set(args) - {'prompt', 'target', 'title'} or not isinstance(args.get('prompt'), str):
                return False
            title = args.get('title')
            if 'title' in args and (not isinstance(title, str) or not 1 <= len(title) <= 120
                                    or any(ord(c) < 32 for c in title)):
                return False
            target = args.get('target')
            return (1 <= len(args['prompt'].strip()) <= 4000 and isinstance(target, dict)
                    and (target == {'type': 'projectless'} or
                         set(target) == {'type', 'projectId', 'environment'} and target.get('type') == 'project'
                         and isinstance(target.get('projectId'), str) and bool(target['projectId'])
                         and target['environment'] == {'type': 'local'}))
        return False

    def projects(self, source_thread_id: str, expected_identity: str) -> list[dict]:
        self.thread(source_thread_id, expected_identity)
        result = self.call('list_projects', {}, source_thread_id=source_thread_id,
                           expected_identity=expected_identity, operation_id=uuid.uuid4().hex)
        rows = result.get('data', {}).get('projects')
        if result['state'] != 'confirmed' or not isinstance(rows, list) or len(rows) > 256:
            raise NativeToolError('native-project-catalog-unavailable')
        output, seen = [], set()
        for row in rows:
            if not isinstance(row, dict) or row.get('projectKind') != 'local' or row.get('hostId') != 'local':
                continue
            ident, name = row.get('projectId'), row.get('label')
            if not isinstance(ident, str) or not ident or ident in seen or not isinstance(name, str):
                raise NativeToolError('native-project-catalog-ambiguous')
            seen.add(ident)
            output.append({'id': ident, 'name': name, 'path': row.get('path')})
        return output

    def open_thread(self, thread_id: str, expected_identity: str, operation_id: str) -> dict:
        """Open an existing verified local chat in the current Desktop window."""
        try:
            self.thread(thread_id, expected_identity)
        except (DesktopControlError, OSError, TimeoutError, ValueError):
            return {'state': 'rejected', 'reply': '无法核实原对话身份，未尝试打开。'}
        result = self.call('navigate_to_codex_page', {'threadId': thread_id},
                           source_thread_id=thread_id, expected_identity=expected_identity,
                           operation_id=operation_id, mutation=True)
        if result['state'] == 'confirmed' and result.get('data') == {'navigated': True}:
            return {'state': 'confirmed'}
        if result['state'] == 'rejected':
            return {'state': 'rejected', 'reply': '当前 Desktop 无法打开此对话，请在电脑打开后再发送任务。'}
        return {'state': 'unknown', 'reply': '已请求打开对话，但未取得确认。请先在电脑核对；不会重复打开或发送任务。'}

    def boundary(self, thread_id: str, expected_identity: str) -> dict:
        result = self.call('read_thread', {'threadId': thread_id, 'turnLimit': 1,
                           'includeOutputs': False, 'maxOutputCharsPerItem': 0},
                           source_thread_id=thread_id, expected_identity=expected_identity,
                           operation_id=uuid.uuid4().hex)
        data = result.get('data', {})
        row, turns, page = data.get('thread'), data.get('turns'), data.get('page')
        if (result['state'] != 'confirmed' or not isinstance(row, dict) or row.get('id') != thread_id
                or row.get('hostId') != 'local' or row.get('kind') != 'codex'
                or not isinstance(page, dict) or page.get('order') != 'newest_first'
                or not isinstance(turns, list) or len(turns) != 1 or not isinstance(turns[0], dict)
                or not isinstance(turns[0].get('id'), str) or not turns[0]['id']
                or turns[0].get('status') != 'completed'):
            raise NativeToolError('native-fork-boundary-unavailable')
        turn = turns[0]
        return {'turn_id': turn['id'], 'completed_at': turn.get('completedAt'),
                'updated_at': row.get('updatedAt'), 'title': row.get('title')}

    def fork_latest(self, thread_id: str, expected_identity: str, operation_id: str) -> dict:
        try:
            self.thread(thread_id, expected_identity)
        except (DesktopControlError, OSError, TimeoutError, ValueError):
            return {'state': 'rejected', 'reply': '无法确认原生源对话，未分支。请恢复连接后重新查看范围。'}
        result = self.call('fork_thread', {'threadId': thread_id, 'environment': {'type': 'same-directory'}},
                           source_thread_id=thread_id, expected_identity=expected_identity,
                           operation_id=operation_id, mutation=True)
        if result['state'] != 'confirmed':
            return {'state': result['state'], 'reply': '分支结果未确认，可能已创建；不会再次分支，请先在电脑查看列表。' if result['state'] == 'unknown' else '当前 Desktop 的原生分支服务不可用，未分支。'}
        data, ident = result['data'], result['data'].get('threadId')
        if (data.get('status') != 'created' or not isinstance(ident, str)
                or not THREAD_ID.fullmatch(ident) or ident == thread_id
                or data.get('sourceThreadId') != thread_id
                or data.get('environment') != {'type': 'same-directory'}):
            return {'state': 'unknown', 'reply': '原生分支返回身份不完整，结果未知。请在电脑查看；不会再次创建。'}
        sync = data.get('synchronization')
        if not isinstance(sync, dict) or sync.get('status') != 'complete':
            return {'state': 'unknown', 'thread_id': ident,
                    'reply': '分支已创建，但原生设置同步没有完成。请先在电脑修复这个分支，不要重复分支或发送任务。'}
        return {'state': 'confirmed', 'thread_id': ident}

    def rename(self, thread_id: str, title: str, expected_identity: str, operation_id: str) -> dict:
        title = title.strip()
        try:
            title.encode('utf-8', errors='strict')
            if not 1 <= len(title) <= 120 or any(ord(c) < 32 for c in title):
                raise ValueError('invalid-title')
            before = self.thread(thread_id, expected_identity)
        except (ValueError, UnicodeError, DesktopControlError):
            return {'state': 'rejected', 'reply': '标题或原生对话身份无法确认，未修改。标题须为 1～120 字的单行文字。'}
        if before['title'] == title:
            return {'state': 'confirmed', 'reply': '已经是这个标题，没有重复修改。', 'title': title}
        result = self.call('set_thread_title', {'source': 'codex', 'threadId': thread_id, 'title': title},
                           source_thread_id=thread_id, expected_identity=expected_identity,
                           operation_id=operation_id, mutation=True)
        if result['state'] != 'confirmed':
            return {'state': result['state'], 'reply': '改名结果未确认，可能已经生效；不会自动重试，请查看对话列表。' if result['state'] == 'unknown' else '当前 Desktop 的原生改名服务不可用，未修改。'}
        data = result['data']
        try:
            after = self.thread(thread_id, expected_identity)
        except DesktopControlError:
            after = {}
        if data.get('threadId') != thread_id or data.get('title') != title or after.get('title') != title:
            return {'state': 'unknown', 'reply': '窗口已受理改名，但回读未确认一致；不会重复写入，请在电脑核对。'}
        return {'state': 'confirmed', 'reply': '标题已修改并通过原生回读确认；旧引用仍指向这个对话。', 'title': title}

    def create(self, source_thread_id: str, project: dict, prompt: str, title: str | None,
               expected_identity: str, operation_id: str) -> dict:
        try:
            prompt.encode('utf-8', errors='strict')
            if not 1 <= len(prompt.strip()) <= 4000:
                raise ValueError('invalid-first-prompt')
            if title is not None and (not 1 <= len(title) <= 120 or any(ord(c) < 32 for c in title)):
                raise ValueError('invalid-title')
            if title is not None:
                title.encode('utf-8', errors='strict')
            self.thread(source_thread_id, expected_identity)
            if project.get('id') is not None:
                current = next((p for p in self.projects(source_thread_id, expected_identity)
                                if p['id'] == project['id']), None)
                if current != project:
                    return {'state': 'rejected', 'reply': '所选项目已变化或不可用，未创建。请重新选择项目。'}
                target = {'type': 'project', 'projectId': project['id'], 'environment': {'type': 'local'}}
            else:
                if project != {'id': None, 'name': '无项目', 'path': None}:
                    raise ValueError('invalid-projectless-selection')
                target = {'type': 'projectless'}
        except (ValueError, UnicodeError, DesktopControlError):
            return {'state': 'rejected', 'reply': '原生来源、项目或首条任务无法确认，未创建。请重新开始向导。'}
        result = self.call('create_thread', {'target': target, 'prompt': prompt,
                           **({'title': title} if title else {})}, source_thread_id=source_thread_id,
                           expected_identity=expected_identity, operation_id=operation_id, mutation=True)
        if result['state'] != 'confirmed':
            return {'state': result['state'], 'reply': '创建结果未确认，可能已创建并开始执行。不会自动重试；请先查看新对话列表，不要再次确认创建。' if result['state'] == 'unknown' else '当前 Desktop 的原生创建服务不可用，未创建。'}
        data = result['data']
        ident = data.get('threadId') or (data.get('conversationId') if data.get('status') == 'created' else None)
        if (not isinstance(ident, str) or not THREAD_ID.fullmatch(ident) or ident == source_thread_id
                or data.get('hostId') != 'local'):
            return {'state': 'unknown', 'reply': '原生创建返回的信息不完整，结果未知。请查看新对话列表；不会再次创建。'}
        first = data.get('firstTurn')
        if first is not None and (not isinstance(first, dict) or first.get('status') != 'accepted'):
            return {'state': 'unknown', 'thread_id': ident, 'reply': '对话已创建，但首条任务尚未确认开始。请引用本条查看状态；不要重新创建。'}
        try:
            after = self.thread(ident, expected_identity)
        except DesktopControlError:
            return {'state': 'unknown', 'thread_id': ident, 'reply': '已取得新对话编号，但原生回读未确认。任务可能已经开始；请引用本条查询，不要重新创建。'}
        return {'state': 'confirmed', 'thread_id': ident, 'title': after.get('title'),
                'reply': '对话已创建，首条任务已被原生入口接受；完成或出错后继续提醒。'}
