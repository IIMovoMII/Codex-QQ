from __future__ import annotations

import copy
import json
import sys
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src' / 'codex_desktop_weixin'))
from desktop_control import DesktopControlError
from native_app_tools import NativeAppTools

THREAD = '01a10000-0000-7000-8000-000000000001'
CHILD = '01a10000-0000-7000-8000-000000000002'
ENDPOINT = r'\\.\pipe\codex-browser-use-00000000-0000-0000-0000-000000000000'
PROJECT = {'id': 'project-one', 'name': '中文项目', 'path': 'D:/中文 空格'}


class Service:
    def __init__(self):
        self.names = {'read_thread', 'list_projects', 'set_thread_title', 'create_thread', 'fork_thread', 'navigate_to_codex_page'}
        self.schemas = {name: {'type': 'object'} for name in self.names}
        self.writes = []
        self.titles = {THREAD: '旧标题'}
        self.projects = [dict(PROJECT)]
        self.timeout_tool = None
        self.failed_tool = None
        self.wrong_readback = False
        self.bad_reply_id = False
        self.child_result = {'threadId': CHILD, 'hostId': 'local'}
        self.fork_result = {'status': 'created', 'threadId': CHILD, 'sourceThreadId': THREAD,
                            'environment': {'type': 'same-directory'}, 'synchronization': {'status': 'complete'}}
        self.turns = {THREAD: [{'id': 'latest-completed', 'status': 'completed', 'completedAt': 100}]}
        self.order = 'newest_first'
        self.identity = 'desktop-one'
        self.opens = []

    def factory(self, **options):
        self.opens.append(options)
        return Pipe(self)

    def client(self):
        return NativeAppTools(pipe_factory=self.factory,
                              identity_reader=lambda _: (self.identity, 1.0, 'trusted-app'),
                              contract_check=lambda _: None, endpoint=ENDPOINT)


class Pipe:
    def __init__(self, service):
        self.s, self.pending = service, []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def write(self, message):
        self.s.writes.append(copy.deepcopy(message))
        reply = {'jsonrpc': '2.0', 'id': 'wrong-id' if self.s.bad_reply_id else message['id']}
        if message['method'] == 'tools/list':
            reply['result'] = {'tools': [{'name': n, 'namespace': 'codex_app',
                                         'inputSchema': self.s.schemas.get(n)} for n in self.s.names]}
        else:
            self.assert_method(message)
            params, data = message['params'], {}
            tool, args = params['tool'], params['arguments']
            if tool == 'read_thread':
                ident = args['threadId']
                data = {'thread': {'id': ident, 'kind': 'codex', 'hostId': 'local', 'updatedAt': 100,
                                   'title': 'wrong' if self.s.wrong_readback else self.s.titles[ident],
                                   'status': {'type': 'idle'}, 'cwd': 'D:/中文 空格'},
                        'page': {'order': self.s.order}, 'turns': copy.deepcopy(self.s.turns.get(ident, []))}
            elif tool == 'list_projects':
                data = {'projects': [{'projectId': p['id'], 'label': p['name'], 'path': p['path'],
                                      'projectKind': 'local', 'hostId': 'local'} for p in self.s.projects]}
            elif tool == 'set_thread_title':
                self.s.titles[args['threadId']] = args['title']
                data = {'threadId': args['threadId'], 'title': args['title']}
            elif tool == 'create_thread':
                self.s.titles[CHILD] = args.get('title', '原生新标题')
                data = self.s.child_result
            elif tool == 'fork_thread':
                self.s.titles[CHILD] = '新分支'
                self.s.turns[CHILD] = copy.deepcopy(self.s.turns[THREAD])
                data = self.s.fork_result
            elif tool == 'navigate_to_codex_page':
                data = {'navigated': True}
            else:
                raise AssertionError('unexpected tool')
            if self.s.timeout_tool == tool:
                return
            reply['result'] = {'success': self.s.failed_tool != tool,
                               'contentItems': [{'type': 'inputText', 'text': json.dumps(data, ensure_ascii=False)}]}
        self.pending.append(reply)

    def read(self):
        if not self.pending:
            raise TimeoutError
        return self.pending.pop(0)

    @staticmethod
    def assert_method(message):
        if message['method'] != 'tools/call':
            raise AssertionError('unexpected RPC')


class NativeToolTests(unittest.TestCase):
    def setUp(self):
        self.service = Service()
        self.native = self.service.client()

    def mutations(self, tool):
        return [x for x in self.service.writes if x.get('params', {}).get('tool') == tool]

    def test_capabilities_and_metadata_use_short_connections_same_desktop(self):
        self.assertIn('create_thread', self.native.capabilities('desktop-one'))
        self.assertEqual(self.native.thread(THREAD, 'desktop-one')['title'], '旧标题')
        self.assertEqual(self.native.projects(THREAD, 'desktop-one'), [PROJECT])
        self.assertTrue(all(x['path'] == ENDPOINT and x['timeout_seconds'] <= 10 for x in self.service.opens))
        for message in self.mutations('read_thread'):
            self.assertEqual(message['params']['threadId'], THREAD)
            self.assertTrue(message['params']['turnId'].startswith('qq-control-context-'))
            self.assertEqual(message['params']['arguments']['maxOutputCharsPerItem'], 0)

    def test_wrong_process_and_contract_block_before_tool_dispatch(self):
        self.assertEqual(self.native.rename(THREAD, '新标题', 'other-process', 'op1')['state'], 'rejected')
        self.assertEqual(self.service.writes, [])
        self.native.contract_check = lambda _: (_ for _ in ()).throw(DesktopControlError('changed-contract'))
        self.assertEqual(self.native.rename(THREAD, '新标题', 'desktop-one', 'op1')['state'], 'rejected')
        self.assertEqual(self.service.writes, [])

    def test_missing_or_non_native_endpoint_is_not_discovered_by_scanning(self):
        for value in ('', r'\\.\pipe\some-other-service', 'https://example.test', 'C:/tmp/pipe'):
            self.native.endpoint = value
            self.assertEqual(self.native.rename(THREAD, 'new', 'desktop-one', 'op1')['state'], 'rejected')
        self.assertEqual(self.service.opens, [])

    def test_only_allowlisted_tools_and_explicit_write_classification(self):
        for tool, mutation in (('command/exec', True), ('set_thread_title', False), ('read_thread', True)):
            result = self.native.call(tool, {}, source_thread_id=THREAD,
                                      expected_identity='desktop-one', operation_id='op1', mutation=mutation)
            self.assertEqual(result['state'], 'rejected')
        self.assertEqual(self.service.writes, [])

    def test_unpublished_title_tool_is_not_called(self):
        self.service.names.remove('set_thread_title')
        self.assertEqual(self.native.rename(THREAD, '中文', 'desktop-one', 'op1')['state'], 'rejected')
        self.assertEqual(self.mutations('set_thread_title'), [])

    def test_new_required_argument_blocks_only_that_tool_before_dispatch(self):
        self.service.schemas['navigate_to_codex_page'] = {
            'type': 'object', 'required': ['threadId', 'newRequiredField']}
        result = self.native.open_thread(THREAD, 'desktop-one', 'op-open')
        self.assertEqual(result['state'], 'rejected')
        self.assertEqual(self.mutations('navigate_to_codex_page'), [])
        self.assertEqual(self.native.rename(THREAD, '兼容的改名', 'desktop-one', 'op-rename')['state'], 'confirmed')

    def test_missing_or_unrecognized_schema_never_sends_a_write(self):
        for schema in (None, {'type': 'object', 'unknownValidationKeyword': True}):
            self.service.schemas['set_thread_title'] = schema
            self.assertEqual(self.native.rename(THREAD, '新标题', 'desktop-one', 'op-rename')['state'], 'rejected')
        self.assertEqual(self.mutations('set_thread_title'), [])

    def test_open_existing_chat_uses_published_navigation_once(self):
        self.assertEqual(self.native.open_thread(THREAD, 'desktop-one', 'op-open'), {'state': 'confirmed'})
        calls = self.mutations('navigate_to_codex_page')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['params']['arguments'], {'threadId': THREAD})
        self.assertEqual(calls[0]['params']['threadId'], THREAD)
        self.assertEqual(self.mutations('create_thread'), [])

    def test_open_missing_tool_or_lost_confirmation_never_retries(self):
        self.service.names.remove('navigate_to_codex_page')
        self.assertEqual(self.native.open_thread(THREAD, 'desktop-one', 'op1')['state'], 'rejected')
        self.assertEqual(self.mutations('navigate_to_codex_page'), [])
        self.service.names.add('navigate_to_codex_page')
        self.service.timeout_tool = 'navigate_to_codex_page'
        self.assertEqual(self.native.open_thread(THREAD, 'desktop-one', 'op2')['state'], 'unknown')
        self.assertEqual(len(self.mutations('navigate_to_codex_page')), 1)

    def test_rename_exact_thread_unicode_and_readback(self):
        title = '中文😀 “测试” $不是命令'
        result = self.native.rename(THREAD, title, 'desktop-one', 'op1')
        self.assertEqual(result['state'], 'confirmed')
        self.assertEqual(result['title'], title)
        self.assertEqual(self.mutations('set_thread_title')[0]['params']['arguments'],
                         {'source': 'codex', 'threadId': THREAD, 'title': title})
        self.assertEqual(len(self.mutations('set_thread_title')), 1)

    def test_rename_noop_and_invalid_title_never_write(self):
        self.assertEqual(self.native.rename(THREAD, '旧标题', 'desktop-one', 'op1')['state'], 'confirmed')
        for title in ('', 'x\ny', 'x' * 121, '\udced'):
            self.assertEqual(self.native.rename(THREAD, title, 'desktop-one', 'op1')['state'], 'rejected')
        self.assertEqual(self.mutations('set_thread_title'), [])

    def test_rename_lost_confirmation_or_tool_failure_never_retries(self):
        for attr in ('timeout_tool', 'failed_tool'):
            service = Service()
            setattr(service, attr, 'set_thread_title')
            result = service.client().rename(THREAD, '已变化', 'desktop-one', 'op1')
            self.assertEqual(result['state'], 'unknown')
            self.assertEqual(service.titles[THREAD], '已变化')
            self.assertEqual(sum(x.get('params', {}).get('tool') == 'set_thread_title' for x in service.writes), 1)

    def test_rename_inconsistent_readback_is_unknown(self):
        self.service.wrong_readback = True
        self.assertEqual(self.native.rename(THREAD, 'new', 'desktop-one', 'op1')['state'], 'unknown')

    def test_create_requires_real_source_project_and_nonempty_prompt(self):
        for prompt in (' ', '\udced', 'x' * 4001):
            self.assertEqual(self.native.create(THREAD, PROJECT, prompt, None, 'desktop-one', 'op1')['state'], 'rejected')
        changed = {**PROJECT, 'name': '旧项目名'}
        self.assertEqual(self.native.create(THREAD, changed, 'first', None, 'desktop-one', 'op1')['state'], 'rejected')
        self.assertEqual(self.mutations('create_thread'), [])

    def test_create_uses_native_local_target_and_does_not_override_settings(self):
        result = self.native.create(THREAD, PROJECT, '首条任务😀\n保持多行', '新标题', 'desktop-one', 'op1')
        self.assertEqual(result['state'], 'confirmed')
        self.assertEqual(result['thread_id'], CHILD)
        args = self.mutations('create_thread')[0]['params']['arguments']
        self.assertEqual(args, {'target': {'type': 'project', 'projectId': PROJECT['id'],
                                          'environment': {'type': 'local'}},
                                'prompt': '首条任务😀\n保持多行', 'title': '新标题'})
        self.assertEqual(self.service.titles[THREAD], '旧标题')

    def test_projectless_create_does_not_fabricate_a_directory(self):
        result = self.native.create(THREAD, {'id': None, 'name': '无项目', 'path': None}, 'first', None, 'desktop-one', 'op1')
        self.assertEqual(result['state'], 'confirmed')
        self.assertEqual(self.mutations('create_thread')[0]['params']['arguments']['target'], {'type': 'projectless'})

    def test_create_lost_reply_does_not_create_twice(self):
        self.service.timeout_tool = 'create_thread'
        result = self.native.create(THREAD, PROJECT, 'first', None, 'desktop-one', 'op1')
        self.assertEqual(result['state'], 'unknown')
        self.assertIn(CHILD, self.service.titles)
        self.assertEqual(len(self.mutations('create_thread')), 1)

    def test_created_thread_with_unconfirmed_first_task_retains_identity(self):
        self.service.child_result = {'status': 'created', 'conversationId': CHILD,
                                     'hostId': 'local', 'firstTurn': {'status': 'failed'}}
        result = self.native.create(THREAD, PROJECT, 'first', None, 'desktop-one', 'op1')
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual(result['thread_id'], CHILD)
        self.assertEqual(len(self.mutations('create_thread')), 1)

    def test_foreign_or_pending_creation_result_is_not_success(self):
        for value in ({'clientThreadId': CHILD, 'hostId': 'local'},
                      {'threadId': CHILD, 'hostId': 'remote'}, {'threadId': THREAD, 'hostId': 'local'}):
            service = Service()
            service.child_result = value
            self.assertEqual(service.client().create(THREAD, PROJECT, 'first', None, 'desktop-one', 'op1')['state'], 'unknown')

    def test_wrong_response_id_fails_before_a_write(self):
        self.service.bad_reply_id = True
        self.assertEqual(self.native.rename(THREAD, 'new', 'desktop-one', 'op1')['state'], 'rejected')
        self.assertEqual(self.mutations('set_thread_title'), [])

    def test_boundary_is_latest_completed_only_and_order_is_verified(self):
        self.assertEqual(self.native.boundary(THREAD, 'desktop-one'),
                         {'turn_id': 'latest-completed', 'completed_at': 100, 'updated_at': 100, 'title': '旧标题'})
        for turns in ([], [{'id': 'active', 'status': 'inProgress'}],
                      [{'id': 'a', 'status': 'completed'}, {'id': 'b', 'status': 'completed'}]):
            self.service.turns[THREAD] = turns
            with self.assertRaises(DesktopControlError):
                self.native.boundary(THREAD, 'desktop-one')
        self.service.turns[THREAD] = [{'id': 'latest', 'status': 'completed'}]
        self.service.order = 'oldest_first'
        with self.assertRaises(DesktopControlError):
            self.native.boundary(THREAD, 'desktop-one')

    def test_fork_native_scope_new_id_and_no_extra_task_or_history_override(self):
        result = self.native.fork_latest(THREAD, 'desktop-one', 'op1')
        self.assertEqual(result, {'state': 'confirmed', 'thread_id': CHILD})
        params = self.mutations('fork_thread')[0]['params']
        self.assertEqual(params['arguments'], {'threadId': THREAD, 'environment': {'type': 'same-directory'}})
        self.assertEqual(params['threadId'], THREAD)
        self.assertEqual(self.mutations('create_thread'), [])
        self.assertEqual(self.native.boundary(CHILD, 'desktop-one')['turn_id'], 'latest-completed')

    def test_unpublished_fork_and_unverified_source_do_not_dispatch(self):
        self.service.names.remove('fork_thread')
        self.assertEqual(self.native.fork_latest(THREAD, 'desktop-one', 'op1')['state'], 'rejected')
        self.assertEqual(self.mutations('fork_thread'), [])
        self.service.identity = 'another-app'
        self.assertEqual(self.native.fork_latest(THREAD, 'desktop-one', 'op1')['state'], 'rejected')

    def test_fork_reply_loss_or_explicit_failure_remains_unknown_without_retry(self):
        for attr in ('timeout_tool', 'failed_tool'):
            service = Service()
            setattr(service, attr, 'fork_thread')
            result = service.client().fork_latest(THREAD, 'desktop-one', 'op1')
            self.assertEqual(result['state'], 'unknown')
            self.assertIn(CHILD, service.titles)
            self.assertEqual(sum(x.get('params', {}).get('tool') == 'fork_thread' for x in service.writes), 1)

    def test_bad_fork_identity_is_unknown_and_sync_failure_retains_child_id(self):
        for fields in ({'threadId': THREAD}, {'sourceThreadId': CHILD},
                       {'environment': {'type': 'worktree'}}, {'status': 'pending'}):
            self.service.fork_result = {**Service().fork_result, **fields}
            self.assertEqual(self.native.fork_latest(THREAD, 'desktop-one', 'op1')['state'], 'unknown')
        for value in (None, {'status': 'failed'}):
            self.service.fork_result = {**Service().fork_result, 'synchronization': value}
            result = self.native.fork_latest(THREAD, 'desktop-one', 'op1')
            self.assertEqual(result['state'], 'unknown')
            self.assertEqual(result['thread_id'], CHILD)

    def test_direct_call_rejects_hidden_tools_wrong_target_and_extra_fields(self):
        cases = [('fork_thread', {'threadId': CHILD, 'environment': {'type': 'same-directory'}}),
                 ('fork_thread', {'threadId': THREAD, 'environment': {'type': 'same-directory'}, 'upToItemId': 'earlier'}),
                 ('create_thread', {'target': {'type': 'projectless'}, 'prompt': 'first', 'model': 'override'}),
                 ('create_thread', {'target': {'type': 'projectless'}, 'prompt': 'first', 'title': 'multi\nline'}),
                 ('create_thread', {'target': {'type': 'project', 'projectId': 'p', 'environment': {'type': 'worktree'}}, 'prompt': 'first'}),
                 ('set_thread_title', {'threadId': CHILD, 'source': 'codex', 'title': 'other'}),
                 ('set_thread_title', {'threadId': THREAD, 'source': 'chatgpt', 'title': 'other'})]
        for tool, args in cases:
            result = self.native.call(tool, args, source_thread_id=THREAD,
                                      expected_identity='desktop-one', operation_id='op', mutation=True)
            self.assertEqual(result['state'], 'rejected')
        self.assertEqual(self.service.writes, [])

    def test_server_identity_is_checked_again_immediately_before_call(self):
        self.native.identity_reader = lambda _: ('desktop-one' if not self.service.writes else 'new-desktop', 1.0, 'trusted-app')
        with self.assertRaises(DesktopControlError):
            self.native.thread(THREAD, 'desktop-one')
        self.assertEqual(self.mutations('read_thread'), [])

    def test_hint_is_small_native_only_and_does_not_override_explicit_endpoint(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'CODEX_APP_TOOLS_PIPE_PATH': ''}):
            hint = Path(temp) / 'hint.json'
            hint.write_text(json.dumps({'version': 1, 'path': ENDPOINT}), encoding='utf-8')
            client = NativeAppTools(hint_path=hint)
            self.assertEqual(client._path(), ENDPOINT)
            client.endpoint = ''
            with self.assertRaises(DesktopControlError):
                client._path()
            client.endpoint = None
            for content in (json.dumps({'version': 2, 'path': ENDPOINT}),
                            json.dumps({'version': 1, 'path': 'https://example.test'}), 'x' * 1025, '[]'):
                hint.write_text(content, encoding='utf-8')
                with self.assertRaises(DesktopControlError):
                    client._path()

    def test_refreshed_hint_precedes_resident_gateway_stale_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            hint = Path(temp) / 'hint.json'
            fresh = ENDPOINT.replace('00000000', '11111111')
            hint.write_text(json.dumps({'version': 1, 'path': fresh}), encoding='utf-8')
            with patch.dict(os.environ, {'CODEX_APP_TOOLS_PIPE_PATH': ENDPOINT}):
                self.assertEqual(NativeAppTools(hint_path=hint)._path(), fresh)


if __name__ == '__main__':
    unittest.main()
