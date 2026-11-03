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
        if (not columns or tuple(row[1] for row in columns) != QUEUE_COLUMNS
                or columns[0][5] != 1):
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
    result = {'status': 'unconfirmed', 'client_id': receipt.get('client_id'), 'turn_id': None,
              'reason': 'queue-and-history-not-yet-matched'}
    try:
        queued = read_queued_item(queue_path, receipt['thread_id'], receipt['queue_id'],
                                  text_digest=receipt.get('text_digest'))
        if queued is None:
            result['reason'] = 'queue-row-not-found'
        if queued and queued['client_id']:
            if result['client_id'] and result['client_id'] != queued['client_id']:
                result['reason'] = 'queue-client-id-conflicts-with-receipt'
                return result
            result['client_id'] = queued['client_id']
        if result['client_id']:
            try:
                state = snapshot(receipt['thread_id'])
            except (OSError, TimeoutError, ValueError, DesktopControlError):
                state = {}
                result['reason'] = 'desktop-history-read-failed'
            found = [x for x in state.get('accepted_submissions', [])
                     if x.get('client_id') == result['client_id']]
            if len(found) == 1:
                return {**result, 'status': 'entered', 'turn_id': found[0]['turn_id'],
                        'turn_status': found[0].get('status'), 'reason': None}
            if len(found) > 1:
                result['reason'] = 'client-id-matched-multiple-turns'
                return result
            if state.get('snapshot_complete') is False:
                result['reason'] = 'desktop-history-summary-incomplete'
        # Sample again after the snapshot; a consumed row is never "started".
        current = read_queued_item(queue_path, receipt['thread_id'], receipt['queue_id'],
                                  text_digest=receipt.get('text_digest'))
        if current is not None:
            return {**result, 'status': 'queued', 'payload_verified': current['payload_verified'],
                    'reason': None if current['payload_verified'] else 'queued-row-payload-not-verified'}
        if result['reason'] not in ('desktop-history-read-failed', 'desktop-history-summary-incomplete'):
            result['reason'] = 'absent-from-queue-and-no-accepted-turn'
    except (sqlite3.Error, OSError, ValueError, TimeoutError, KeyError, DesktopControlError) as error:
        result['reason'] = f'queue-or-desktop-read-error:{type(error).__name__}'
    return result


def status_text(value: dict) -> str:
    reason = value.get('reason')
    status = value.get('status')
    if reason == 'queued-row-payload-not-verified':
        return '正在排队：本地原生队列里存在该编号，但正文结构或摘要与发送回执不能核对，无法证明仍是原消息。不会删除或重发；请检查 Desktop 队列中的原文。'
    if status == 'queued':
        return '正在排队：查询时这条消息仍在原生队列，尚未确认开始。任务被暂停后，旧排队项可能继续等待处理。'
    if status == 'entered':
        suffix = {'completed': '对应轮次已完成。', 'failed': '对应轮次已出错。',
                  'interrupted': '对应轮次已暂停。'}.get(value.get('turn_status'), '已不在等待排队。')
        return '已进入对话：窗口已确认这条消息的身份。' + suffix
    if reason == 'desktop-history-read-failed':
        return '该消息不在本地可读队列中，且 Desktop 历史读取失败；不能确认它已开始、已删除还是仍由 Desktop 持有。不会重发。请在目标对话核对。'
    if reason == 'desktop-history-summary-incomplete':
        return '该消息不在本地可读队列中；Desktop 只返回最近一轮摘要，未提供包含这条消息身份的完整历史。无法据此判断已执行还是已删除，不会重发；请在目标对话查看原文。'
    if reason == 'queue-row-not-found':
        return '没有在本地可读队列中找到这条消息，也没有匹配到已接受的 turn ID；它可能已开始、已被电脑删除或已移出队列。不会重发，请查看目标对话。'
    if reason == 'absent-from-queue-and-no-accepted-turn':
        return '队列中已没有这条消息，Desktop 最近历史也没有匹配到它的 client ID；无法区分已消费、被删除或已移出队列。不会重发，请在目标对话查看。'
    if reason == 'queue-client-id-conflicts-with-receipt':
        return '队列编号对应的 client ID 与发送回执不一致；没有确认这是否是原消息。不会删除或重发，请检查 Desktop 队列。'
    if reason == 'client-id-matched-multiple-turns':
        return '同一个 client ID 匹配到多个 Desktop 轮次，不能选择其中一个作为本次发送结果。不会重发，请检查目标对话。'
    if isinstance(reason, str) and reason.startswith('queue-or-desktop-read-error:'):
        error = reason.partition(':')[2]
        detail = {'DatabaseError': '本机 SQLite 队列读取失败，可能是数据库暂不可读或结构不兼容',
                  'OperationalError': '本机队列数据库不可读，或 Desktop 队列结构与当前适配不兼容',
                  'IntegrityError': '本机队列数据库记录违反结构约束',
                  'OSError': '本机队列文件读取失败',
                  'TimeoutError': '读取 Desktop 队列或历史超时'}.get(error, f'读取时发生 {error}')
        return f'发送结果未确认：{detail}。没有重发；检查 Desktop 可见队列和目标对话后再决定。'
    if reason == 'queue-and-history-not-yet-matched':
        return ('Codex queue 返回了成功结果，但尚未在本地队列或 Desktop 已接受的 turn 历史中找到匹配编号；'
                '不能确认这条消息已入队或开始执行。不会重发，请检查目标对话的可见待发送列表和最近消息。')
    if reason:
        code = reason if re.fullmatch(r'[A-Za-z0-9._:-]{1,100}', reason) else 'unknown-status-reason'
        return (f'状态查询返回未处理的原因码 {code}，没有取得可匹配的队列行或 Desktop turn ID；'
                '发送状态不能判定。不会重发，请检查目标对话和可见待发送列表。')
    return ('状态查询没有返回受支持的队列状态，也没有可匹配的 Desktop turn ID；发送状态不能判定。'
            '不会重发，请检查目标对话和可见待发送列表。')
