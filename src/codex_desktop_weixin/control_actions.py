"""Small deterministic action payloads; never an AI command router."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from errors import redact_error_text

SUPPORTED_REQUESTS = {
    'item/tool/requestUserInput': '问题',
    'item/commandExecution/requestApproval': '命令审批',
    'item/fileChange/requestApproval': '文件审批',
}
REQUEST_METHODS = {
    'item/tool/requestUserInput': 'thread-follower-submit-user-input',
    'item/commandExecution/requestApproval': 'thread-follower-command-approval-decision',
    'item/fileChange/requestApproval': 'thread-follower-file-approval-decision',
}


def signature(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def request_reference(thread_id: str, request: dict, owner: str, connection: str) -> dict:
    if request.get('method') not in SUPPORTED_REQUESTS or not isinstance(request.get('params'), dict):
        raise ValueError('unsupported-request')
    rid = request.get('id')
    if isinstance(rid, bool) or not isinstance(rid, (int, str)) or len(str(rid)) > 240:
        raise ValueError('invalid-request-id')
    params = request['params']
    if params.get('threadId') != thread_id or not isinstance(params.get('turnId'), str) or not params['turnId']:
        raise ValueError('request-target-mismatch')
    return {'thread_id': thread_id, 'request_id': rid, 'method': request['method'],
            'turn_id': params['turnId'], 'digest': signature(request), 'owner_id': owner, 'connection': connection}


def describe_request(request: dict) -> str:
    method, params = request['method'], request['params']
    if method == 'item/tool/requestUserInput':
        questions = params.get('questions')
        if not isinstance(questions, list) or not 1 <= len(questions) <= 3:
            raise ValueError('unsupported-questions')
        if len({q.get('id') for q in questions if isinstance(q, dict) and isinstance(q.get('id'), str)}) != len(questions):
            raise ValueError('ambiguous-question-ids')
        lines = []
        for i, question in enumerate(questions, 1):
            if not isinstance(question, dict) or not isinstance(question.get('id'), str) or not isinstance(question.get('question'), str):
                raise ValueError('invalid-question')
            # Secrets, onboarding credentials and login challenges stay on the PC.
            if question.get('isSecret') or question.get('is_secret'):
                raise ValueError('secret-question')
            lines.append(f"{i}. {redact_error_text(question['question'], limit=300)}")
            options = question.get('options') or []
            if not isinstance(options, list) or len(options) > 10:
                raise ValueError('too-many-options')
            for j, option in enumerate(options, 1):
                if not isinstance(option, dict) or not isinstance(option.get('label'), str):
                    raise ValueError('invalid-option')
                lines.append(f"   {j}) {redact_error_text(option['label'], limit=80)}")
        lines.append('引用回复答案或选项序号。多个问题逐行填写“1=答案”“2=答案”。')
        return '\n'.join(lines)
    command = params.get('command')
    if isinstance(command, list):
        command = ' '.join(str(x) for x in command)
    detail = command or params.get('reason') or '请核对电脑中的原生审批详情。'
    return f"{redact_error_text(str(detail), limit=650)}\n目录：{redact_error_text(str(params.get('cwd') or params.get('grantRoot') or '原生请求指定范围'), limit=200)}\n引用回复“允许”“拒绝”或“取消”。仅处理本次请求，不授予永久权限。"


def request_payload(request: dict, text: str) -> dict:
    method = request['method']
    if method != 'item/tool/requestUserInput':
        choices = {'允许': 'accept', '拒绝': 'decline', '取消': 'cancel'}
        if text not in choices:
            raise ValueError('请回复“允许”“拒绝”或“取消”。')
        return {'decision': choices[text]}
    # Revalidate schema before any write; the card stores identity only.
    describe_request(request)
    questions = request['params']['questions']
    if len(questions) == 1:
        values = [text]
    else:
        parts = [x.strip() for x in text.splitlines() if x.strip()]
        if len(parts) != len(questions) or any(not x.startswith(f'{i}=') for i, x in enumerate(parts, 1)):
            raise ValueError('多个问题请逐行填写“1=答案”“2=答案”，每个问题都要回答。')
        values = [x.split('=', 1)[1].strip() for x in parts]
    answers = {}
    for question, value in zip(questions, values):
        if not value or len(value) > 1500:
            raise ValueError('答案不能为空，单个答案最多 1,500 字。')
        try:
            value.encode('utf-8', errors='strict')
        except UnicodeEncodeError as error:
            raise ValueError('答案包含损坏字符，未提交；请重新发送原文。') from error
        options = question.get('options') or []
        if value.isdigit() and options:
            index = int(value) - 1
            if not 0 <= index < len(options):
                raise ValueError('答案序号不在原问题的选项里。')
            value = options[index]['label']
        answers[question['id']] = {'answers': [value]}
    return {'response': {'answers': answers}}


def normalize_saved_queue(value: Any) -> list[dict]:
    if not isinstance(value, list) or len(value) > 200:
        raise ValueError('unsupported-local-queue')
    result, ids = [], set()
    for row in value:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not 1 <= len(row['id']) <= 240 or row['id'] in ids:
            raise ValueError('ambiguous-queued-message')
        ids.add(row['id'])
        status = (row.get('submission') or {}).get('status', 'queued')
        host = (row.get('submission') or {}).get('hostId', 'local')
        if host != 'local':
            continue
        text = row.get('text')
        if not isinstance(text, str):
            raise ValueError('unsupported-queue-text')
        result.append({'id': row['id'], 'summary': redact_error_text(text.replace('\n', ' '), limit=160),
                       'status': status, 'digest': signature(row), 'paused': bool(row.get('pausedReason'))})
    return result
