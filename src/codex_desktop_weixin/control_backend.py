"""按实际接口能力连接现有 Desktop 对话的窗口所有者。"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from typing import Any

from desktop_control import (DesktopClient, DesktopControlError, QUEUED_FOLLOWUPS_METHOD,
                             WindowsDesktopPipe, pause_desktop, pipe_server_identity)


class NativeBackend:
    def __init__(self, *, pipe_factory: Any = WindowsDesktopPipe, contract_check: Any = None,
                 app_tools: Any = None, catalog: Any = None) -> None:
        self.pipe_factory = pipe_factory
        self.contract_check = contract_check
        self.process_image: str | None = None
        self.app_tools = app_tools
        self.catalog = catalog
        self._queue_locks: dict[str, threading.Lock] = {}

    def native_tools(self) -> Any:
        if self.app_tools is None:
            from native_app_tools import NativeAppTools
            self.app_tools = NativeAppTools()
        return self.app_tools

    def rename(self, thread_id: str, title: str, connection: str, operation_id: str) -> dict:
        return self.native_tools().rename(thread_id, title, connection, operation_id)

    def creation_projects(self, source_thread_id: str, connection: str) -> list[dict]:
        return self.native_tools().projects(source_thread_id, connection)

    def open_thread(self, thread_id: str, connection: str, operation_id: str) -> dict:
        return self.native_tools().open_thread(thread_id, connection, operation_id)

    def create_thread(self, source_thread_id: str, project: dict, prompt: str,
                      title: str | None, connection: str, operation_id: str) -> dict:
        result = self.native_tools().create(source_thread_id, project, prompt, title, connection, operation_id)
        if result.get('thread_id'):
            try:
                settings = self.read_settings(result['thread_id'])
                result['settings'] = {k: settings.get(k) for k in ('model', 'effort')}
            except (DesktopControlError, OSError, TimeoutError, ValueError):
                # Creation already happened. A setting lookup is never a reason
                # to create or submit the first task again.
                result['settings'] = None
        return result

    def require_extended_contract(self) -> None:
        if self.contract_check is not None:
            self.contract_check()
            return
        from control_contract import check_contract
        if self.process_image is None:
            self.probe()
        check_contract(self.process_image or '')

    def probe(self) -> tuple[str, float]:
        with self.pipe_factory() as pipe:
            client = DesktopClient(pipe)
            client.initialize()
            identity, created, image = pipe_server_identity(pipe)
            self.process_image = image
            return identity, created

    def snapshot(self, thread_id: str) -> dict[str, Any]:
        with self.pipe_factory() as pipe:
            if hasattr(pipe, "deadline"):
                pipe.deadline = min(pipe.deadline, time.monotonic() + 3.0)
            client = DesktopClient(pipe)
            client.initialize()
            owner = client.owner(thread_id)
            client.following(thread_id, True, owner)
            try:
                # A follower's owner discovery causes the owner to publish a snapshot.
                client.owner(thread_id)
                client.request("thread-follower-read-model-settings", {"conversationId": thread_id}, version=1, target_client_id=owner)
                while True:
                    event = client.broadcasts.get("thread-stream-state-changed", {})
                    params = event.get("params", {})
                    change = params.get("change", {})
                    state = change.get("conversationState")
                    if (params.get("conversationId") == thread_id and params.get('hostId') == 'local'
                            and event.get('sourceClientId') == owner and event.get('version') == 11
                            and change.get('type') == 'snapshot' and isinstance(state, dict)
                            and state.get('id') == thread_id):
                        return {**self._view(state), 'owner_id': owner}
                    frame = pipe.read()
                    if frame.get("type") == "broadcast":
                        client.broadcasts[str(frame.get("method"))] = frame
                    elif frame.get("type") == "client-discovery-request":
                        pipe.write({"type": "client-discovery-response", "requestId": frame.get("requestId"), "response": {"canHandle": False}})
            finally:
                try:
                    client.following(thread_id, False, owner)
                except (OSError, TimeoutError, DesktopControlError):
                    pass

    def ensure_owner(self, thread_id: str) -> None:
        with self.pipe_factory() as pipe:
            client = DesktopClient(pipe)
            client.initialize()
            client.owner(thread_id)

    def status(self, thread_id: str) -> str:
        from runtime_io import default_data_dir
        from launcher import read_monitor_pid, _query_process_identity
        root = default_data_dir()
        try:
            value = json.loads((root / "state" / "control-status.json").read_text(encoding="utf-8"))
            pid = read_monitor_pid(root, require_ready=True)
            if pid and value.get("monitor_pid") == pid and value.get("monitor_created") == _query_process_identity(pid).get("process_start_time"):
                state = (value.get("threads", {}).get(thread_id) or {}).get("state")
                label = {"running": "运行中", "waiting_approval": "等待审批", "waiting_input": "等待回答", "completed": "空闲（已完成）", "failed": "出错", "interrupted": "已暂停", "disconnected": "未连接（任务结果未知）", "crashed": "异常停止", "idle": "空闲"}.get(state)
                if label:
                    return label
        except (OSError, ValueError, TypeError):
            pass
        try:
            self.ensure_owner(thread_id)
        except (OSError, TimeoutError, DesktopControlError, ValueError):
            return "未载入或当前不可连接"
        return "已载入，状态未记录"

    def read_settings(self, thread_id: str) -> dict[str, Any]:
        with self.pipe_factory() as pipe:
            client = DesktopClient(pipe)
            client.initialize()
            owner = client.owner(thread_id)
            response = client.request("thread-follower-read-model-settings", {"conversationId": thread_id}, version=1, target_client_id=owner)
            if response.get("handledByClientId") != owner:
                raise DesktopControlError("owner-mismatch")
            value = response["result"].get("settings")
            if not isinstance(value, dict):
                raise DesktopControlError("invalid-settings")
            return {"model": value.get("model"), "effort": value.get("reasoningEffort"), "settings": {}, "permissions": {}, "runtime": {}, "raw": value}

    def update_model_settings(self, thread_id: str, changes: dict[str, str], expected: dict[str, Any],
                              connection: str | None = None) -> dict[str, Any]:
        """Conditional next-turn updates through the existing owner, not config.

        Partial fields preserve permissions/plugins/collaboration settings. A lost
        reply is never retried, including when the subsequent read also fails.
        """
        if not changes or set(changes) - {"model", "effort"} or not all(isinstance(v, str) and v for v in changes.values()):
            return {"state": "rejected", "reply": "设置字段不合法，未修改。"}
        before = self.read_settings(thread_id)
        if connection is not None:
            try:
                if self.probe()[0] != connection:
                    return {"state": "rejected", "reply": "Codex 连接已变化，未修改设置。请重新查看设置。"}
            except (DesktopControlError, OSError, TimeoutError):
                return {"state": "rejected", "reply": "无法核实 Codex 当前连接，未修改设置。"}
        if any(before.get(k) != expected.get(k) for k in ("model", "effort")):
            return {"state": "rejected", "reply": "对话设置已发生变化。这次未修改，请重新查看选项。"}
        if all(before.get(k) == v for k, v in changes.items()):
            return {"state": "confirmed", "reply": "已经是所选设置，没有重复写入。", "settings": before}
        result = self.call(thread_id, "thread-follower-update-thread-settings", {
            "threadSettings": changes,
            "condition": {"ifModelEquals": before.get("model"), "ifEffortEquals": before.get("effort")},
        }, 2, expected_connection=connection)
        if result["state"] != "confirmed":
            return {"state": result["state"], "reply": "设置提交后未取得确认，可能已生效。不会自动重试，请重新查看设置。" if result["state"] == "unknown" else "无法提交设置，未修改。"}
        if result["result"].get("applied") is not True:
            return {"state": "rejected", "reply": "窗口没有接受这次设置修改；请重新查看设置。"}
        try:
            after = self.read_settings(thread_id)
        except (DesktopControlError, TimeoutError, OSError, ValueError):
            return {"state": "unknown", "reply": "窗口已受理，但回读没有确认。不会重复修改，请在电脑查看。"}
        if not all(after.get(k) == v for k, v in changes.items()):
            return {"state": "unknown", "reply": "窗口已受理，但实际设置与请求不一致；未自动重写或降档，请在电脑查看。"}
        return {"state": "confirmed", "reply": "设置已保存并回读确认。只影响此对话后续轮次，不改变正在执行的本轮。", "settings": after}

    @staticmethod
    def _view(state: dict[str, Any]) -> dict[str, Any]:
        # Do not retain the full transcript after extracting control state.
        history = (state.get("turnHistory") or {}).get("history") or {}
        entities = history.get("entitiesByKey") or {}
        turns = [entities[e["value"]] for island in history.get("islands", [])
                 for e in island.get("entries", []) if isinstance(e, dict)
                 and e.get("value") in entities and isinstance(entities[e["value"]], dict)
                 and entities[e["value"]].get("turnId")]
        turns = turns or state.get("turns") or []
        latest = turns[-1] if turns else {}
        params = latest.get('params') if isinstance(latest.get('params'), dict) else {}
        context = params.get('context') if isinstance(params.get('context'), dict) else {}
        collab = state.get("latestCollaborationMode") or {}
        settings = state.get("latestThreadSettings") or {}
        compactions = [{"id": item.get('id'), "turn_id": turn.get('turnId'),
                        "status": item.get('status'), "source": item.get('source'),
                        "turn_status": turn.get('status')}
                       for turn in turns[-8:] for item in turn.get('items', [])
                       if isinstance(item, dict) and item.get('type') == 'contextCompaction'][-8:]
        from async_questions import pending_batches
        from submission_receipts import accepted_submissions
        try:
            async_batches, async_unverified = pending_batches(latest), False
        except (ValueError, UnicodeError, TypeError):
            async_batches, async_unverified = [], True
        return {
            "id": state.get("id"), "model": state.get("latestModel"),
            "effort": state.get("latestReasoningEffort") or settings.get("effort") or (collab.get("settings") or {}).get("reasoning_effort"),
            "settings": settings, "permissions": state.get("currentPermissions"),
            "runtime": state.get("threadRuntimeStatus"), "resumeState": state.get("resumeState"),
            "turn": {k: latest.get(k) for k in ("id", "turnId", "status", "state", "clientUserMessageId")},
            "requests": state.get("requests") or [],
            'compactions': compactions, 'pending': bool(state.get('unconfirmedTurnSubmissions')),
            'cwd': state.get('cwd'),
            'collaboration_mode': state.get('latestCollaborationMode'),
            'goal_known': 'threadGoal' in state,
            'goal': state.get('threadGoal'),
            'workspace_roots': context.get('workspaceRoots'),
            'async_questions': async_batches, 'async_questions_unverified': async_unverified,
            'accepted_submissions': accepted_submissions(turns),
            'mode': state.get('mode'), 'thread_start_kind': state.get('threadStartKind'),
        }

    @staticmethod
    def _message_route(state: dict) -> str:
        # A paused queue is not an active task. Normal composer submission
        # starts a fresh turn; it does not wait for that queue to auto-resume.
        if state.get('pending') or state.get('resumeState') != 'resumed':
            return 'unavailable'
        turn = state.get('turn') or {}
        runtime = (state.get('runtime') or {}).get('type')
        if turn.get('status') == 'interrupted' and runtime == 'active':
            return 'stopping'
        if turn.get('status') == 'inProgress' or runtime == 'active':
            return 'queued'
        if runtime != 'idle' or state.get('requests'):
            return 'unavailable'
        if turn.get('status') in ('completed', 'interrupted', 'failed'):
            return 'started'
        return 'unavailable'

    def enqueue_native_follow_up(self, thread_id: str, text: str, connection: str,
                                 operation_id: str) -> dict:
        """Append one message to Desktop's visible native follow-up queue.

        The Desktop IPC accepts a whole snapshot, not an atomic append. Read the
        current local snapshot, preserve every row, submit once to the verified
        owner, then read it back. A lost response or changed snapshot is unknown
        and is never retried through the hidden app-server queue.
        """
        catalog = self.catalog
        if catalog is None or not hasattr(catalog, 'saved_queue_rows'):
            return {'state': 'rejected', 'reply': '当前无法读取 Desktop 可见待发送列表，未发送、未排队。请在电脑查看。'}
        lock = self._queue_locks.setdefault(thread_id, threading.Lock())
        with lock:
            try:
                current = self.snapshot(thread_id)
                if self._message_route(current) != 'queued':
                    return {'state': 'rejected', 'reply': '对话状态已变化，这条消息未发送、未排队；请重新查询后发送。'}
                identity, _ = self.probe()
                if identity != connection or current.get('owner_id') is None:
                    return {'state': 'rejected', 'reply': 'Codex 连接或对话所有者已变化，这条消息未发送、未排队。'}
                rows = catalog.saved_queue_rows(thread_id)
                if any(row.get('id') == operation_id for row in rows):
                    return {'state': 'rejected', 'reply': '这条消息编号已存在，未重复加入队列。'}
                if len(rows) >= 200:
                    return {'state': 'rejected', 'reply': 'Desktop 可见待发送列表已达到上限，这条消息未发送、未排队。'}
                # The native operation replaces the whole snapshot.  Take a
                # second read immediately before dispatch so a concurrent
                # Desktop/UI update is rejected instead of being overwritten.
                latest_rows = catalog.saved_queue_rows(thread_id)
                if latest_rows != rows:
                    return {'state': 'rejected', 'reply': 'Desktop 可见队列正在变化，这条消息未发送、未排队；请重新查看队列后重发。'}
                row = {
                    'id': operation_id,
                    'text': text,
                    'createdAt': int(time.time() * 1000),
                    'submission': {'hostId': 'local', 'status': 'queued'},
                }
                next_rows = [*rows, row]
                result = self.call(
                    thread_id, 'thread-follower-set-queued-follow-ups-state',
                    {'state': {thread_id: next_rows}, 'discardedMessageIds': []}, 1,
                    expected_owner=current['owner_id'], expected_connection=connection,
                )
            except (DesktopControlError, OSError, TimeoutError, ValueError, TypeError, KeyError):
                return {'state': 'rejected', 'reply': '无法核实 Desktop 当前待发送列表，这条消息未发送、未排队。请在电脑查看。'}
            if result['state'] != 'confirmed':
                return {'state': result['state'], 'reply':
                        '已尝试加入 Desktop 可见待发送列表，但没有取得确认；不会改走隐藏队列或自动重试，请查看电脑。'
                        if result['state'] == 'unknown' else
                        'Desktop 当前没有接受这条排队消息，未发送、未排队。'}
            try:
                from control_actions import normalize_saved_queue
                entry = None
                for _ in range(5):
                    after_rows = catalog.saved_queue_rows(thread_id)
                    matches = [item for item in after_rows if item.get('id') == operation_id]
                    preserved = all(any(item == original for item in after_rows) for original in rows)
                    if preserved and len(matches) == 1 and matches[0] == row:
                        entry = normalize_saved_queue([matches[0]])[0]
                        break
                    time.sleep(0.05)
                if entry is None:
                    raise ValueError('queue-readback-mismatch')
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                return {'state': 'unknown', 'reply':
                        'Desktop 已受理排队请求，但回读未能确认这条消息；不会重复排队，请在电脑查看。'}
            return {'state': 'confirmed', 'mode': 'queued', 'native': True,
                    'queue_id': operation_id, 'entry': entry}

    def submit_message(self, thread_id: str, text: str, connection: str,
                       operation_id: str, queue_func: Any) -> dict:
        """Normal QQ text: start idle work or use Desktop's visible busy queue.

        A lost native confirmation never becomes a second submission via another
        path. The legacy queue callback is retained for compatibility but is
        intentionally never used for this normal-message path.
        """
        try:
            text.encode('utf-8', errors='strict')
            if (not 1 <= len(text.strip()) <= 4000 or not re.fullmatch(r'[A-Za-z0-9_-]{20,100}', thread_id)
                    or not re.fullmatch(r'[A-Za-z0-9_-]{20,100}', operation_id)):
                raise ValueError('invalid-message')
        except (ValueError, UnicodeError, TypeError):
            return {'state': 'rejected', 'reply': '消息或编号格式无效，未发送；请重新输入原文。'}
        before = self.control_state(thread_id)
        identity, _ = self.probe()
        if identity != connection:
            return {'state': 'rejected', 'reply': 'Codex 连接已变化，这条消息未发送；恢复后请重新发。'}
        route = self._message_route(before)
        if route == 'started':
            if self.contract_check is None:
                from control_contract import check_queue_contract
                check_queue_contract(self.process_image or '')
            # Re-read immediately before an idle start. A newly busy owner gets
            # a queue submission; this is a pre-dispatch choice, never a retry.
            current = self.snapshot(thread_id)
            if current['owner_id'] != before['owner_id']:
                return {'state': 'rejected', 'reply': '对话所有者已变化，未发送。请恢复连接后重新发。'}
            before, route = current, self._message_route(current)
        if route == 'stopping':
            return {'state': 'rejected', 'reply': '原生暂停仍在收尾，这条新消息未发送、未排队；待电脑显示停止后重新发。'}
        if route == 'unavailable':
            return {'state': 'rejected', 'reply': '原生状态尚未就绪或上次提交未确认，未发送新消息、未排队。请先在电脑核对。'}
        if route == 'queued':
            return self.enqueue_native_follow_up(thread_id, text, connection, operation_id)
        if (before.get('mode') not in (None, 'default')
                or before.get('thread_start_kind') not in (None, 'default')
                or not isinstance(before.get('cwd'), str) or not before['cwd']):
            return {'state': 'rejected', 'reply': '这类对话的直接开始通路尚未核实，未发送、未强制排队。请在电脑处理。'}
        inputs = [{'type': 'text', 'text': text, 'text_elements': []}]
        roots = before.get('workspace_roots')
        if not isinstance(roots, list) or not roots or not all(isinstance(x, str) for x in roots):
            roots = [before['cwd']]
        restore = {'id': operation_id, 'text': text, 'cwd': before['cwd'],
                   'createdAt': int(time.time() * 1000), 'context': {
                       'prompt': text, 'turnTrigger': 'composer', 'addedFiles': [],
                       'fileAttachments': [], 'imageAttachments': [], 'ideContext': None,
                       'workspaceRoots': roots}}
        result = self.call(thread_id, 'thread-follower-start-turn', {'turnStart': {
            'request': {'threadId': thread_id, 'input': inputs,
                        'clientUserMessageId': operation_id, 'turnTrigger': 'composer'},
            'context': {'inheritThreadSettings': True, 'restoreMessage': restore, 'attachments': []},
        }}, 2, expected_owner=before['owner_id'], expected_connection=connection)
        value = (result.get('result') or {}).get('result') or {}
        turn = value.get('turn') if isinstance(value, dict) else None
        if (result['state'] == 'confirmed' and isinstance(turn, dict)
                and isinstance(turn.get('id'), str) and turn['id']):
            return {'state': 'confirmed', 'mode': 'started', 'turn_id': turn['id'],
                    'client_id': operation_id, 'turn_status': turn.get('status')}
        return {'state': result['state'] if result['state'] != 'confirmed' else 'unknown',
                'reply': '新一轮请求未取得有效确认，可能已经开始；不会再次排队或引导，请查看电脑。'
                         if result['state'] != 'rejected' else '原生窗口或连接已变化，开始请求未发出；没有改走排队。'}

    def steer(self, thread_id: str, text: str, connection: str, operation_id: str) -> dict:
        """Desktop's execution-time active-turn semantics, explicitly accepted by the user."""
        try:
            text.encode('utf-8', errors='strict')
            if not 1 <= len(text.strip()) <= 4000:
                raise ValueError('invalid-steer-text')
            before = self.control_state(thread_id)
            current_identity, _ = self.probe()
            if current_identity != connection:
                raise DesktopControlError('desktop-generation-changed')
            if self.contract_check is None:
                from control_contract import check_steer_contract
                check_steer_contract(self.process_image or '')
        except (DesktopControlError, OSError, TimeoutError, ValueError, UnicodeError):
            return {'state': 'rejected', 'reply': '无法确认当前窗口和引导正文，未执行；请恢复连接后重新发送。'}
        if before['turn'].get('status') != 'inProgress':
            return {'state': 'rejected', 'reply': '此对话当前没有正在运行的任务，未引导，也没有排入新一轮。'}
        if not before['turn'].get('turnId') or not isinstance(before.get('cwd'), str) or not before['cwd']:
            return {'state': 'rejected', 'reply': '任务仍在初始化，暂无法确认原生引导上下文；未排队，请稍后重发。'}
        roots = before.get('workspace_roots')
        if not isinstance(roots, list) or not roots or not all(isinstance(x, str) for x in roots):
            roots = [before['cwd']]
        inputs = [{'type': 'text', 'text': text, 'text_elements': []}]
        context = {'workspaceRoots': roots, 'commentAttachments': []}
        if isinstance(before.get('collaboration_mode'), dict):
            context['collaborationMode'] = before['collaboration_mode']
        # Restore metadata is for native optimistic UI only. No model, effort,
        # permission, goal, or expected-turn override is submitted.
        result = self.call(thread_id, 'thread-follower-steer-turn', {
            'input': inputs, 'clientUserMessageId': operation_id, 'attachments': [],
            'restoreMessage': {'input': inputs, 'cwd': before['cwd'], 'context': context},
        }, 1, expected_owner=before['owner_id'], expected_connection=connection)
        response = result.get('result')
        value = response.get('result') if isinstance(response, dict) else None
        if result['state'] == 'confirmed' and isinstance(value, dict) and isinstance(value.get('turnId'), str) and value['turnId']:
            return {'state': 'confirmed', 'turn_id': value['turnId'],
                    'reply': '引导已被原生入口接受，作用于执行时的当前任务，没有另开一轮。'}
        error = str(result.get('error', '')).lower()
        if ('no active turn to steer' in error or 'is not being streamed' in error
                or 'active turn already ended' in error):
            return {'state': 'rejected', 'reply': '执行时已经没有活动任务，引导未接受；没有转成普通发送或排队。'}
        if result['state'] == 'rejected':
            return {'state': 'rejected', 'reply': '当前窗口没有接受引导；未改成普通消息或排队。请核对连接和对话。'}
        return {'state': result['state'] if result['state'] != 'confirmed' else 'unknown',
                'reply': '引导未取得有效确认，可能已生效。不会自动重试或改成下一轮消息，请查看电脑。'}

    def fork_boundary(self, thread_id: str, connection: str) -> dict:
        from control_actions import signature
        state = self.control_state(thread_id)
        if (not state.get('goal_known') or state.get('goal') is not None or state.get('pending')
                or state['turn'].get('status') != 'completed' or state.get('requests')
                or (state.get('runtime') or {}).get('type') == 'active'):
            raise DesktopControlError('latest-fork-source-not-idle-or-has-goal')
        boundary = self.native_tools().boundary(thread_id, connection)
        if boundary['turn_id'] != state['turn'].get('turnId'):
            raise DesktopControlError('latest-fork-boundary-mismatch')
        baseline = {'boundary': boundary, 'owner_id': state['owner_id'],
                    'settings_digest': signature({k: state.get(k) for k in
                        ('model', 'effort', 'settings', 'permissions', 'collaboration_mode', 'cwd')})}
        return baseline

    def fork_latest(self, thread_id: str, baseline: dict, connection: str, operation_id: str) -> dict:
        try:
            if self.fork_boundary(thread_id, connection) != baseline:
                return {'state': 'rejected', 'reply': '源对话或分支位置已变化，未分支。请重新查看分支范围。'}
        except (DesktopControlError, OSError, TimeoutError, ValueError):
            return {'state': 'rejected', 'reply': '源对话正在运行、存在自动目标或分支边界不可核实，未分支。请回电脑核对。'}
        result = self.native_tools().fork_latest(thread_id, connection, operation_id)
        ident = result.get('thread_id')
        if result['state'] != 'confirmed' or not ident:
            return result
        try:
            source_after = self.fork_boundary(thread_id, connection)
            child_boundary = self.native_tools().boundary(ident, connection)
            child = self.control_state(ident)
            if (source_after != baseline or child_boundary['turn_id'] != baseline['boundary']['turn_id']
                    or not child.get('goal_known') or child.get('goal') is not None
                    or child['turn'].get('turnId') != child_boundary['turn_id']
                    or child['turn'].get('status') != 'completed' or child.get('pending') or child.get('requests')
                    or (child.get('runtime') or {}).get('type') == 'active'):
                raise DesktopControlError('fork-range-or-runtime-unconfirmed')
        except (DesktopControlError, OSError, TimeoutError, ValueError):
            return {'state': 'unknown', 'thread_id': ident,
                    'reply': '已取得分支编号，但源历史或分支范围没有回读确认。请先在电脑核对；未发送新任务，也不会再次分支。'}
        return {**result, 'settings': {k: child.get(k) for k in ('model', 'effort')},
                'title': child_boundary.get('title'),
                'reply': '已从最近完成位置分支，并核对新对话的原生历史边界；原对话不变，没有发送新任务。'}

    def call(self, thread_id: str, method: str, params: dict[str, Any], version: int,
             *, expected_owner: str | None = None, expected_connection: str | None = None) -> dict[str, Any]:
        sent = False
        try:
            with self.pipe_factory() as pipe:
                client = DesktopClient(pipe)
                client.initialize()
                # The production Windows pipe exposes a process identity handle.
                # Small protocol fakes and alternate tested transports do not;
                # their caller has already performed the generation probe.
                if expected_connection is not None and hasattr(pipe, 'handle'):
                    if pipe_server_identity(pipe)[0] != expected_connection:
                        raise DesktopControlError('desktop-changed-before-dispatch')
                owner = client.owner(thread_id)
                if expected_owner is not None and owner != expected_owner:
                    raise DesktopControlError('owner-changed-before-dispatch')
                if self.contract_check is None:
                    from control_contract import check_method_contract
                    check_method_contract(pipe_server_identity(pipe)[2], method, version)
                # Recheck directly before the mutating RPC. A restarted Desktop
                # can reuse the same thread owner after the initial discovery;
                # never send an old QQ card into that new generation.
                if expected_connection is not None and hasattr(pipe, 'handle'):
                    if pipe_server_identity(pipe)[0] != expected_connection:
                        raise DesktopControlError('desktop-changed-before-dispatch')
                sent = True
                if method == QUEUED_FOLLOWUPS_METHOD:
                    state = params.get('state')
                    rows = state.get(thread_id) if isinstance(state, dict) else None
                    discarded = params.get('discardedMessageIds', [])
                    if not isinstance(rows, list) or not isinstance(discarded, list):
                        raise DesktopControlError('invalid-queue-state')
                    result = client.set_queued_follow_ups(thread_id, rows, owner, discarded)
                    return {"state": "confirmed", "result": result}
                response = client.request(method, {"conversationId": thread_id, **params}, version=version, target_client_id=owner)
                if response.get("handledByClientId") != owner:
                    raise DesktopControlError("owner-mismatch")
                return {"state": "confirmed", "result": response["result"]}
        except (AttributeError, OSError, TimeoutError, DesktopControlError, ValueError) as error:
            return {"state": "unknown" if sent else "rejected", "error": str(error)[:120]}

    def control_state(self, thread_id: str) -> dict:
        self.require_extended_contract()
        return self.snapshot(thread_id)

    def submission_status(self, receipt: dict, catalog: Any, connection: str) -> dict:
        from submission_receipts import observed_status
        try:
            identity, _ = self.probe()
            if identity != connection or receipt['connection'] != connection:
                raise DesktopControlError('receipt-connection-changed')
            if self.contract_check is None:
                from control_contract import check_queue_contract
                check_queue_contract(self.process_image or '')
            else:
                self.contract_check()
        except (DesktopControlError, OSError, TimeoutError, ValueError, KeyError):
            return {'status': 'unconfirmed', 'client_id': receipt.get('client_id'), 'turn_id': None}
        return observed_status(receipt, catalog.queue_path, self.snapshot)

    def remove_submission(self, receipt: dict, catalog: Any, connection: str) -> dict:
        """Native single-item removal, never a database deletion or delete-then-steer."""
        from submission_receipts import read_queued_item, digest
        status = self.submission_status(receipt, catalog, connection)
        if status['status'] != 'queued' or not status.get('payload_verified'):
            return {'state': 'rejected', 'reply': '这条消息已进入对话、内容变化或排队状态无法核实，未删除。请查询状态或查看电脑。'}
        try:
            queued = read_queued_item(catalog.queue_path, receipt['thread_id'], receipt['queue_id'],
                                      text_digest=receipt['text_digest'])
            if queued is None or not queued['payload_verified']:
                raise ValueError('queue-item-no-longer-verified')
            before = self.snapshot(receipt['thread_id'])
        except (OSError, TimeoutError, ValueError, DesktopControlError, sqlite3.Error):
            return {'state': 'rejected', 'reply': '原排队记录已变化或窗口无法核实，未删除；请重新查询状态。'}
        result = self.call(receipt['thread_id'], 'thread-follower-remove-queued-message',
                           {'messageId': receipt['queue_id']}, 1, expected_owner=before['owner_id'],
                           expected_connection=connection)
        if result['state'] != 'confirmed':
            return {'state': result['state'], 'reply': '删除没有取得有效确认，可能已生效；不会重试，也不会转为引导。请查看电脑。'}
        removed = result['result'].get('removed')
        if removed is None:
            return {'state': 'rejected', 'reply': '原生队列已没有这一条；它可能已经开始或被移除。没有删除其他消息。'}
        message = removed.get('message') if isinstance(removed, dict) else None
        if (not isinstance(message, dict) or message.get('id') != receipt['queue_id']
                or not isinstance(message.get('text'), str) or digest(message['text']) != receipt['text_digest']):
            return {'state': 'unknown', 'reply': '移除返回的记录未能与原回执完整对应，结果未确认；不会重复删除，请核对电脑。'}
        return {'state': 'confirmed', 'reply': '原生窗口已移除这一个排队条目，没有清空其他消息。若已被消费，删除不能撤回模型已经开始的工作。'}

    def remove_saved_queue_item(self, thread_id: str, entry: dict, catalog: Any,
                                connection: str | None = None) -> dict:
        self.require_extended_contract()
        current = next((x for x in catalog.saved_queue(thread_id) if x['id'] == entry['id']), None)
        if current is None or current['digest'] != entry['digest'] or current['status'] != 'queued':
            return {'state': 'rejected', 'reply': '这条排队消息已开始、变化或不再存在，未删除。请重新查看队列。'}
        before = self.snapshot(thread_id)
        result = self.call(thread_id, 'thread-follower-remove-queued-message', {'messageId': entry['id']}, 1,
                           expected_owner=before['owner_id'], expected_connection=connection)
        if result['state'] != 'confirmed':
            return {'state': result['state'], 'reply': '删除未取得确认，可能已生效；不会重复删除，请重新查看队列。' if result['state'] == 'unknown' else '无法删除，未发送操作。'}
        removed = result['result'].get('removed')
        if removed is None:
            return {'state': 'rejected', 'reply': '原生队列中已没有这条消息，没有删除其他条目；它可能已经开始。'}
        from control_actions import signature
        message = removed.get('message') if isinstance(removed, dict) else None
        if not isinstance(message, dict) or message.get('id') != entry['id'] or signature(message) != entry['digest']:
            return {'state': 'unknown', 'reply': '窗口已受理移除，但返回记录变化，不能确认是否赶在发送前删除。不会重试，请检查电脑。'}
        return {'state': 'confirmed', 'reply': '已移除这一条待发送消息，未清空其他消息。已经开始的模型任务不能靠删除队列撤回。'}

    def respond_to_request(self, data: dict, text: str) -> dict:
        self.require_extended_contract()
        from control_actions import request_payload, signature, REQUEST_METHODS
        state = self.snapshot(data['thread_id'])
        if state['owner_id'] != data['owner_id']:
            return {'state': 'rejected', 'reply': '原请求的窗口所有者已变化，请重新查询待办。'}
        matches = [r for r in state['requests'] if isinstance(r, dict) and r.get('id') == data['request_id'] and r.get('method') == data['method']]
        request = matches[0] if len(matches) == 1 else None
        if request is None or signature(request) != data['digest']:
            return {'state': 'rejected', 'reply': '这项问题或审批已处理、过期或发生变化，没有发送新任务。'}
        try:
            payload = request_payload(request, text)
        except ValueError as error:
            return {'state': 'rejected', 'reply': str(error)}
        result = self.call(data['thread_id'], REQUEST_METHODS[data['method']],
                           {'requestId': data['request_id'], **payload}, 1,
                           expected_owner=data['owner_id'], expected_connection=data.get('connection'))
        if result['state'] != 'confirmed' or result['result'].get('ok') is not True:
            return {'state': result['state'] if result['state'] != 'confirmed' else 'unknown',
                    'reply': '答复未取得有效确认，可能已提交；不会自动重发。请重新查询待办。'}
        # The owner can return ok after another client has already resolved it.
        # Do not claim our decision won that race or that the model has resumed.
        return {'state': 'confirmed', 'reply': '窗口已受理本次答复；若已在电脑处理，以原生处理结果为准。未发送新的模型任务。'}

    def update_permissions(self, thread_id: str, changes: dict, baseline: dict,
                           connection: str | None = None) -> dict:
        self.require_extended_contract()
        allowed = {'sandboxPolicy', 'approvalPolicy', 'approvalsReviewer', 'activePermissionProfile'}
        if (set(changes) != allowed or changes.get('approvalPolicy') not in ('never', 'on-request')
                or changes.get('approvalsReviewer') != 'user' or changes.get('activePermissionProfile') is not None
                or not isinstance(changes.get('sandboxPolicy'), dict)
                or changes['sandboxPolicy'].get('type') not in ('readOnly', 'workspaceWrite', 'dangerFullAccess')):
            return {'state': 'rejected', 'reply': '权限组合不合法，未修改。'}
        before = self.snapshot(thread_id)
        from control_actions import signature
        current = before.get('settings') or {}
        effective = {**(before.get('permissions') or {}), **{k: v for k, v in current.items()
                    if k in ('sandboxPolicy', 'approvalPolicy', 'permissions', 'activePermissionProfile', 'approvalsReviewer')}}
        if signature(effective) != baseline['digest'] or before['owner_id'] != baseline['owner_id']:
            return {'state': 'rejected', 'reply': '权限或所有者已变化，未修改。请重新查看权限。'}
        # Only explicit next-turn permission fields; never a current-turn id.
        result = self.call(thread_id, 'thread-follower-update-thread-settings', {
            'threadSettings': changes,
            'condition': {'ifModelEquals': before['model'], 'ifEffortEquals': before['effort']},
        }, 2, expected_owner=before['owner_id'], expected_connection=connection)
        if result['state'] != 'confirmed' or result['result'].get('applied') is not True:
            return {'state': result['state'] if result['state'] != 'confirmed' else 'rejected', 'reply': '权限没有得到有效确认；可能已生效时不会重复修改，请查看电脑。'}
        try:
            after = self.snapshot(thread_id)
            saved = after.get('settings') or {}
            if not all(saved.get(k) == v for k, v in changes.items()):
                raise DesktopControlError('permissions-readback-mismatch')
        except (OSError, TimeoutError, ValueError, DesktopControlError):
            return {'state': 'unknown', 'reply': '窗口已受理权限修改，但回读未确认；不会重试，请查看电脑。'}
        return {'state': 'confirmed', 'reply': '权限已保存并回读确认，仅影响此对话后续轮次，当前任务不变。'}

    def compact(self, thread_id: str, connection: str | None = None) -> dict:
        before = self.control_state(thread_id)
        if (before['turn'].get('status') == 'inProgress' or before.get('pending')
                or (before.get('runtime') or {}).get('type') == 'active' or before['requests']):
            return {'state': 'rejected', 'reply': '对话正在运行或等待处理，未压缩。请等本轮结束后再试。'}
        result = self.call(thread_id, 'thread-follower-compact-thread', {}, 1,
                           expected_owner=before['owner_id'], expected_connection=connection)
        accepted = result['state'] == 'confirmed' and result['result'].get('ok') is True
        return {'state': 'confirmed' if accepted else result['state'] if result['state'] != 'confirmed' else 'unknown',
                'reply': '原生窗口已受理压缩，不等于压缩完成。引用本条发送“压缩状态”查询；压缩会调用模型并可能消耗额度。' if accepted else '压缩未取得有效确认，可能已开始；不会自动重试。请查看电脑。',
                'baseline': before['compactions'], 'owner_id': before['owner_id']}

    def pause(self, thread_id: str, connection: str | None = None) -> dict[str, Any]:
        # Preserve the already verified native user-stop behavior.
        ok, text = pause_desktop(thread_id, expected_connection=connection, pipe_factory=self.pipe_factory)
        return {"state": "confirmed" if ok else "unknown" if "可能" in text or "确认" in text else "rejected", "reply": text}
