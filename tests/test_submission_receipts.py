from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src' / 'codex_desktop_weixin'))
from submission_receipts import (accepted_submissions, digest, observed_status,
                                 queue_ack, read_queued_item, status_text)
from control_backend import NativeBackend
from desktop_control import DesktopControlError
import test_control_router as fixtures
from test_control_router import THREAD_A

QUEUE = '00000000-0000-7000-8000-000000000004'
CLIENT = '00000000-0000-4000-8000-000000000005'
TURN = '0196a9d9-5916-7777-84bb-6178a2e3a177'
TEXT = '请检查中文排队😀'


class QueueFixture:
    def __init__(self, path):
        self.path = path
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('CREATE TABLE queued_items(id TEXT PRIMARY KEY NOT NULL, '
                       'thread_id TEXT NOT NULL, payload_json TEXT NOT NULL, '
                       'queue_order INTEGER NOT NULL, created_at_ms INTEGER NOT NULL, '
                       'updated_at_ms INTEGER NOT NULL)')
        self.add()

    def add(self, *, payload=None, thread=THREAD_A):
        if payload is None:
            payload = {'clientUserMessageId': CLIENT,
                       'input': [{'type': 'text', 'text': TEXT, 'text_elements': []}]}
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('INSERT OR REPLACE INTO queued_items VALUES(?,?,?,0,1,1)',
                       (QUEUE, thread, json.dumps(payload)))

    def consume(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('DELETE FROM queued_items WHERE id=?', (QUEUE,))


def receipt(**kwargs):
    return {'thread_id': THREAD_A, 'queue_id': QUEUE, 'client_id': None,
            'text_digest': digest(TEXT), 'connection': 'same-process', **kwargs}


class QueueBase:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'queue.sqlite'
        self.queue = QueueFixture(self.path)


class ReceiptTests(QueueBase, unittest.TestCase):

    def test_queue_ack_exact_target_and_single_identity_only(self):
        ack = f'Queued message {QUEUE} for thread {THREAD_A}.'
        self.assertEqual(queue_ack(ack, THREAD_A), QUEUE)
        self.assertEqual(queue_ack('log line\n' + ack + '\n', THREAD_A), QUEUE)
        for output in ('', ack.replace(THREAD_A, QUEUE), ack + '\n' + ack,
                       f'queue command success {QUEUE}', ack.replace('Queued message', 'started')):
            self.assertIsNone(queue_ack(output, THREAD_A))

    def test_read_exact_queue_identity_without_retaining_body(self):
        item = read_queued_item(self.path, THREAD_A, QUEUE, text_digest=digest(TEXT))
        self.assertEqual(item['client_id'], CLIENT)
        self.assertTrue(item['payload_verified'])
        self.assertNotIn(TEXT, str(item))
        with closing(sqlite3.connect(self.path)) as db, db:
            self.assertEqual(db.execute('SELECT count(*) FROM queued_items').fetchone()[0], 1)
        with self.assertRaises(ValueError):
            read_queued_item(self.path, QUEUE, QUEUE)
        with self.assertRaises(ValueError):
            read_queued_item(self.path, THREAD_A, QUEUE, text_digest=digest('改过了'))

    def test_unknown_payload_can_show_queued_but_not_enable_delete(self):
        self.queue.add(payload={'input': [], 'clientUserMessageId': CLIENT, 'newField': True})
        result = observed_status(receipt(), self.path, lambda _: {'accepted_submissions': []})
        self.assertEqual(result['status'], 'queued')
        self.assertFalse(result['payload_verified'])
        self.assertIsNone(result['client_id'])

    def test_snake_case_is_explicit_and_mixed_aliases_reject(self):
        payload = {'client_user_message_id': CLIENT, 'input': [{'type': 'text', 'text': TEXT}]}
        self.queue.add(payload=payload)
        self.assertTrue(read_queued_item(self.path, THREAD_A, QUEUE)['payload_verified'])
        payload['clientUserMessageId'] = CLIENT
        self.queue.add(payload=payload)
        self.assertFalse(read_queued_item(self.path, THREAD_A, QUEUE)['payload_verified'])

    def test_empty_missing_corrupt_or_future_schema_is_not_started(self):
        self.queue.consume()
        snapshot = lambda _, expected_connection=None: {'turn': {'status': 'inProgress'}, 'accepted_submissions': []}
        self.assertEqual(observed_status(receipt(client_id=CLIENT), self.path, snapshot)['status'], 'unconfirmed')

        empty = Path(self.tmp.name) / 'empty.sqlite'
        with closing(sqlite3.connect(empty)):
            pass
        with self.assertRaises(ValueError):
            read_queued_item(empty, THREAD_A, QUEUE)
        self.assertEqual(observed_status(receipt(), empty, snapshot)['status'], 'unconfirmed')

        missing = Path(self.tmp.name) / 'missing.sqlite'
        self.assertEqual(observed_status(receipt(), missing, snapshot)['status'], 'unconfirmed')
        self.assertFalse(missing.exists())
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('ALTER TABLE queued_items ADD COLUMN future TEXT')
        self.assertEqual(observed_status(receipt(), self.path, snapshot)['status'], 'unconfirmed')

    def test_started_requires_confirmed_client_identity_not_busy_or_text(self):
        values = {'accepted_submissions': [{'client_id': CLIENT, 'turn_id': TURN, 'status': 'inProgress'}]}
        result = observed_status(receipt(), self.path, lambda _: values)
        self.assertEqual(result['status'], 'entered')
        self.assertEqual(result['turn_id'], TURN)
        other = {'accepted_submissions': [{**values['accepted_submissions'][0], 'client_id': QUEUE}]}
        self.assertEqual(observed_status(receipt(), self.path, lambda _: other)['status'], 'queued')

    def test_consume_or_delete_during_query_never_implies_started(self):
        def concurrent(_):
            self.queue.consume()
            return {'accepted_submissions': []}
        self.assertEqual(observed_status(receipt(), self.path, concurrent)['status'], 'unconfirmed')
        self.queue.add()
        def actual_start(_):
            self.queue.consume()
            return {'accepted_submissions': [{'client_id': CLIENT, 'turn_id': TURN, 'status': 'completed'}]}
        result = observed_status(receipt(), self.path, actual_start)
        self.assertEqual(result['status'], 'entered')
        self.assertIn('已完成', status_text(result))

    def test_ambiguous_duplicate_client_is_not_success(self):
        item = {'client_id': CLIENT, 'turn_id': TURN, 'status': 'inProgress'}
        result = observed_status(receipt(), self.path, lambda _: {'accepted_submissions': [item, item]})
        self.assertEqual(result['status'], 'unconfirmed')

    def test_owner_read_failure_preserves_queue_observation_and_client_id(self):
        def disconnected(_):
            raise DesktopControlError('no-client-found')
        result = observed_status(receipt(), self.path, disconnected)
        self.assertEqual(result['status'], 'queued')
        self.assertEqual(result['client_id'], CLIENT)

    def test_projection_skips_optimistic_or_unaccepted_and_keeps_no_text(self):
        turns = [{'turnId': TURN, 'status': 'inProgress', 'params': {'clientUserMessageId': CLIENT},
                  'items': [{'type': 'steeringUserMessage', 'status': 'pending', 'clientUserMessageId': CLIENT,
                             'input': [{'text': TEXT}]}]}]
        self.assertEqual(accepted_submissions(turns), [])
        turns[0]['items'].append({'type': 'userMessage', 'clientId': CLIENT, 'content': [{'text': TEXT}]})
        items = accepted_submissions(turns)
        self.assertEqual(items, [{'client_id': CLIENT, 'turn_id': TURN, 'status': 'inProgress'}])
        self.assertNotIn(TEXT, str(items))
        turns[0]['items'] = [{'type': 'steeringUserMessage', 'status': 'accepted',
                              'serverClientUserMessageId': CLIENT, 'serverUserMessageId': 'server-id'}]
        self.assertEqual(accepted_submissions(turns), items)
        turns[0]['turnId'] = None
        self.assertEqual(accepted_submissions(turns), [])

    def test_queue_reader_sees_wal_without_copy_or_checkpoint(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('UPDATE queued_items SET updated_at_ms=2')
            db.commit()
            self.assertTrue(read_queued_item(self.path, THREAD_A, QUEUE)['payload_verified'])


class ReceiptBackendTests(QueueBase, unittest.TestCase):
    def backend(self):
        b = NativeBackend(contract_check=lambda: None)
        b.probe = lambda: ('same-process', 1)
        b.snapshot = lambda _, expected_connection=None: {'owner_id': 'owner', 'accepted_submissions': []}
        self.calls = []
        def call(thread, method, params, version, **kwargs):
            self.calls.append((thread, method, params, version))
            self.queue.consume()
            return {'state': 'confirmed', 'result': {'removed': {'message': {'id': QUEUE, 'text': TEXT}}}}
        b.call = call
        return b

    def test_native_delete_only_exact_id_and_no_database_mutation_by_adapter(self):
        b = self.backend()
        result = b.remove_submission(receipt(), type('Catalog', (), {'queue_path': self.path})(), 'same-process')
        self.assertEqual(result['state'], 'confirmed')
        self.assertEqual(self.calls[0][1:4], ('thread-follower-remove-queued-message', {'messageId': QUEUE}, 1))
        self.assertEqual(len(self.calls), 1)

    def test_changed_connection_or_started_or_unknown_input_cannot_delete(self):
        b = self.backend()
        catalog = type('Catalog', (), {'queue_path': self.path})()
        self.assertEqual(b.remove_submission(receipt(), catalog, 'new-process')['state'], 'rejected')
        b.snapshot = lambda _, expected_connection=None: {'accepted_submissions': [{'client_id': CLIENT, 'turn_id': TURN}]}
        self.assertEqual(b.remove_submission(receipt(), catalog, 'same-process')['state'], 'rejected')
        b.snapshot = lambda _, expected_connection=None: {}
        self.queue.add(payload={'new': 'field'})
        self.assertEqual(b.remove_submission(receipt(), catalog, 'same-process')['state'], 'rejected')
        self.assertEqual(self.calls, [])

    def test_delete_after_dispatch_loss_or_wrong_return_never_retries(self):
        b = self.backend()
        catalog = type('Catalog', (), {'queue_path': self.path})()
        b.call = lambda *a, **k: {'state': 'unknown'}
        self.assertEqual(b.remove_submission(receipt(), catalog, 'same-process')['state'], 'unknown')
        b.call = lambda *a, **k: {'state': 'confirmed', 'result': {'removed': {'message': {'id': QUEUE, 'text': '不同正文'}}}}
        self.assertEqual(b.remove_submission(receipt(), catalog, 'same-process')['state'], 'unknown')
        self.assertTrue(read_queued_item(self.path, THREAD_A, QUEUE))

    def test_native_consumption_wins_delete_race_without_deleting_other_ids(self):
        b = self.backend()
        def consumed(*args, **kwargs):
            self.calls.append(args[2]['messageId'])
            self.assertEqual(kwargs['expected_connection'], 'same-process')
            self.queue.consume()
            return {'state': 'confirmed', 'result': {'removed': None}}
        b.call = consumed
        result = b.remove_submission(receipt(), type('Catalog', (), {'queue_path': self.path})(), 'same-process')
        self.assertEqual(result['state'], 'rejected')
        self.assertEqual(self.calls, [QUEUE])

    def test_connection_recheck_at_dispatch_blocks_restarted_desktop(self):
        from test_desktop_control import FakePipe
        pipe = FakePipe()
        b = NativeBackend(pipe_factory=lambda: pipe)
        with patch('control_backend.pipe_server_identity', return_value=('new-process', 2, 'test.exe')):
            result = b.call(THREAD_A, 'thread-follower-remove-queued-message', {'messageId': QUEUE}, 1,
                            expected_connection='old-process')
        self.assertEqual(result['state'], 'rejected')
        self.assertFalse(any(x.get('method') == 'thread-follower-remove-queued-message' for x in pipe.writes))


class ReceiptRouterTests(unittest.TestCase):
    request = fixtures.ControlRouterTests.request
    send = fixtures.ControlRouterTests.send

    def setUp(self):
        fixtures.ControlRouterTests.setUp(self)
        self.backend.submission_status = lambda data, catalog, conn: {'status': 'queued', 'client_id': CLIENT}
        self.removed = []
        def remove(data, catalog, conn):
            self.removed.append(data['queue_id'])
            return {'state': 'unknown', 'reply': '删除未取得确认，不会重试。'}
        self.backend.remove_submission = remove
        self.controller.queue_func = lambda thread, text: (self.calls.append((thread, text)) or True,
                                                        f'Queued message {QUEUE} for thread {thread}.')

    def test_send_receipt_and_quoted_status_keep_exact_target_and_no_plain_resubmit(self):
        card = self.send('/use ' + THREAD_A)
        ack = self.send(TEXT, quote=card)
        self.assertIn('正在排队', ack)
        ref = self.controller._resolve_quote('test|qqbot:user|user', ack)
        self.assertEqual(ref['kind'], 'submission')
        self.assertEqual(ref['data']['queue_id'], QUEUE)
        self.assertNotIn(TEXT, str(ref['data']))
        check = self.send('状态', quote=ack)
        self.assertIn('正在排队', check)
        self.assertIn('没有再次提交', self.send('继续', quote=ack))
        self.assertIn('本条发送回执不能直接转引导', self.send('引导', quote=ack))
        self.assertEqual(self.calls, [(THREAD_A, TEXT)])

    def test_receipt_can_reach_normal_thread_for_explicit_direct_steer(self):
        card = self.send('/use ' + THREAD_A)
        ack = self.send(TEXT, quote=card)
        selected = self.send('对话', quote=ack)
        ref = self.controller._resolve_quote('test|qqbot:user|user', selected)
        self.assertEqual(ref['kind'], 'thread')
        self.assertEqual(ref['data']['thread_id'], THREAD_A)

    def test_delete_unknown_is_deduped_across_updated_cards_and_platform_replay(self):
        ack = self.send(TEXT, quote=self.send('/use ' + THREAD_A))
        updated = self.send('状态', quote=ack)
        self.send('删除', quote=ack, message_id='delete-one')
        self.send('删除', quote=ack, message_id='delete-one')
        self.send('删除', quote=updated)
        self.assertEqual(self.removed, [QUEUE])
        self.assertEqual(len(self.calls), 1)

    def test_missing_native_id_does_not_fabricate_status_or_delete_card(self):
        self.controller.queue_func = lambda t, text: (True, '')
        ack = self.send(TEXT, quote=self.send('/use ' + THREAD_A))
        self.assertIn('状态未确认', ack)
        self.assertEqual(self.controller._resolve_quote('test|qqbot:user|user', ack)['kind'], 'thread')

    def test_status_can_show_entered_completed_without_another_send(self):
        ack = self.send(TEXT, quote=self.send('/use ' + THREAD_A))
        self.backend.submission_status = lambda *a: {'status': 'entered', 'client_id': CLIENT,
                                                    'turn_id': TURN, 'turn_status': 'completed'}
        status = self.send('状态', quote=ack)
        self.assertIn('已进入对话', status)
        self.assertIn('已完成', status)
        self.assertEqual(len(self.calls), 1)


if __name__ == '__main__':
    unittest.main()
