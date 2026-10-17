"""Exact queue receipts and bounded read-only status, never a history writer.

The queue command acknowledges admission to a queue, not model execution.
Only its returned queue ID joins observations; text and an empty queue are
never evidence that a message started. All queries are on demand.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from desktop_control import DesktopControlError

ID = re.compile(r'^[A-Za-z0-9_-]{20,100}$')
ACK = re.compile(r'^Queued message ([A-Za-z0-9_-]{20,100}) for thread ([A-Za-z0-9_-]{20,100})\.$')
QUEUE_COLUMNS = ('id', 'thread_id', 'payload_json', 'queue_order', 'created_at_ms', 'updated_at_ms')


def queue_ack(stdout: str, thread_id: str) -> str | None:
    """Ignore other log lines but require exactly one unambiguous acknowledgement."""
    matches = [ACK.fullmatch(line.strip()) for line in stdout.splitlines()]
    matches = [m for m in matches if m is not None]
    if len(matches) != 1 or matches[0][2] != thread_id:
        return None
    return matches[0][1]


def digest(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8', errors='strict')).hexdigest()


def read_queued_item(path: Path, thread_id: str, queue_id: str,
                     *, text_digest: str | None = None) -> dict | None:
    """At most one primary-key lookup in Codex's queue DB, using WAL-aware read-only SQLite.

    The ID's presence proves a queue observation even if a future payload shape
    is unknown. A client ID is retained only from the inspected, text-only shape.
    No original text is returned or written to our reference store.
    """
    if not ID.fullmatch(thread_id) or not ID.fullmatch(queue_id):
        raise ValueError('invalid-queue-identity')
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=0.15)
    try:
        db.execute('PRAGMA query_only=ON')
        columns = db.execute('PRAGMA table_info(queued_items)').fetchall()
        if tuple(row[1] for row in columns) != QUEUE_COLUMNS or columns[0][5] != 1:
            raise ValueError('unsupported-native-queue-schema')
        # Bound the payload before transferring it from SQLite to Python.
        row = db.execute('SELECT thread_id, CASE WHEN length(payload_json)<=32768 '
                         'THEN payload_json ELSE NULL END FROM queued_items WHERE id=?', (queue_id,)).fetchone()
        if row is None:
            return None
        if row[0] != thread_id:
            raise ValueError('queue-thread-mismatch')
        result: dict[str, Any] = {'queue_id': queue_id, 'client_id': None, 'payload_verified': False}
        if not isinstance(row[1], str):
            return result
        try:
            payload = json.loads(row[1])
            if not isinstance(payload, dict):
                return result
            # The schema uses clientUserMessageId; the internal serialized form
            # may use snake_case. Mixed aliases or non-text data fail closed.
            names = [k for k in ('clientUserMessageId', 'client_user_message_id') if k in payload]
            if len(names) != 1 or set(payload) - {names[0], 'input', 'id'}:
                return result
            if 'id' in payload and payload['id'] != queue_id:
                return result
            inputs, client_id = payload.get('input'), payload[names[0]]
            if (not isinstance(client_id, str) or not ID.fullmatch(client_id)
                    or not isinstance(inputs, list) or len(inputs) != 1):
                return result
            item = inputs[0]
            if (not isinstance(item, dict) or set(item) - {'type', 'text', 'text_elements'}
                    or item.get('type') != 'text' or not isinstance(item.get('text'), str)
                    or item.get('text_elements', []) != []):
                return result
            if text_digest is not None and digest(item['text']) != text_digest:
                raise ValueError('queued-input-changed')
            return {**result, 'client_id': client_id, 'payload_verified': True}
        except (json.JSONDecodeError, UnicodeError):
            return result
    finally:
        db.close()


def accepted_submissions(turns: list[dict]) -> list[dict]:
    """Project only confirmed native message identity, not optimistic starts or text."""
    result = []
    for turn in turns[-16:]:
        if not isinstance(turn, dict) or not isinstance(turn.get('turnId'), str) or not turn['turnId']:
            continue
        for item in turn.get('items') or []:
            if not isinstance(item, dict):
                continue
            if item.get('type') == 'userMessage':
                client_id = item.get('clientId')
            elif (item.get('type') == 'steeringUserMessage' and item.get('status') == 'accepted'
                  and item.get('serverUserMessageId')):
                client_id = item.get('serverClientUserMessageId')
            else:
                continue
            if isinstance(client_id, str) and ID.fullmatch(client_id):
                result.append({'client_id': client_id, 'turn_id': turn['turnId'],
                               'status': turn.get('status')})
    return result[-128:]


def observed_status(receipt: dict, queue_path: Path, snapshot: Any) -> dict:
    """A fixed number of observations; losing either side never causes a resend."""
    result = {'status': 'unconfirmed', 'client_id': receipt.get('client_id'), 'turn_id': None}
    try:
        queued = read_queued_item(queue_path, receipt['thread_id'], receipt['queue_id'],
                                  text_digest=receipt.get('text_digest'))
        if queued and queued['client_id']:
            if result['client_id'] and result['client_id'] != queued['client_id']:
                return result
            result['client_id'] = queued['client_id']
        if result['client_id']:
            try:
                state = snapshot(receipt['thread_id'])
            except (OSError, TimeoutError, ValueError, DesktopControlError):
                state = {}
            found = [x for x in state.get('accepted_submissions', [])
                     if x.get('client_id') == result['client_id']]
            if len(found) == 1:
                return {**result, 'status': 'entered', 'turn_id': found[0]['turn_id'],
                        'turn_status': found[0].get('status')}
            if len(found) > 1:
                return result
        # Sample again after the snapshot; a consumed row is never "started".
        current = read_queued_item(queue_path, receipt['thread_id'], receipt['queue_id'],
                                  text_digest=receipt.get('text_digest'))
        if current is not None:
            return {**result, 'status': 'queued', 'payload_verified': current['payload_verified']}
    except (sqlite3.Error, OSError, ValueError, TimeoutError, KeyError, DesktopControlError):
        pass
    return result


def status_text(value: dict) -> str:
    if value['status'] == 'queued':
        return '正在排队：查询时这条消息仍在原生队列，尚未确认开始。任务被暂停后，旧排队项可能继续等待处理。'
    if value['status'] == 'entered':
        suffix = {'completed': '对应轮次已完成。', 'failed': '对应轮次已出错。',
                  'interrupted': '对应轮次已暂停。'}.get(value.get('turn_status'), '已不在等待排队。')
        return '已进入对话：窗口已确认这条消息的身份。' + suffix
    return '已提交，但当前执行状态未确认。记录消失不等于已开始，也可能被电脑删除；不会重发。'
