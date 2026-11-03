"""按实际接口能力连接现有 Desktop 对话的窗口所有者。"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from desktop_control import (DesktopClient, DesktopControlError, QUEUED_FOLLOWUPS_METHOD,
                             WindowsDesktopPipe, pause_desktop, pipe_server_identity,
                             public_error_code)


class NativeBackend:
    # Desktop briefly publishes ``needs_resume`` while an opened conversation
    # is hydrating.  Treating the first such snapshot as terminal made a QQ
    # message fail even though the same conversation became ready moments
    # later.  The retry is deliberately short and only applies to this known
    # transient state; pending submissions and every other ambiguous state
    # remain fail-closed.
    # The Desktop UI can take several seconds to finish MCP/session
    # hydration after a conversation becomes visible; keep the total wait
    # bounded at roughly eight seconds while covering that normal window.
    _SUBMISSION_RESUME_RETRY_DELAYS = (0.25, 0.5, 1.0, 2.0, 4.0)

    def __init__(self, *, pipe_factory: Any = WindowsDesktopPipe, contract_check: Any = None,
                 app_tools: Any = None, catalog: Any = None) -> None:
        self.pipe_factory = pipe_factory
        self.contract_check = contract_check
        self.process_image: str | None = None
        self.app_tools = app_tools
        self.catalog = catalog

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
                settings = self.read_settings(result['thread_id'], expected_connection=connection)
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

    @staticmethod
    def _check_connection(pipe: Any, expected_connection: str | None) -> None:
        """Reject reads from a Desktop process generation different from the card."""
        if expected_connection is not None and hasattr(pipe, 'handle'):
            if pipe_server_identity(pipe)[0] != expected_connection:
                raise DesktopControlError('desktop-generation-changed-during-read')

    def snapshot(self, thread_id: str, expected_connection: str | None = None) -> dict[str, Any]:
        try:
            with self.pipe_factory() as pipe:
                if hasattr(pipe, "deadline"):
                    pipe.deadline = min(pipe.deadline, time.monotonic() + 3.0)
                client = DesktopClient(pipe)
                client.initialize()
                self._check_connection(pipe, expected_connection)
                owner = client.owner(thread_id)
                client.following(thread_id, True, owner)
                try:
                    # A follower's owner discovery causes the owner to publish a snapshot.
                    client.owner(thread_id)
                    client.request("thread-follower-read-model-settings", {"conversationId": thread_id}, version=1, target_client_id=owner)
                    if client.oversized_frame:
                        raise DesktopControlError("frame-too-large")
                    while True:
                        event = client.broadcasts.get("thread-stream-state-changed", {})
                        params = event.get("params", {})
                        change = params.get("change", {})
                        state = change.get("conversationState")
                        if (params.get("conversationId") == thread_id and params.get('hostId') == 'local'
                                and event.get('sourceClientId') == owner and event.get('version') == 11
                                and change.get('type') == 'snapshot' and isinstance(state, dict)
                                and state.get('id') == thread_id):
                            self._check_connection(pipe, expected_connection)
                            return {**self._view(state), 'owner_id': owner, 'snapshot_complete': True}
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
        except DesktopControlError as error:
            if str(error) != "frame-too-large":
                raise
            return self._bounded_snapshot(thread_id, expected_connection)

    def _bounded_snapshot(self, thread_id: str, expected_connection: str | None = None) -> dict[str, Any]:
        """Read only bounded native data when the full follower snapshot is huge.

        Desktop's follower broadcast contains the entire transcript-backed state
        and can exceed the pipe frame cap. The app-tool read is limited to one
        turn and no outputs. It can authorize only a normal new turn when the
        native thread status and latest turn agree that the conversation is
        idle; request, queue, permission, and history controls still require a
        complete follower snapshot.
        """
        identity, _ = self.probe()
        if expected_connection is not None and identity != expected_connection:
            raise DesktopControlError('desktop-generation-changed-during-read')
        with self.pipe_factory() as pipe:
            client = DesktopClient(pipe)
            client.initialize()
            self._check_connection(pipe, expected_connection)
            owner = client.owner(thread_id)
            self._check_connection(pipe, expected_connection)
        result = self.native_tools().call(
            'read_thread',
            {'threadId': thread_id, 'turnLimit': 1, 'includeOutputs': False,
             'maxOutputCharsPerItem': 0},
            source_thread_id=thread_id, expected_identity=identity,
            operation_id=uuid.uuid4().hex,
        )
        data = result.get('data') if isinstance(result, dict) else None
        row = data.get('thread') if isinstance(data, dict) else None
        turns = data.get('turns') if isinstance(data, dict) else None
        if result.get('state') != 'confirmed' or not isinstance(row, dict):
            raise DesktopControlError('bounded-snapshot-read-failed')
        if (row.get('id') != thread_id or row.get('hostId') != 'local'
                or row.get('kind') != 'codex'):
            raise DesktopControlError('bounded-snapshot-thread-identity-mismatch')
        if not isinstance(turns, list) or len(turns) > 1:
            raise DesktopControlError('bounded-snapshot-turn-page-invalid')
        latest = turns[0] if turns else {}
        status = latest.get('status') if isinstance(latest, dict) else None
        if status not in ('inProgress', 'completed', 'failed', 'interrupted'):
            raise DesktopControlError('bounded-snapshot-turn-status-unavailable')
        native_status = row.get('status')
        status_type = native_status.get('type') if isinstance(native_status, dict) else None
        active_flags = native_status.get('activeFlags', []) if isinstance(native_status, dict) else None
        if (status_type not in ('active', 'idle') or not isinstance(active_flags, list)
                or any(not isinstance(flag, str) for flag in active_flags) or active_flags):
            raise DesktopControlError('bounded-snapshot-thread-status-unavailable')
        if ((status_type == 'idle' and status not in ('completed', 'failed', 'interrupted'))
                or (status_type == 'active' and status != 'inProgress')):
            raise DesktopControlError('bounded-snapshot-status-conflict')
        cwd = row.get('cwd')
        if not isinstance(cwd, str) or not cwd:
            raise DesktopControlError('bounded-snapshot-working-directory-unavailable')
        settings: dict[str, Any] = {}
        try:
            settings = self.read_settings(thread_id, expected_connection=identity)
        except (DesktopControlError, OSError, TimeoutError, ValueError):
            pass
        raw = settings.get('raw') if isinstance(settings.get('raw'), dict) else {}
        resume_state = raw.get('resumeState') if isinstance(raw.get('resumeState'), str) else 'unknown'
        idle_safe = status_type == 'idle' and resume_state == 'resumed'
        runtime = {'type': status_type}
        items = latest.get('items')
        recent_turn = {'turnId': latest.get('id'), 'status': status,
                       'items': items if isinstance(items, list) else []}
        from async_questions import pending_batches
        try:
            if not isinstance(items, list):
                raise ValueError('recent-turn-items-unavailable')
            async_batches, async_unverified = pending_batches(recent_turn), False
        except (ValueError, UnicodeError, TypeError):
            async_batches, async_unverified = [], True
        compactions = self._compactions([{
            'turnId': latest.get('id'), 'status': status, 'items': items if isinstance(items, list) else [],
        }])
        return {
            'id': thread_id, 'model': settings.get('model'), 'effort': settings.get('effort'),
            'settings': {}, 'permissions': {}, 'runtime': runtime,
            'resumeState': resume_state,
            'turn': {'id': latest.get('id'), 'turnId': latest.get('id'), 'status': status,
                     'state': status, 'clientUserMessageId': None},
            'requests': [], 'requests_known': False, 'compactions': compactions,
            'pending': False, 'cwd': cwd,
            'collaboration_mode': None, 'goal_known': False, 'goal': None,
            'permissions_known': False, 'workspace_roots': [cwd],
            'async_questions': async_batches,
            'async_questions_unverified': async_unverified, 'accepted_submissions': [],
            'mode': raw.get('mode'), 'thread_start_kind': None,
            'owner_id': owner, 'snapshot_complete': False,
            'bounded_idle_safe': idle_safe,
            'bounded_snapshot': {
                'reason': 'full-follower-frame-exceeded-8-mib',
                'thread_status': status_type, 'turn_status': status,
                'resume_state': resume_state,
                'unavailable_fields': ['native_requests', 'current_permissions', 'thread_goal', 'full_history'],
            },
        }

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
            if not isinstance(value, dict) or not isinstance(value.get('threads', {}), dict):
                return "后台状态不可用：control-status.json 格式无效；Codex Desktop 在线状态未由本地监控回报。"
            pid = read_monitor_pid(root, require_ready=True)
            if pid and value.get("monitor_pid") == pid and value.get("monitor_created") == _query_process_identity(pid).get("process_start_time"):
                row = value.get("threads", {}).get(thread_id)
                state = row.get('state') if isinstance(row, dict) else None
                label = {"running": "运行中", "waiting_approval": "等待审批", "waiting_input": "等待回答", "completed": "空闲（已完成）", "result_unknown": "已收到结束事件，但任务记录不完整，不能判定成功或失败", "failed": "出错", "interrupted": "已暂停", "disconnected": "监控与任务断开，未收到最终结果", "crashed": "异常停止", "idle": "空闲"}.get(state)
                if label:
                    return label
        except (OSError, ValueError, TypeError):
            pass
        try:
            self.ensure_owner(thread_id)
        except (OSError, TimeoutError, DesktopControlError, ValueError) as error:
            return f"Desktop 未确认该对话的窗口 owner（{public_error_code(error)}）；本地没有可显示的运行状态。"
        return (f"Desktop 已确认 thread={thread_id} 已载入，但本地 control-status.json 没有该对话的 monitor 状态记录；"
                "因此无法显示当前轮次是运行中还是空闲。请在托盘助手运行 doctor 检查监控状态，不要据此推断任务已结束。")

    def read_settings(self, thread_id: str, expected_connection: str | None = None) -> dict[str, Any]:
        with self.pipe_factory() as pipe:
            client = DesktopClient(pipe)
            client.initialize()
            self._check_connection(pipe, expected_connection)
            owner = client.owner(thread_id)
            response = client.request("thread-follower-read-model-settings", {"conversationId": thread_id}, version=1, target_client_id=owner)
            if response.get("handledByClientId") != owner:
                raise DesktopControlError("owner-mismatch")
            self._check_connection(pipe, expected_connection)
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
        before = self.read_settings(thread_id, expected_connection=connection)
        if connection is not None:
            try:
                if self.probe()[0] != connection:
                    return {"state": "rejected", "reply": "Codex 连接已变化，未修改设置。请重新查看设置。"}
            except (DesktopControlError, OSError, TimeoutError) as error:
                return {"state": "rejected", "reply": f"未修改设置：连接身份复查失败（{public_error_code(error)}）；没有发送设置更新。恢复 Desktop 连接后重新查看模型选项。"}
        if any(before.get(k) != expected.get(k) for k in ("model", "effort")):
            return {"state": "rejected", "reply": "对话设置已发生变化。这次未修改，请重新查看选项。"}
        if all(before.get(k) == v for k, v in changes.items()):
            return {"state": "confirmed", "reply": "已经是所选设置，没有重复写入。", "settings": before}
        result = self.call(thread_id, "thread-follower-update-thread-settings", {
            "threadSettings": changes,
            "condition": {"ifModelEquals": before.get("model"), "ifEffortEquals": before.get("effort")},
        }, 2, expected_connection=connection)
        if result["state"] != "confirmed":
            if result['state'] == 'rejected':
                return {"state": "rejected", "reply": f"thread-settings 更新在发送前被拒绝（{public_error_code(result.get('error', 'no-error-code'))}）；没有改动模型或思考档位。"}
            return {"state": "unknown", "reply": f"已调用 thread-settings 更新，但没有有效回执（{public_error_code(result.get('error', 'no-response'))}）；模型/档位可能已改变。不会重试，请引用目标对话发送‘设置’核对实际值。"}
        if result["result"].get("applied") is not True:
            return {"state": "rejected", "reply": "窗口没有接受这次设置修改；请重新查看设置。"}
        try:
            after = self.read_settings(thread_id, expected_connection=connection)
        except (DesktopControlError, TimeoutError, OSError, ValueError) as error:
            return {"state": "unknown", "reply": f"Desktop 已确认受理模型/档位更新，但 read-model-settings 回读失败（{public_error_code(error)}）。不会重复修改；请用‘设置’核对实际值。"}
        if not all(after.get(k) == v for k, v in changes.items()):
            actual = ', '.join(f'{key}={after.get(key, "缺失")}' for key in changes)
            requested = ', '.join(f'{key}={value}' for key, value in changes.items())
            return {"state": "unknown", "reply": f"Desktop 已受理更新但回读值不匹配：请求 {requested}，实际 {actual}。不会自动重写或降档；请重新查看设置。"}
        return {"state": "confirmed", "reply": "设置已保存并回读确认。只影响此对话后续轮次，不改变正在执行的本轮。", "settings": after}

    @staticmethod
    def _compactions(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{"id": item.get('id'), "turn_id": turn.get('turnId'),
                 "status": item.get('status'), "source": item.get('source'),
                 "turn_status": turn.get('status')}
                for turn in turns for item in (turn.get('items') if isinstance(turn.get('items'), list) else [])
                if isinstance(item, dict) and item.get('type') == 'contextCompaction'][-8:]

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
        from control_actions import missing_permission_fields
        current_permissions = state.get('currentPermissions')
        permissions_known = not missing_permission_fields({
            'permissions': current_permissions if isinstance(current_permissions, dict) else {},
            'settings': settings,
        })
        compactions = NativeBackend._compactions(turns[-8:])
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
            "requests_known": isinstance(state.get("requests"), list),
            'compactions': compactions, 'pending': bool(state.get('unconfirmedTurnSubmissions')),
            'cwd': state.get('cwd'),
            'collaboration_mode': state.get('latestCollaborationMode'),
            'goal_known': 'threadGoal' in state,
            'goal': state.get('threadGoal'),
            'permissions_known': permissions_known,
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
        if state.get('snapshot_complete') is not False and state.get('requests_known') is False:
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
                                 queue_func: Any) -> dict:
        """Append to Codex's native visible queue without replacing its snapshot."""
        try:
            accepted, detail = queue_func(thread_id, text)
        except TimeoutError:
            return {'state': 'unknown', 'reply':
                    'Codex 队列命令超时，消息可能已经排队；结果未确认，助手不会重发。'
                    '请先检查目标对话的可见待发送列表和最近消息。'}
        except OSError as error:
            return {'state': 'rejected', 'reply':
                    f'无法启动 Codex 队列命令（{type(error).__name__}），消息未排队；恢复后请重新发送。'}

        if accepted is None:
            return {'state': 'unknown', 'reply':
                    f'{detail or "Codex 队列命令超时"}；消息可能已经排队，结果未确认。助手不会重发。'
                    '请先检查目标对话的可见待发送列表和最近消息。'}
        if not accepted:
            return {'state': 'rejected', 'reply':
                    f'Codex 没有接受这条排队消息（{detail or "命令未确认"}）；消息未排队。'}

        from submission_receipts import queue_ack
        queue_id = queue_ack(detail, thread_id)
        if not queue_id:
            return {'state': 'unknown', 'reply':
                    'Codex 队列命令返回成功，但没有唯一且匹配的队列编号；消息可能已经排队，结果未确认。'
                    '助手不会重发。请先检查目标对话的可见待发送列表和最近消息。'}
        return {'state': 'confirmed', 'mode': 'queued', 'queue_id': queue_id,
                'detail': detail}

    def _submission_state(self, thread_id: str, connection: str) -> dict:
        """Read a submission boundary, waiting only for transient hydration.

        ``needs_resume`` is emitted by Desktop while it is restoring a
        conversation.  It is not safe to dispatch against that snapshot, but
        it is also not a reason to make the user resend a message immediately.
        Re-read until Desktop publishes a different state or the bounded
        retry window expires.  A pending/unconfirmed submission stops the
        retry immediately so an unknown prior mutation can never be bypassed.
        """
        state = self.control_state(thread_id, connection=connection)
        if state.get('pending') or state.get('resumeState') != 'needs_resume':
            return state
        for delay in self._SUBMISSION_RESUME_RETRY_DELAYS:
            time.sleep(delay)
            state = self.control_state(thread_id, connection=connection)
            if state.get('pending') or state.get('resumeState') != 'needs_resume':
                return state
        return state

    def preflight_guide_saved_queue_item(self, thread_id: str, entry: dict, catalog: Any,
                                         connection: str | None = None) -> dict:
        """Check the retryable busy/initializing cases before claiming an operation."""
        try:
            self.require_extended_contract()
            from control_actions import normalize_saved_queue
            rows = catalog.saved_queue_rows(thread_id)
            matches = [row for row in rows if isinstance(row, dict) and row.get('id') == entry.get('id')]
            current = normalize_saved_queue(matches)
            if (len(matches) != 1 or len(current) != 1 or current[0]['digest'] != entry.get('digest')
                    or current[0]['status'] != 'queued'):
                return {'state': 'rejected', 'retryable': True,
                        'reply': '这条排队消息已变化、开始或不再存在，未删除、未引导；请重新查看队列。'}
        except (OSError, TimeoutError, ValueError, TypeError, KeyError, DesktopControlError, sqlite3.Error):
            return {'state': 'rejected', 'retryable': True,
                    'reply': '未删除、未引导：检查本地队列行、正文摘要或 Desktop 当前轮次失败（本次没有调用原生 remove/steer）。请恢复连接并重新查看队列。'}
        try:
            before = self.snapshot(thread_id, expected_connection=connection)
        except (OSError, TimeoutError, ValueError, TypeError, KeyError, DesktopControlError):
            return {'state': 'rejected', 'retryable': True,
                    'reply': '未删除、未引导：Desktop snapshot/read_thread 没有返回可验证的活动轮次；本次没有调用原生 remove/steer。恢复连接后重新查看任务状态。'}
        turn = before.get('turn') or {}
        if turn.get('status') != 'inProgress':
            return {'state': 'rejected', 'retryable': True,
                    'reply': '当前没有正在运行的任务，未删除排队消息；请在任务运行时再引导。'}
        if not turn.get('turnId') or not isinstance(before.get('cwd'), str) or not before['cwd']:
            return {'state': 'rejected', 'retryable': True,
                    'reply': '当前任务仍在初始化，未删除排队消息；稍后再试。'}
        return {'state': 'confirmed', 'reply': '当前任务可引导。'}

    def submit_message(self, thread_id: str, text: str, connection: str,
                       operation_id: str, queue_func: Any) -> dict:
        """Start idle work or append busy work through Codex's native queue.

        A lost native confirmation never becomes a second submission via another
        path. Busy messages use the exact-thread CLI queue append, never the
        Desktop whole-snapshot replacement endpoint.
        """
        try:
            text.encode('utf-8', errors='strict')
            if (not 1 <= len(text.strip()) <= 4000 or not re.fullmatch(r'[A-Za-z0-9_-]{20,100}', thread_id)
                    or not re.fullmatch(r'[A-Za-z0-9_-]{20,100}', operation_id)):
                raise ValueError('invalid-message')
        except (ValueError, UnicodeError, TypeError):
            return {'state': 'rejected', 'reply': '消息或编号格式无效，未发送；请重新输入原文。'}
        before = self._submission_state(thread_id, connection)
        if (before.get('snapshot_complete') is False
                and not before.get('bounded_idle_safe')
                and (before.get('turn') or {}).get('status') != 'inProgress'):
            return {'state': 'rejected',
                    'reply': self._bounded_start_rejection(before)}
        identity, _ = self.probe()
        if identity != connection:
            return {'state': 'rejected', 'reply': 'Codex 连接已变化，这条消息未发送；恢复后请重新发。'}
        route = self._message_route(before)
        if route == 'started':
            if self.contract_check is None:
                from control_contract import check_queue_contract
                check_queue_contract(self.process_image or '')
            # Re-read immediately before an idle start. A newly busy owner gets
            # a rejection; this is a pre-dispatch check, never a retry.
            current = self.snapshot(thread_id, expected_connection=connection)
            if current['owner_id'] != before['owner_id']:
                return {'state': 'rejected', 'reply': '对话所有者已变化，未发送。请恢复连接后重新发。'}
            if (current.get('snapshot_complete') is False
                    and not current.get('bounded_idle_safe')):
                return {'state': 'rejected',
                        'reply': self._bounded_start_rejection(current, preflight=True)}
            before, route = current, self._message_route(current)
        if route == 'stopping':
            return {'state': 'rejected', 'reply': '原生暂停仍在收尾，这条新消息未发送、未排队；待电脑显示停止后重新发。'}
        if route == 'unavailable':
            if before.get('snapshot_complete') is not False and before.get('requests_known') is False:
                return {'state': 'rejected', 'reply': '未发送、未排队：Desktop 完整状态缺少 requests 列表，不能确认是否正等待问题或审批。没有调用 start-turn；请在目标对话处理待办并重新读取状态。'}
            if before.get('pending'):
                return {'state': 'rejected', 'reply': '这条新消息没有发送或排队：本机记录到上一条提交尚无 Desktop 回执。请先在目标对话检查上一条是否出现；助手不会自动补发。'}
            if before.get('resumeState') != 'resumed':
                resume = before.get('resumeState') or '字段缺失'
                return {'state': 'rejected', 'reply': f'Desktop resumeState={resume}；这条新消息未发送、未排队。等目标对话显示就绪后重新发送。'}
            runtime = (before.get('runtime') or {}).get('type') or '缺失'
            turn_status = (before.get('turn') or {}).get('status') or '缺失'
            if before.get('snapshot_complete') is False:
                return {'state': 'rejected', 'reply': f'这条消息未发送、未排队：长对话摘要返回 thread={runtime}、turn={turn_status}，且没有完整待办/提交状态；当前不足以选择安全的发送入口。请在 Desktop 查看目标对话状态后重新发送。'}
            return {'state': 'rejected', 'reply': f'这条消息未发送、未排队：Desktop 没有提供可用的发送状态（thread={runtime}、turn={turn_status}，resumeState=resumed）。请在 Desktop 确认该对话已载入且无待处理问题后重新发送。'}
        if route == 'queued':
            return self.enqueue_native_follow_up(thread_id, text, connection, queue_func)
        if (before.get('mode') not in (None, 'default')
                or before.get('thread_start_kind') not in (None, 'default')
                or not isinstance(before.get('cwd'), str) or not before['cwd']):
            return {'state': 'rejected', 'reply': f'未发送、未排队：Desktop 返回 mode={before.get("mode")!r}、threadStartKind={before.get("thread_start_kind")!r} 或缺少有效工作目录；助手没有确认这类对话支持普通新一轮发送。请在 Desktop 打开目标对话检查任务模式。'}
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
        if result['state'] == 'rejected':
            return {'state': 'rejected', 'reply': f'新一轮未开始：start-turn 发送前预检被拒绝（{public_error_code(result.get("error", "no-error-code"))}）。没有改走队列或引导。'}
        error = public_error_code(result.get('error', 'missing-turn-id'))
        return {'state': 'unknown', 'reply': f'已调用 start-turn，但 Desktop 未回传可核对的 turn ID（{error}）；任务可能已开始。不会重复提交或改走其他入口。请在目标对话检查最新一轮。'}

    @staticmethod
    def _bounded_start_rejection(state: dict, *, preflight: bool = False) -> str:
        bounded = state.get('bounded_snapshot') or {}
        thread_status = bounded.get('thread_status') or (state.get('runtime') or {}).get('type') or '未知'
        turn_status = bounded.get('turn_status') or (state.get('turn') or {}).get('status') or '未知'
        resume_state = bounded.get('resume_state') or state.get('resumeState') or '未知'
        phase = '提交前复查' if preflight else '首次状态读取'
        return (f'{phase}未发送：Desktop 完整状态帧超过 8 MiB；最近轮次摘要为 '
                f'thread={thread_status}、turn={turn_status}、resume={resume_state}，'
                '未同时确认空闲、终态和已恢复。未发送、未排队；没有切换目标。')

    def steer(self, thread_id: str, text: str, connection: str, operation_id: str) -> dict:
        """Desktop's execution-time active-turn semantics, explicitly accepted by the user."""
        try:
            text.encode('utf-8', errors='strict')
            if not 1 <= len(text.strip()) <= 4000:
                raise ValueError('invalid-steer-text')
            before = self.control_state(thread_id, connection=connection)
            current_identity, _ = self.probe()
            if current_identity != connection:
                raise DesktopControlError('desktop-generation-changed')
            if self.contract_check is None:
                from control_contract import check_steer_contract
                check_steer_contract(self.process_image or '')
        except (DesktopControlError, OSError, TimeoutError, ValueError, UnicodeError) as error:
            detail = str(error)
            if detail == 'desktop-generation-changed':
                reply = '引导未发送：Codex Desktop 进程在读取期间已重启，旧窗口身份已失效。请引用原对话重新查看后再发。'
            elif detail in ('bounded-snapshot-read-failed', 'bounded-snapshot-thread-identity-mismatch',
                            'bounded-snapshot-turn-page-invalid', 'bounded-snapshot-turn-status-unavailable',
                            'bounded-snapshot-thread-status-unavailable', 'bounded-snapshot-status-conflict',
                            'bounded-snapshot-working-directory-unavailable'):
                reasons = {
                    'bounded-snapshot-read-failed': 'read_thread 没有返回成功结果',
                    'bounded-snapshot-thread-identity-mismatch': 'read_thread 返回的 thread ID、host 或 kind 与引用目标不一致',
                    'bounded-snapshot-turn-page-invalid': '最近轮次分页结构不符合原生接口声明',
                    'bounded-snapshot-turn-status-unavailable': '最近轮次缺少受支持的状态字段',
                    'bounded-snapshot-thread-status-unavailable': 'Desktop 没有返回可用的 idle/active 状态，或 activeFlags 非空',
                    'bounded-snapshot-status-conflict': 'thread 状态与最近轮次状态互相矛盾',
                    'bounded-snapshot-working-directory-unavailable': '摘要没有返回活动轮次的工作目录',
                }
                reply = f'引导未发送：完整状态超过 8 MiB，最近轮次摘要{reasons[detail]}；没有调用原生 steer-turn，也没有排队。请在 Desktop 打开目标对话并查看当前任务。'
            elif isinstance(error, (OSError, TimeoutError)):
                reply = '引导未发送：连接 Desktop 原生窗口时未收到状态响应；没有调用原生引导接口。检查 Desktop/助手连接后重试。'
            elif detail == 'invalid-steer-text':
                reply = '引导未执行：正文为空或超过 4,000 字，未调用原生引导接口。'
            else:
                reply = f'引导未发送：Desktop 原生接口或窗口身份检查失败（{public_error_code(error)}）；没有调用原生引导接口。请按错误码检查助手适配与窗口连接。'
            return {'state': 'rejected', 'reply': reply}
        if before['turn'].get('status') != 'inProgress':
            return {'state': 'rejected', 'reply': '此对话当前没有正在运行的任务，未引导，也没有排入新一轮。'}
        if not before['turn'].get('turnId') or not isinstance(before.get('cwd'), str) or not before['cwd']:
            missing = 'turn ID' if not before['turn'].get('turnId') else '工作目录'
            return {'state': 'rejected', 'reply': f'引导未发送：Desktop 的活动轮次缺少{missing}，原生 steer-turn 所需上下文不完整；没有调用引导接口，也没有排队。等任务状态刷新后重新查看。'}
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
            return {'state': 'rejected', 'reply': f'steer-turn 发送前预检被拒绝（{public_error_code(result.get("error", "no-error-code"))}）；没有改成普通消息或排队。'}
        error = public_error_code(result.get('error', 'missing-steer-receipt'))
        return {'state': result['state'] if result['state'] != 'confirmed' else 'unknown',
                'reply': f'Desktop 未返回 steer-turn 接受回执（{error}）；引导可能已送达当前任务。不会自动重试，也不会改成下一轮消息。请检查目标对话的最新活动。'}

    def fork_boundary(self, thread_id: str, connection: str) -> dict:
        from control_actions import signature
        state = self.control_state(thread_id, connection=connection)
        if state.get('snapshot_complete') is False:
            raise DesktopControlError('fork-goal-and-request-metadata-unavailable')
        if not state.get('goal_known'):
            raise DesktopControlError('fork-goal-metadata-unavailable')
        if state.get('goal') is not None:
            raise DesktopControlError('fork-source-has-active-goal')
        if state.get('requests_known') is False:
            raise DesktopControlError('fork-request-metadata-unavailable')
        if state.get('pending'):
            raise DesktopControlError('fork-source-has-unconfirmed-submission')
        if state.get('requests'):
            raise DesktopControlError('fork-source-has-pending-request')
        if (state['turn'].get('status') != 'completed'
                or (state.get('runtime') or {}).get('type') == 'active'):
            raise DesktopControlError('fork-source-not-idle-at-completed-turn')
        boundary = self.native_tools().boundary(thread_id, connection)
        if boundary['turn_id'] != state['turn'].get('turnId'):
            raise DesktopControlError('fork-latest-turn-changed-during-read')
        baseline = {'boundary': boundary, 'owner_id': state['owner_id'],
                    'settings_digest': signature({k: state.get(k) for k in
                        ('model', 'effort', 'settings', 'permissions', 'collaboration_mode', 'cwd')})}
        return baseline

    def fork_latest(self, thread_id: str, baseline: dict, connection: str, operation_id: str) -> dict:
        try:
            current = self.fork_boundary(thread_id, connection)
            if current != baseline:
                changed = []
                if current.get('owner_id') != baseline.get('owner_id'):
                    changed.append('Desktop 窗口所有者')
                old_edge, new_edge = baseline.get('boundary', {}), current.get('boundary', {})
                if old_edge.get('turn_id') != new_edge.get('turn_id'):
                    changed.append('最近完成轮次 ID')
                if old_edge.get('updated_at') != new_edge.get('updated_at'):
                    changed.append('对话更新时间')
                if old_edge.get('title') != new_edge.get('title'):
                    changed.append('对话标题')
                if current.get('settings_digest') != baseline.get('settings_digest'):
                    changed.append('模型/权限/工作目录设置')
                detail = '、'.join(changed) if changed else '最近完成轮次元数据'
                return {'state': 'rejected', 'reply': f'未创建分支：确认卡生成后，{detail}发生变化。没有调用 fork；请重新查看分支范围。'}
        except DesktopControlError as error:
            replies = {
                'fork-goal-and-request-metadata-unavailable': '未创建分支：长对话摘要不含 threadGoal 和原生待处理请求列表，无法证明源对话满足分支前提。没有创建新对话。',
                'fork-goal-metadata-unavailable': '未创建分支：Desktop 没有提供 threadGoal 字段，无法确认源对话没有自动目标。',
                'fork-request-metadata-unavailable': '未创建分支：Desktop 没有提供原生 requests 列表，无法确认源对话没有待处理审批。没有调用 fork。',
                'fork-source-has-active-goal': '未创建分支：源对话仍有关联的自动目标。',
                'fork-source-has-unconfirmed-submission': '未创建分支：源对话存在尚未确认结果的提交。',
                'fork-source-has-pending-request': '未创建分支：源对话存在待处理的问题或审批。',
                'fork-source-not-idle-at-completed-turn': '未创建分支：源对话不是空闲且最近一轮已完成。',
                'fork-latest-turn-changed-during-read': '未创建分支：读取期间最近完成轮次发生变化。请重新查看范围。',
                'native-fork-boundary-unavailable': '未创建分支：read_thread 没有返回恰好一条、属于本地目标对话的最新已完成轮次。没有创建新对话，请在 Desktop 打开该对话后重新查看范围。',
                'fork-range-or-runtime-unconfirmed': '未创建分支：创建后回读没有核对源/新对话具有相同最近完成轮次，或新分支仍有活动任务/请求。分支可能已创建，不要重复操作；请按返回的分支编号检查。',
            }
            return {'state': 'rejected', 'reply': replies.get(str(error),
                    f'未创建分支：Desktop 未返回可验证的“本地目标对话最新已完成轮次”（{public_error_code(error)}）。没有创建新对话；请在 Desktop 打开目标后重新查看范围。')}
        except (OSError, TimeoutError, ValueError) as error:
            return {'state': 'rejected', 'reply': f'未创建分支：读取源对话边界时连接失败（{type(error).__name__}）；没有创建新对话。'}
        result = self.native_tools().fork_latest(thread_id, connection, operation_id)
        ident = result.get('thread_id')
        if result['state'] != 'confirmed' or not ident:
            return result
        try:
            source_after = self.fork_boundary(thread_id, connection)
            child_boundary = self.native_tools().boundary(ident, connection)
            child = self.control_state(ident, connection=connection)
            if (source_after != baseline or child_boundary['turn_id'] != baseline['boundary']['turn_id']
                    or not child.get('goal_known') or child.get('goal') is not None
                    or child['turn'].get('turnId') != child_boundary['turn_id']
                    or child['turn'].get('status') != 'completed' or child.get('pending') or child.get('requests')
                    or (child.get('runtime') or {}).get('type') == 'active'):
                raise DesktopControlError('fork-range-or-runtime-unconfirmed')
        except (DesktopControlError, OSError, TimeoutError, ValueError) as error:
            return {'state': 'unknown', 'thread_id': ident,
                    'reply': (f'分支已创建，编号为 {ident}；回读未能核对源/新对话最近轮次是否一致（{type(error).__name__}）。'
                              '没有发送首条任务；不要重复分支，请在对话列表中检查该编号。')}
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

    def control_state(self, thread_id: str, connection: str | None = None) -> dict:
        self.require_extended_contract()
        return self.snapshot(thread_id, expected_connection=connection)

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
        return observed_status(receipt, catalog.queue_path,
                                lambda thread_id: self.snapshot(thread_id, expected_connection=connection))

    def remove_submission(self, receipt: dict, catalog: Any, connection: str) -> dict:
        """Native single-item removal, never a database deletion or delete-then-steer."""
        from submission_receipts import read_queued_item, digest
        status = self.submission_status(receipt, catalog, connection)
        if status['status'] == 'entered':
            return {'state': 'rejected', 'reply': f'未删除：Desktop 已把这条消息关联到轮次 {status.get("turn_id") or "（无轮次编号）"}，它已离开排队阶段。'}
        if status['status'] == 'queued' and not status.get('payload_verified'):
            return {'state': 'rejected', 'reply': '未删除：队列中存在相同编号，但正文摘要与发送回执不一致；不会删除无法证明是原文的队列项。请在 Desktop 检查该条目。'}
        if status['status'] != 'queued':
            reason = {'desktop-history-read-failed': 'Desktop 历史读取失败，未能核对该消息是否已开始',
                      'desktop-history-summary-incomplete': '长对话摘要不含该消息身份，未能核对它是否已开始',
                      'absent-from-queue-and-no-accepted-turn': '该编号既不在可读队列，也未匹配到已接受的 turn ID'}.get(
                          status.get('reason'), '队列中没有匹配的已验证条目')
            return {'state': 'rejected', 'reply': f'未删除：{reason}。队列状态可能已变化；没有发送 remove 请求。请在 Desktop 查看原消息，确认仍排队后再删除。'}
        try:
            queued = read_queued_item(catalog.queue_path, receipt['thread_id'], receipt['queue_id'],
                                      text_digest=receipt['text_digest'])
            if queued is None or not queued['payload_verified']:
                raise ValueError('queue-item-no-longer-verified')
            before = self.snapshot(receipt['thread_id'], expected_connection=connection)
        except (OSError, TimeoutError, ValueError, DesktopControlError, sqlite3.Error) as error:
            return {'state': 'rejected', 'reply': f'未删除：读取本机队列正文或 Desktop 当前轮次失败（{public_error_code(error)}）；没有发送 remove 请求。恢复连接后重新查看队列和目标对话。'}
        result = self.call(receipt['thread_id'], 'thread-follower-remove-queued-message',
                           {'messageId': receipt['queue_id']}, 1, expected_owner=before['owner_id'],
                           expected_connection=connection)
        if result['state'] != 'confirmed':
            detail = public_error_code(result.get('error', 'no-valid-response'))
            if result['state'] == 'rejected':
                return {'state': 'rejected', 'reply': f'未发送 remove-queued-message：删除预检被拒绝（{detail}）。没有删除或引导该消息。'}
            return {'state': 'unknown', 'reply': f'已调用 remove-queued-message，但没有有效删除回执（{detail}）；队列项可能已删除。不会重试或转为引导，请重新查看该条队列记录。'}
        removed = result['result'].get('removed')
        if removed is None:
            return {'state': 'rejected', 'reply': '原生队列已没有这一条；它可能已经开始或被移除。没有删除其他消息。'}
        message = removed.get('message') if isinstance(removed, dict) else None
        if (not isinstance(message, dict) or message.get('id') != receipt['queue_id']
                or not isinstance(message.get('text'), str) or digest(message['text']) != receipt['text_digest']):
            return {'state': 'unknown', 'reply': 'Desktop 返回了 remove 结果，但其中的队列 ID 或正文摘要与原回执不一致；未确认这条消息是否删除成功。不会再次删除，请检查目标对话的可见队列。'}
        return {'state': 'confirmed', 'reply': '原生窗口已移除这一个排队条目，没有清空其他消息。若已被消费，删除不能撤回模型已经开始的工作。'}

    def remove_saved_queue_item(self, thread_id: str, entry: dict, catalog: Any,
                                connection: str | None = None) -> dict:
        try:
            self.require_extended_contract()
            current = next((x for x in catalog.saved_queue(thread_id) if x['id'] == entry['id']), None)
            before = self.snapshot(thread_id, expected_connection=connection)
        except (OSError, TimeoutError, ValueError, DesktopControlError, sqlite3.Error, KeyError, TypeError) as error:
            return {'state': 'rejected', 'retryable': True,
                    'reply': f'未删除：检查本地队列、接口声明或 Desktop 所有者失败（{type(error).__name__}）。没有发送 remove 请求；重新查看队列后再试。'}
        if current is None or current['digest'] != entry['digest'] or current['status'] != 'queued':
            return {'state': 'rejected', 'retryable': True,
                    'reply': '未删除：队列中没有找到正文摘要相同且状态仍为 queued 的该编号条目。请重新查看队列。'}
        result = self.call(thread_id, 'thread-follower-remove-queued-message', {'messageId': entry['id']}, 1,
                           expected_owner=before['owner_id'], expected_connection=connection)
        if result['state'] != 'confirmed':
            if result['state'] == 'rejected':
                return {'state': 'rejected', 'reply': f'remove-queued-message 发送前预检被拒绝（{public_error_code(result.get("error", "no-error-code"))}）；未删除。'}
            return {'state': 'unknown', 'reply': f'Desktop 未返回 remove-queued-message 的成功回执（{public_error_code(result.get("error", "no-valid-response"))}）；该条可能已删除，不会再次删除。请重新查看队列。'}
        removed = result['result'].get('removed')
        if removed is None:
            return {'state': 'rejected', 'reply': '原生队列中已没有这条消息，没有删除其他条目；它可能已经开始。'}
        from control_actions import signature
        message = removed.get('message') if isinstance(removed, dict) else None
        if not isinstance(message, dict) or message.get('id') != entry['id'] or signature(message) != entry['digest']:
            return {'state': 'unknown', 'reply': 'Desktop 返回了 remove 结果，但队列 ID 或记录摘要与所选条目不一致；无法确认是否在发送前删除。不会重试，请检查电脑队列。'}
        return {'state': 'confirmed', 'reply': '已移除这一条待发送消息，未清空其他消息。已经开始的模型任务不能靠删除队列撤回。'}

    def guide_saved_queue_item(self, thread_id: str, entry: dict, catalog: Any,
                               connection: str | None = None) -> dict:
        """Delete one verified visible queue row, then guide its exact text.

        These are two native requests, not a transaction. We require a live
        task before deleting and only guide after the native removal response
        identifies the exact queued row. An ambiguous/failed guide is never
        retried because the queue item has already been consumed.
        """
        try:
            self.require_extended_contract()
        except Exception as error:
            return {'state': 'rejected', 'retryable': True, 'reply': f'未删除、未引导：当前 Desktop 没有通过队列/steer 接口检查（{type(error).__name__}）。本次未调用原生操作；更新或恢复连接后重新查看队列。'}
        from control_actions import normalize_saved_queue, signature

        try:
            rows = catalog.saved_queue_rows(thread_id)
            matches = [row for row in rows if isinstance(row, dict) and row.get('id') == entry.get('id')]
            if len(matches) != 1:
                return {'state': 'rejected', 'retryable': True, 'reply': '这条排队消息已变化或不再唯一，未删除、未引导；请重新查看队列。'}
            original = matches[0]
            current = normalize_saved_queue([original])
            if (len(current) != 1 or current[0]['digest'] != entry.get('digest')
                    or current[0]['status'] != 'queued'):
                return {'state': 'rejected', 'retryable': True, 'reply': '这条排队消息已开始、变化或不再存在，未删除、未引导；请重新查看队列。'}
            text = original.get('text')
            if not isinstance(text, str):
                return {'state': 'rejected', 'retryable': True, 'reply': '排队正文无法安全读取，未删除、未引导。'}
            text.encode('utf-8', errors='strict')
            if not 1 <= len(text.strip()) <= 4000:
                return {'state': 'rejected', 'retryable': True, 'reply': '原排队正文为空或超过引导长度上限，未删除、未引导。'}

            before = self.snapshot(thread_id, expected_connection=connection)
        except (OSError, TimeoutError, ValueError, UnicodeError, DesktopControlError,
                sqlite3.Error, KeyError, TypeError) as error:
            return {'state': 'rejected', 'retryable': True, 'reply': f'未删除、未引导：读取该队列编号的正文/摘要或当前活动轮次失败（{type(error).__name__}）。本次没有调用 remove 或 steer；请重新查看队列和任务状态。'}

        turn = before.get('turn') or {}
        if turn.get('status') != 'inProgress':
            return {'state': 'rejected', 'retryable': True, 'reply': '当前没有正在运行的任务，未删除排队消息；请在任务运行时再引导。'}
        if not turn.get('turnId') or not isinstance(before.get('cwd'), str) or not before['cwd']:
            missing = 'turn ID' if not turn.get('turnId') else '工作目录'
            return {'state': 'rejected', 'retryable': True, 'reply': f'未删除排队消息：活动轮次缺少{missing}，无法构造原生 steer-turn 上下文。'}

        try:
            removed_result = self.call(
                thread_id, 'thread-follower-remove-queued-message', {'messageId': entry['id']}, 1,
                expected_owner=before['owner_id'], expected_connection=connection)
        except Exception as error:
            return {'state': 'unknown', 'reply': f'已调用 remove-queued-message，但助手未取得回执（{public_error_code(error)}）；没有继续调用 steer。队列项可能已删除或仍在队列中，不要重复操作，先检查电脑队列。'}
        if removed_result.get('state') != 'confirmed':
            state = removed_result.get('state') or 'unknown'
            return {'state': state, 'reply': f'Desktop 未确认删除该队列编号（状态={state}）；没有继续调用 steer。该项可能仍在队列或已被消费，先检查电脑队列再决定。'}
        removed = (removed_result.get('result') or {}).get('removed')
        message = removed.get('message') if isinstance(removed, dict) else None
        if (not isinstance(message, dict) or message.get('id') != entry['id']
                or message.get('text') != text or signature(message) != entry['digest']):
            return {'state': 'unknown', 'reply': 'Desktop 已受理 remove，但回执中的队列编号或正文摘要与所选条目不一致；没有继续调用 steer。请检查队列，避免丢失或重复发送正文。'}

        try:
            guided = self.steer(thread_id, text, connection or '', uuid.uuid4().hex)
        except Exception as error:
            return {'state': 'unknown', 'reply': f'排队消息已删除；随后 steer 调用发生本机异常（{type(error).__name__}），无法判断是否送达活动任务。不要重发原文；检查该对话最新一轮。'}
        if guided.get('state') == 'confirmed':
            return {'state': 'confirmed',
                    'reply': '已先从 Desktop 可见队列移除这条消息，再将相同正文引导给执行时正在运行的任务。'}
        if guided.get('state') == 'rejected':
            return {'state': 'rejected',
                    'reply': f'排队消息已删除；Desktop 拒绝了后续 steer（{guided.get("error", guided.get("reply", "无错误码"))}）。删除不能回滚，没有改为新一轮发送。'}
        return {'state': 'unknown',
                'reply': f'排队消息已删除；Desktop 对 steer-turn 返回状态={guided.get("state", "缺失")}，没有有效 turn ID 回执。引导可能已送达；不要重发，检查该对话最新一轮。'}

    def respond_to_request(self, data: dict, text: str) -> dict:
        self.require_extended_contract()
        from control_actions import request_payload, signature, REQUEST_METHODS
        state = self.snapshot(data['thread_id'], expected_connection=data.get('connection'))
        if state.get('snapshot_complete') is False:
            return {'state': 'rejected',
                    'reply': ('未提交答案：Desktop 完整状态帧超过 8 MiB；read_thread 只提供最近一轮摘要，'
                              '不含此请求的原生 request ID/参数，无法确认卡片仍对应同一请求。'
                              '没有发送答案；请在 Desktop 处理该请求。')}
        if state['owner_id'] != data['owner_id']:
            return {'state': 'rejected', 'reply': f'未提交答案：审批卡绑定的窗口所有者 {data["owner_id"]} 与当前窗口 {state.get("owner_id")} 不同。请在当前对话重新查看待办。'}
        matches = [r for r in state['requests'] if isinstance(r, dict) and r.get('id') == data['request_id'] and r.get('method') == data['method']]
        request = matches[0] if len(matches) == 1 else None
        if request is None or signature(request) != data['digest']:
            return {'state': 'rejected', 'reply': f'未提交答案：当前 requests 没有唯一匹配 request ID={data["request_id"]}、method={data["method"]} 及原摘要的记录。没有调用答复接口，请重新查看待办。'}
        try:
            payload = request_payload(request, text)
        except ValueError as error:
            return {'state': 'rejected', 'reply': str(error)}
        result = self.call(data['thread_id'], REQUEST_METHODS[data['method']],
                           {'requestId': data['request_id'], **payload}, 1,
                           expected_owner=data['owner_id'], expected_connection=data.get('connection'))
        if result['state'] != 'confirmed' or result['result'].get('ok') is not True:
            return {'state': result['state'] if result['state'] != 'confirmed' else 'unknown',
                    'reply': f'Desktop 未返回 request ID={data["request_id"]} 的有效答复回执（{public_error_code(result.get("error", "missing-ok-true"))}）；答案可能已提交，不会重发。请重新查询该审批状态。'}
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
        before = self.snapshot(thread_id, expected_connection=connection)
        from control_actions import missing_permission_fields, permission_values
        missing = missing_permission_fields(before)
        if before.get('snapshot_complete') is False:
            return {'state': 'rejected',
                    'reply': (f'未修改权限：Desktop 完整状态帧超过 8 MiB；read_thread 摘要只返回最近轮次，缺少完整权限基线'
                              f'（未提供 {", ".join(missing) or "权限完整性标记"}）。没有调用设置更新接口；'
                              '请在 Desktop 设置页查看这些权限值。')}
        if missing or not before.get('permissions_known'):
            absent = ', '.join(missing) if missing else '权限基线完整性标记'
            return {'state': 'rejected',
                    'reply': f'未修改权限：Desktop 完整状态缺少/未确认 {absent}，不能建立安全的当前值基线。没有调用设置更新接口，请在该对话设置页查看这些字段。'}
        from control_actions import signature
        effective = permission_values(before)
        if signature(effective) != baseline['digest'] or before['owner_id'] != baseline['owner_id']:
            return {'state': 'rejected', 'reply': '权限或所有者已变化，未修改。请重新查看权限。'}
        # Only explicit next-turn permission fields; never a current-turn id.
        result = self.call(thread_id, 'thread-follower-update-thread-settings', {
            'threadSettings': changes,
            'condition': {'ifModelEquals': before['model'], 'ifEffortEquals': before['effort']},
        }, 2, expected_owner=before['owner_id'], expected_connection=connection)
        if result['state'] != 'confirmed' or result['result'].get('applied') is not True:
            if result['state'] == 'rejected':
                return {'state': 'rejected', 'reply': f'thread-settings 发送前预检被拒绝（{public_error_code(result.get("error", "missing-applied-true"))}）；权限未更改。'}
            return {'state': 'unknown', 'reply': f'Desktop 未返回权限更新的 applied=true 回执（{public_error_code(result.get("error", "no-valid-response"))}）；权限可能已改变，不会重试。请检查沙箱策略、审批策略和预设。'}
        try:
            after = self.snapshot(thread_id, expected_connection=connection)
            if (after.get('snapshot_complete') is False or not after.get('permissions_known')
                    or missing_permission_fields(after)):
                raise DesktopControlError('permissions-readback-fields-unavailable')
            saved = permission_values(after)
            if not all(saved.get(k) == v for k, v in changes.items()):
                raise DesktopControlError('permissions-readback-mismatch')
        except (OSError, TimeoutError, ValueError, DesktopControlError) as error:
            code = public_error_code(error)
            detail = ('回读没有返回 sandboxPolicy、approvalPolicy 等当前权限字段' if code == 'permissions-readback-fields-unavailable'
                      else '回读的权限值与刚才请求不一致' if code == 'permissions-readback-mismatch'
                      else f'回读失败（{code}）')
            return {'state': 'unknown', 'reply': f'Desktop 已受理权限修改，但{detail}。当前沙箱/审批策略可能已变化，不会重写；请在该对话设置页查看实际值。'}
        return {'state': 'confirmed', 'reply': '权限已保存并回读确认，仅影响此对话后续轮次，当前任务不变。'}

    def compact(self, thread_id: str, connection: str | None = None) -> dict:
        before = self.control_state(thread_id, connection=connection)
        bounded_idle = (before.get('snapshot_complete') is False
                        and before.get('bounded_idle_safe') is True)
        if before.get('snapshot_complete') is False and not bounded_idle:
            bounded = before.get('bounded_snapshot') or {}
            if ((before.get('turn') or {}).get('status') == 'inProgress'
                    or (before.get('runtime') or {}).get('type') == 'active'):
                return {'state': 'rejected',
                        'reply': '未压缩：Desktop 明确报告当前轮次仍在运行（thread=active、turn=inProgress）。请等该轮完成后再发送“压缩上下文”；没有调用压缩接口。'}
            return {'state': 'rejected',
                    'reply': (f"未压缩：Desktop 完整状态帧超过 8 MiB，read_thread 返回 "
                              f"thread={bounded.get('thread_status', '未知')}、turn={bounded.get('turn_status', '未知')}、"
                              f"resume={bounded.get('resume_state', '未知')}；未满足明确空闲且已恢复条件。没有调用压缩接口。")}
        if (before['turn'].get('status') == 'inProgress' or before.get('pending')
                or (before.get('runtime') or {}).get('type') == 'active'
                or (before.get('snapshot_complete') is not False and before['requests'])
                or (before.get('snapshot_complete') is not False and not before.get('requests_known'))):
            if before['turn'].get('status') == 'inProgress' or (before.get('runtime') or {}).get('type') == 'active':
                return {'state': 'rejected', 'reply': '未压缩：Desktop 报告任务仍在运行（thread=active 或 turn=inProgress）。没有调用压缩接口；等任务结束后再试。'}
            if before.get('pending'):
                return {'state': 'rejected', 'reply': '未压缩：本机有一条提交尚无 Desktop 回执。没有调用压缩接口；先在目标对话确认该提交状态。'}
            if before['requests']:
                return {'state': 'rejected', 'reply': f'未压缩：Desktop 当前有 {len(before["requests"])} 个待处理原生请求。没有调用压缩接口；先处理这些请求。'}
            return {'state': 'rejected', 'reply': '未压缩：完整状态未提供原生 requests 列表，无法确认没有待处理审批。没有调用压缩接口；请重新读取状态后再试。'}
        result = self.call(thread_id, 'thread-follower-compact-thread', {}, 1,
                           expected_owner=before['owner_id'], expected_connection=connection)
        accepted = result['state'] == 'confirmed' and result['result'].get('ok') is True
        if accepted:
            reply = 'Desktop 的 compact-thread 返回 ok=true；这只证明已受理，不证明压缩完成。引用本条发送“压缩状态”查询；压缩会调用模型并可能消耗额度。'
            state = 'confirmed'
        elif result['state'] == 'rejected':
            reply = f'compact-thread 发送前预检被拒绝（{public_error_code(result.get("error", "no-error-code"))}）；没有开始压缩。'
            state = 'rejected'
        else:
            reply = f'Desktop 未返回 compact-thread 的 ok=true 回执（{public_error_code(result.get("error", "no-valid-response"))}）；压缩可能已开始，不会重试。请在目标对话检查压缩状态。'
            state = 'unknown'
        return {'state': state, 'reply': reply,
                'baseline': before['compactions'], 'owner_id': before['owner_id']}

    def pause(self, thread_id: str, connection: str | None = None) -> dict[str, Any]:
        # Preserve the already verified native user-stop behavior.
        ok, text = pause_desktop(thread_id, expected_connection=connection, pipe_factory=self.pipe_factory)
        return {"state": "confirmed" if ok else "unknown" if "可能" in text or "确认" in text else "rejected", "reply": text}
