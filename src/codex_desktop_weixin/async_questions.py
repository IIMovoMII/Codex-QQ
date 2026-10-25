"""Inspected Desktop question shapes. No model calls, replay, or answer submission."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from errors import redact_error_text
from models import Confidence, Notification, Signal, TurnState

CALL_ID = re.compile(r'^[A-Za-z0-9_-]{1,160}$')
TOOL_NAMES = frozenset({'request_user_input_async', 'functions.request_user_input_async'})
OPEN = '<send_user_message_question_reply>'
CLOSE = '</send_user_message_question_reply>'


def batch_key(session: str, call: str) -> str:
    return 'async-question:' + session + ':' + call


def questions(value: Any) -> list[dict]:
    if not isinstance(value, list) or not 1 <= len(value) <= 3:
        raise ValueError('unsupported-async-questions')
    result = []
    for q in value:
        if not isinstance(q, dict) or set(q) - {'title', 'options', 'question'}:
            raise ValueError('unknown-or-secret-async-question')
        title, options, question = q.get('title'), q.get('options', []), q.get('question', '')
        if (not isinstance(title, str) or not 1 <= len(title.strip()) <= 1500
                or not isinstance(question, str) or len(question) > 2000
                or not isinstance(options, list) or len(options) > 10
                or any(not isinstance(x, str) or not 1 <= len(x) <= 160 for x in options)):
            raise ValueError('unsupported-async-question-shape')
        title.encode('utf-8', errors='strict')
        question.encode('utf-8', errors='strict')
        for option in options:
            option.encode('utf-8', errors='strict')
        result.append({'title': title, 'options': options, **({'question': question} if question else {})})
    return result


def parse_call(payload: dict) -> dict | None:
    call = payload.get('call_id') or payload.get('id')
    if not isinstance(call, str) or not CALL_ID.fullmatch(call):
        return None
    args = payload.get('arguments', payload.get('input'))
    try:
        if isinstance(args, str):
            if len(args) > 16384:
                raise ValueError('arguments-too-large')
            args = json.loads(args)
        if not isinstance(args, dict) or set(args) != {'questions'}:
            raise ValueError('unsupported-arguments')
        rows = questions(args['questions'])
        return {'call_id': call, 'questions': rows, 'question_count': len(rows)}
    except (ValueError, UnicodeError, TypeError):
        # The known question tool still merits a generic notice. Unknown or
        # sensitive schemas never cause the original arguments to be forwarded.
        return {'call_id': call, 'questions': [], 'question_count': 0}


def parse_answers(text: Any) -> list[dict]:
    if not isinstance(text, str) or len(text) > 16384:
        return []
    text = text.strip()
    if not text.startswith(OPEN) or not text.endswith(CLOSE):
        return []
    try:
        raw = json.loads(text[len(OPEN):-len(CLOSE)])
        raw = [raw] if isinstance(raw, dict) else raw
        if not isinstance(raw, list) or not 1 <= len(raw) <= 3:
            return []
        output = []
        for row in raw:
            if (not isinstance(row, dict) or set(row) != {'questionItemId', 'question', 'answer'}
                    or not all(isinstance(row[k], str) for k in row)):
                return []
            ident = json.loads(row['questionItemId'])
            if (not isinstance(ident, list) or len(ident) != 3 or ident[0] != 'request_user_input_async'
                    or not isinstance(ident[1], str) or not CALL_ID.fullmatch(ident[1])
                    or isinstance(ident[2], bool) or not isinstance(ident[2], int) or not 0 <= ident[2] <= 2):
                return []
            # Do not retain the user's answer, including secrets or paths.
            output.append({'call_id': ident[1], 'index': ident[2]})
        return output
    except (ValueError, TypeError, UnicodeError):
        return []


def input_answers(content: Any) -> list[dict]:
    if (not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict)
            or content[0].get('type') not in ('text', 'input_text')):
        return []
    return parse_answers(content[0].get('text'))


def describe(rows: list[dict], indices: list[int] | None = None) -> str:
    lines = []
    for i in indices if indices is not None else range(len(rows)):
        q = rows[i]
        lines.append(f"{i + 1}. {redact_error_text(q['title'], limit=300)}")
        if q.get('question'):
            lines.append('   ' + redact_error_text(q['question'], limit=500))
        for number, option in enumerate(q['options'], 1):
            lines.append(f"   {number}) {redact_error_text(option, limit=80)}")
    body = '\n'.join(lines)
    return body if len(body) <= 1450 else body[:1450] + '\n（仅显示摘要，完整问题请在电脑查看。）'


def notification(signal: Signal, *, turn_id: str, model: str = '') -> Notification | None:
    call = signal.data.get('call_id')
    if not signal.session_id or not isinstance(call, str) or not CALL_ID.fullmatch(call):
        return None
    body = describe(signal.data.get('questions') or []) or '异步问题的格式尚未核实或包含敏感项，请在电脑查看。'
    body += '\n\n请回电脑打开这个对话回答。任务可能仍在工作，这不是暂停通知；QQ 只提醒，不提交这类答案。'
    return Notification(session_id=signal.session_id, turn_id=turn_id,
                        state=TurnState.QUESTION_AVAILABLE, confidence=Confidence.CERTAIN,
                        title='Codex 有问题需要你回答', message=body, model=model,
                        dedupe_key=batch_key(signal.session_id, call),
                        question_call_id=call, question_count=signal.data.get('question_count', 0))


def pending_batches(turn: dict) -> list[dict]:
    """Only the active native turn; queued/unaccepted steering is not an answer."""
    if turn.get('status') != 'inProgress' or not turn.get('turnId'):
        return []
    items = turn.get('items') or []
    answered = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get('type') == 'userMessage':
            answers = input_answers(item.get('content'))
        elif item.get('type') == 'steeringUserMessage' and item.get('status') == 'accepted':
            answers = input_answers(item.get('input'))
        else:
            continue
        answered.update((a['call_id'], a['index']) for a in answers)
    result, seen = [], set()
    for item in items:
        if not isinstance(item, dict) or item.get('type') != 'agentMessage' or item.get('delivery') != 'async':
            continue
        call = item.get('id')
        if not isinstance(call, str) or not CALL_ID.fullmatch(call) or call in seen:
            raise ValueError('ambiguous-async-question-identity')
        seen.add(call)
        rows = questions(item.get('questions'))
        indices = [i for i in range(len(rows)) if (call, i) not in answered]
        if indices:
            digest = hashlib.sha256(json.dumps(rows, ensure_ascii=True, sort_keys=True).encode()).hexdigest()
            result.append({'message_id': call, 'turn_id': turn['turnId'], 'questions': rows,
                           'indices': indices, 'digest': digest})
            if len(result) > 64:
                raise ValueError('too-many-async-question-batches')
    return result
