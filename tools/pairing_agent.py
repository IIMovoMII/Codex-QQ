"""Identity-only ACP endpoint for first setup. Has no Codex/control imports."""
from __future__ import annotations

import json
import os
import re
import sys


def pairing_reply(params: dict, session: str) -> str:
    envelope = params.get('_meta')
    meta = envelope.get('cc_connect') if isinstance(envelope, dict) else None
    if not isinstance(meta, dict):
        return '未收到身份信息，请使用本项目的 CC Connect 适配版。'
    user = meta.get('user_id', '')
    if (meta.get('version') != 1 or meta.get('platform') != 'qqbot'
            or not isinstance(user, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,128}', user)
            or session != 'qqbot:' + user or meta.get('session_key') != session):
        return '请在机器人私聊中发送“绑定”，当前不支持群聊配置。'
    if not isinstance(meta.get('text'), str) or meta['text'].strip() != '绑定':
        return '👋 这是首次配置模式。发送“绑定”查看自己的 OpenID；这里不能操作 Codex。'
    return ('🔗 你的 QQ OpenID：\n' + user + '\n\n'
            '回到电脑停止配置网关，再运行 configure.py --bind，填入这串编号。\n'
            '这不是 QQ 号；当前没有连接或操作 Codex。')


def handle(request: dict, session: str) -> list[dict]:
    method, ident = request.get('method'), request.get('id')
    def response(result):
        return {'jsonrpc': '2.0', 'id': ident, 'result': result}
    if method == 'initialize':
        return [response({'protocolVersion': 1, 'agentCapabilities': {'loadSession': False},
                          'agentInfo': {'name': 'codex-qq-pairing-only', 'version': '1.0'}})]
    if method == 'session/new':
        return [response({'sessionId': 'pairing-only'})]
    if method in ('session/set_mode', 'session/cancel'):
        return [response({})]
    if method == 'session/prompt':
        params = request.get('params') or {}
        reply = pairing_reply(params, session) if isinstance(params, dict) else '配置消息格式不正确。'
        return [{'jsonrpc': '2.0', 'method': 'session/update', 'params': {
            'sessionId': 'pairing-only', 'update': {'sessionUpdate': 'agent_message_chunk',
                'content': {'type': 'text', 'text': reply}}}}, response({'stopReason': 'end_turn'})]
    return [] if ident is None else [{'jsonrpc': '2.0', 'id': ident,
                                      'error': {'code': -32601, 'message': 'pairing only'}}]


def main() -> None:
    sys.stdin.reconfigure(encoding='utf-8', errors='strict')
    sys.stdout.reconfigure(encoding='utf-8', errors='strict', line_buffering=True)
    for line in sys.stdin:
        try:
            request = json.loads(line)
        except ValueError:
            continue
        if isinstance(request, dict):
            for item in handle(request, os.environ.get('CC_SESSION_KEY', '')):
                print(json.dumps(item, ensure_ascii=False))


if __name__ == '__main__':
    main()
