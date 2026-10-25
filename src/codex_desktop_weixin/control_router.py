"""Deterministic QQ commands, stable references and bounded native dispatch."""

from __future__ import annotations

import json
import inspect
import math
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from control_backend import NativeBackend
from control_catalog import LocalCatalog
from control_store import ControlStore
from desktop_control import DesktopControlError
from mobile_copy import help_text


REFERENCE = re.compile(r"^引用标识：([A-Za-z0-9_-]{12})$", re.MULTILINE)
THREAD = re.compile(r"^对话编号：[A-Za-z0-9_-]{20,100}$", re.MULTILINE)
ASYNC_NOTICE = re.compile(r'^异步问题对话：([A-Za-z0-9_-]{20,100})\r?\n提问编号：([A-Za-z0-9_-]{1,160})$', re.MULTILINE)
OFFLINE = "Codex 未启动或当前无法连接。这条指令已作废，不会在恢复后自动执行。请恢复连接后重新发送。"
UNKNOWN = "指令已发出，但没有收到确认，可能已经执行。不会自动重发，请先查看目标对话。"
NEEDS_QUOTE = "这条消息没有引用目标对话，未执行。请引用选定对话的回执或任务提醒，再发送指令；也可先发送“对话”查看列表。"


class ControlRouter:
    def __init__(self, store: ControlStore, catalog: Any, backend: Any,
                 queue_func: Callable[[str, str], tuple[bool, str]],
                 availability_check: Callable[[], bool] | None = None) -> None:
        self.store, self.catalog, self.backend = store, catalog, backend
        self.queue_func = queue_func
        self.availability_check = availability_check
        self._process_started_at = time.time()
        self._bridge_scopes: set[str] = set()
        self.store.prune()

    @classmethod
    def runtime(cls, queue_func: Callable[[str, str], tuple[bool, str]]) -> ControlRouter:
        data = Path(os.environ.get("CC_DATA_DIR") or Path(os.environ.get("LOCALAPPDATA", "")) / "CodexCC")
        codex = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        from runtime_io import default_data_dir
        root = default_data_dir()
        def available() -> bool:
            from runtime_mode import runtime_mode
            from launcher import read_monitor_pid
            mode = runtime_mode(root)
            return mode == 'desktop' or mode == 'independent' and read_monitor_pid(root, require_ready=True) is not None
        catalog = LocalCatalog(codex)
        return cls(ControlStore(data / "control-state.sqlite", legacy_routes=data / "qq-desktop-routes.json"),
                   catalog, NativeBackend(catalog=catalog), queue_func, availability_check=available)

    def handle(self, params: Any) -> str:
        if self.availability_check is not None:
            try:
                if not self.availability_check():
                    return 'QQ 助手未就绪或已退出，这条指令已作废，不会补执行。请在电脑开启应用后重新发送。'
            except (OSError, ValueError):
                return '无法核实 QQ 助手状态，未执行；请在电脑核对并重新发送。'
        envelope = params.get("_meta") if isinstance(params, dict) else None
        meta = envelope.get("cc_connect") if isinstance(envelope, dict) else None
        expected = os.environ.get("CC_SESSION_KEY", "")
        if not isinstance(meta, dict) or meta.get("version") != 1 or meta.get("platform") != "qqbot" or meta.get("session_key") != expected or not expected:
            return "入站消息缺少可信的原始身份和时间，未执行。需要启用配套的 CC Connect 元信息转发。"
        if not isinstance(meta.get("text"), str) or not isinstance(meta.get("user_id"), str) or not meta["user_id"]:
            return "入站身份或正文不完整，未执行。"
        if meta.get("quoted_text") is not None and not isinstance(meta["quoted_text"], str):
            return "引用信息格式不完整，未执行。请重新引用目标消息。"
        try:
            meta['text'].encode('utf-8', errors='strict')
            (meta.get('quoted_text') or '').encode('utf-8', errors='strict')
        except UnicodeEncodeError:
            return '消息包含损坏字符，未执行。请重新发送原文。'
        if len(meta['text']) > 4000 or len(meta.get('quoted_text') or '') > 16000:
            return '正文或引用过长，未执行。正文最多 4,000 字，请重新选择简短卡片。'
        message_id = meta.get("message_id")
        if not isinstance(message_id, str) or not 1 <= len(message_id) <= 240:
            return "入站消息缺少原始编号，未执行。"
        scope = f"{os.environ.get('CC_PROJECT','default')}|{expected}|{meta['user_id']}"
        # A bridge restart is a new control generation for this QQ session,
        # even when Desktop and its process identity remain unchanged. Keep
        # the cutoff scoped so another user's ACP process is not invalidated.
        if scope not in self._bridge_scopes:
            try:
                self.store.begin_bridge_session(scope, now=self._process_started_at)
            except (OSError, sqlite3.Error):
                # Keep the existing state and fail closed just like later
                # event-state writes. A locked/corrupt store must not allow
                # an untracked native mutation or crash the ACP request.
                return '本地控制状态暂不可用，未执行这条指令；没有清空或重建已有状态。'
            self._bridge_scopes.add(scope)
        try:
            sent_at = float(meta["sent_at_ms"]) / 1000
            gateway_ready = float(meta["connection_ready_at_ms"]) / 1000
            if not math.isfinite(sent_at) or not math.isfinite(gateway_ready) or gateway_ready <= 0 or sent_at <= 0 or sent_at > time.time() + 2:
                raise ValueError("invalid-event-time")
        except (KeyError, TypeError, ValueError):
            return "无法确认这条消息的发送时间，指令已作废，请重新发送。"
        try:
            duplicate = self.store.begin(scope, message_id, sent_at)
        except (OSError, sqlite3.Error):
            return '本地控制状态暂不可用，未执行这条指令；没有清空或重建已有状态。'
        if duplicate is not None:
            return duplicate
        outcome = "confirmed"
        probed = False
        dispatching = False
        try:
            identity, started_at = self.backend.probe()
            probed = True
            since = self.store.ready(identity, True, now=started_at, scope=scope)
            if sent_at < max(since, gateway_ready):
                outcome, reply = "rejected", "这条消息来自离线或重连前，已作废。恢复后请重新发送；旧对话卡片仍可引用。"
            else:
                meta = {**meta, '_desktop_identity': identity}
                dispatching = True
                reply, outcome = self._dispatch(scope, meta)
        except (OSError, TimeoutError):
            self.store.offline(scope)
            outcome, reply = ('unknown', UNKNOWN) if dispatching else ('rejected', OFFLINE)
        except Exception as error:
            # A dispatched mutation can have succeeded. Never transform an exception
            # into an instruction replay. Keep the exception type, not its body.
            if isinstance(error, DesktopControlError):
                # An unloaded conversation or an unsupported method is not a
                # global disconnect. Other owner windows remain usable.
                if not probed or str(error) == "connection-closed":
                    self.store.offline(scope)
                if 'contract' in str(error) or 'installation-mismatch' in str(error):
                    outcome, reply = 'rejected', '这项操作的原生接口格式已变化，未执行；其他兼容功能和通知继续可用。请更新 QQ 助手的接口适配；不需要固定 Codex 安装版本。'
                elif str(error).startswith('desktop-capability-source-'):
                    outcome, reply = 'rejected', '暂时无法读取当前 Codex 的接口声明，这项操作未执行、不会补发。请检查当前应用是否已完成更新；聊天记录未修改。'
                elif str(error).startswith('native-app-tools-'):
                    outcome, reply = 'rejected', '当前 Desktop 的本机工具服务未连接，新操作未执行。请从本任务重载 QQ 桥接以更新环境；已有通知、发送和暂停不受此能力限制。'
                elif str(error).startswith('no-client-found') or str(error) in ('owner-mismatch', 'owner-changed-before-dispatch'):
                    outcome, reply = 'rejected', '无法连接该对话的当前窗口：聊天记录存在，但没有可响应的窗口所有者（未载入、切换窗口或正在重连）。请在电脑点开这个对话后重发；本条不会以后补执行。'
                elif str(error) == 'request-version-mismatch':
                    outcome, reply = 'rejected', '该操作的原生接口版本不匹配；不是聊天记录丢失，也不能靠换对话解决。未执行，请更新桥接适配。'
                else:
                    outcome, reply = 'unknown', '原生控制状态或返回结果无法核实，不能认定是对话未打开。不会自动重发，请在电脑核对；已有聊天记录未修改。'
            else:
                outcome, reply = "unknown", f"处理结果未确认（{type(error).__name__}），不会自动重发。"
        try:
            self.store.finish(scope, message_id, outcome, reply)
        except (OSError, sqlite3.Error):
            return reply + '\n本地处理回执未能保存；原指令不会自动重发，请重新查询状态。'
        return reply

    def _card(self, scope: str, kind: str, data: Any, body: str, *, ttl: float | None = None) -> str:
        token = self.store.reference(scope, kind, data, ttl=ttl)
        footer = ""
        if kind == "thread":
            footer = f"\n\n对话编号：{data['thread_id']}"
        footer += f"\n引用标识：{token}"
        return body[:2000 - len(footer)] + footer

    def _backend_call(self, name: str, *args: Any, connection: str | None = None) -> Any:
        method = getattr(self.backend, name)
        parameters = {}
        try:
            parameters = inspect.signature(method).parameters
            supports_connection = ('connection' in parameters or 'expected_connection' in parameters
                                   or any(p.kind is inspect.Parameter.VAR_POSITIONAL
                                          for p in parameters.values()))
        except (TypeError, ValueError):
            supports_connection = True
        if connection is not None and supports_connection:
            if 'expected_connection' in parameters and 'connection' not in parameters:
                return method(*args, expected_connection=connection)
            return method(*args, connection)
        return method(*args)

    def _thread_card(self, scope: str, row: dict, status: str = '', connection: str | None = None) -> str:
        data = {"thread_id": row["id"]}
        if connection:
            data["connection"] = connection
        return self._card(scope, "thread", data,
                          f"💬 已选定 {self.catalog.label(row)}\n\n引用本条，说说接下来要做什么。\n这条回执以后也能用；每次操作都请引用目标。" +
                          (f"\n窗口状态：{status}" if status else ''))

    def _selected_thread_card(self, scope: str, row: dict, connection: str) -> tuple[str, str]:
        """Only an explicit selection opens an unloaded chat; lists stay read-only."""
        thread_id = row['id']
        try:
            self.backend.ensure_owner(thread_id)
            return self._thread_card(scope, row, '已连接。', connection), 'confirmed'
        except (OSError, TimeoutError, DesktopControlError):
            pass
        result = self.backend.open_thread(thread_id, connection, str(uuid.uuid4()))
        if result['state'] != 'confirmed':
            return self._thread_card(scope, row, result['reply'], connection), result['state']
        deadline = time.monotonic() + 3.0
        while True:
            try:
                self.backend.ensure_owner(thread_id)
                return self._thread_card(scope, row, '已打开并连接。', connection), 'confirmed'
            except (OSError, TimeoutError, DesktopControlError):
                if time.monotonic() >= deadline:
                    return (self._thread_card(scope, row,
                            '已请求打开，但尚未确认连接。请稍后引用本卡片再发；当前没有发送任务。', connection), 'unknown')
                time.sleep(0.1)

    def _list_card(self, scope: str, kind: str, rows: list[dict], title: str,
                   offset: int = 0, connection: str | None = None) -> str:
        lines, part = [title], []
        for row in rows:
            label = str(row["name"]).replace("\n", " ")[:80] if kind == "projects" else self.catalog.label(row) + " · " + row.get("status", "状态未知")
            line = f"{offset + len(part) + 1}. {label}"
            if sum(len(x) + 1 for x in lines) + len(line) > 1550:
                break
            lines.append(line)
            part.append(row)
        remaining = rows[len(part):]
        lines.extend(["", "引用本条回复序号选择。"])
        if remaining:
            lines.append("引用回复“下一页”继续查看。")
        data = {"entries": part, "remaining": remaining, "offset": offset, "title": title}
        if connection:
            data["connection"] = connection
        return self._card(scope, kind, data, "\n".join(lines), ttl=1800)

    def _resolve_quote(self, scope: str, quote: str) -> dict | None:
        tokens = REFERENCE.findall(quote)
        if len(tokens) == 1 and quote.rstrip().endswith("引用标识：" + tokens[0]):
            found = self.store.get_reference(scope, tokens[0])
            if found is None:
                raise ValueError("unknown-reference")
            return found
        if tokens:
            raise ValueError("ambiguous-reference")
        async_notices = list(ASYNC_NOTICE.finditer(quote.rstrip()))
        if async_notices:
            if len(async_notices) != 1 or async_notices[0].end() != len(quote.rstrip()):
                raise ValueError('ambiguous-async-notice')
            match = async_notices[0]
            return {'kind': 'async_notice', 'data': {'thread_id': match[1], 'message_id': match[2]}}
        # Legacy notifications have exactly one complete identity as their footer.
        matches = THREAD.findall(quote)
        if len(matches) == 1 and quote.rstrip().endswith(matches[0]):
            return {"kind": "thread", "data": {"thread_id": matches[0].split("：", 1)[1]}}
        if quote:
            raise ValueError("missing-reference")
        return None

    def _dispatch(self, scope: str, meta: dict) -> tuple[str, str]:
        text = meta["text"].strip()
        quote = meta.get("quoted_text") or ""
        # Help is navigation, not an answer to an operation card. Never feed it
        # into a pending question or a new-task wizard merely due to a quote.
        help_parts = text.split(maxsplit=1)
        if help_parts and help_parts[0].lower() in ('帮助', '/help', '/帮助'):
            return help_text(help_parts[1] if len(help_parts) > 1 else ''), 'confirmed'
        try:
            ref = self._resolve_quote(scope, quote)
        except ValueError:
            return "引用信息不完整或已过期，请发送“对话”重新选择；不会猜测目标。", "rejected"
        # A target is valid only for this incoming quotation. Never route an
        # unquoted command using a remembered selection from an earlier message.
        thread_id = ref["data"].get("thread_id") if ref and isinstance(ref["data"], dict) else None
        kind = ref["kind"] if ref else None
        if ref and kind == "setting_options":
            # Model/effort options are mutating cards. Unlike old thread
            # selection notices, they must always carry the current connection
            # generation; otherwise a pre-restart card could change the new
            # window's settings.
            if ref["data"].get("connection") != meta.get("_desktop_identity"):
                return "这张设置卡片来自旧连接，已过期。请重新查看目标对话；不会修改任何设置。", "rejected"
        elif ref and kind in {"thread", "project", "threads", "projects"}:
            # Legacy plain notifications never carried a connection token; keep
            # their read/selection compatibility. Every newly generated card
            # carries one and is rejected after a Desktop restart.
            if (ref["data"].get("connection") is not None
                    and ref["data"].get("connection") != meta.get("_desktop_identity")):
                return "这张卡片来自旧连接，已过期。请重新发送“对话”并选择目标；不会修改任何设置。", "rejected"
        from control_commands import ActionCommands
        from creation_commands import CreationCommands
        actions = ActionCommands(self, scope, meta.get('_desktop_identity', ''))
        creation = CreationCommands(self, scope, meta.get('_desktop_identity', ''))
        if ref:
            result = creation.quoted(ref, text)
            if result is not None:
                return result
            result = actions.quoted(ref, text)
            if result is not None:
                return result
        if kind in ("threads", "projects"):
            offset = ref["data"].get("offset", 0)
            if text == "下一页":
                remaining = ref["data"].get("remaining") or []
                if not remaining:
                    return "这份列表已经没有下一页。", "confirmed"
                return self._list_card(scope, kind, remaining, ref["data"]["title"],
                                       offset + len(ref["data"]["entries"]), meta['_desktop_identity']), "confirmed"
            if not text.isdigit():
                return "请引用这份列表回复序号。需要发送任务时，请引用选定对话的卡片。", "rejected"
            index = int(text) - 1 - offset
            entries = ref["data"]["entries"]
            if not 0 <= index < len(entries):
                return "序号不在这份列表里，请重新选择。", "rejected"
            row = entries[index]
            if kind == "projects":
                return self._card(scope, "project", {"project_id": row["id"], "connection": meta['_desktop_identity']},
                                  f"📁 已选定项目：{row['name']}\n\n引用本条发送“对话”或“搜索 关键词”查看项目对话。"), "confirmed"
            verified = self.catalog.thread(row["id"])
            if not verified:
                return "该对话已不在可控制列表，请重新选择。", "rejected"
            return self._selected_thread_card(scope, verified, meta['_desktop_identity'])
        if kind == "setting_options":
            data = ref["data"]
            if text == "下一页":
                if not data.get("remaining"):
                    return "这份选项已经没有下一页。", "confirmed"
                row = self.catalog.thread(thread_id)
                if not row:
                    return "目标对话已不在可控制列表，请重新选择。", "rejected"
                return self._setting_options_card(scope, row, data["field"], data["baseline"], data["remaining"], data["source"],
                                                  data.get("offset", 0) + len(data["options"]), meta['_desktop_identity']), "confirmed"
            if text == "取消":
                return "已取消，没有修改设置。", "confirmed"
            parts = text.split()
            options = ref["data"]["options"]
            index = int(parts[0]) - 1 - data.get("offset", 0) if parts and parts[0].isdigit() else -1
            if not 0 <= index < len(options) or len(parts) > 2:
                return "请引用这份选项回复序号；选模型可加档位，例如“2 high”。回复“取消”不修改。", "rejected"
            option = options[index]
            if ref["data"]["field"] == "model":
                changes = {"model": option["model"]}
                if len(parts) == 2:
                    changes["effort"] = parts[1]
            else:
                if len(parts) != 1:
                    return "选择思考档位只需回复序号。", "rejected"
                changes = {"effort": option["effort"]}
            return self._change_model_settings(scope, thread_id, changes, ref["data"]["baseline"], meta['_desktop_identity'])
        if kind not in (None, "thread", "project", "settings"):
            return "这条操作卡片当前不能按普通正文回复，请重新查看对应功能。", "rejected"
        command, _, arg = text.partition(" ")
        command, arg = command.lower(), arg.strip()
        if command in ("/forget", "/取消", "取消选择"):
            self.store.select(scope, None)
            return "已清除旧版选择记录。每次操作仍需引用目标对话；已有卡片继续有效。", "confirmed"
        if command in ("项目",):
            rows = self.catalog.projects()
            if not rows:
                return "没有可控制的本地项目。可发送“对话”查看无项目对话。", "confirmed"
            return self._list_card(scope, "projects", rows, "项目", connection=meta['_desktop_identity']), "confirmed"
        if command in ("对话", "搜索"):
            if command == "对话" and arg and (not arg.isdigit() or not 1 <= int(arg) <= 50):
                return "数量请选择 1～50，例如“对话 20”。", "rejected"
            if command == "搜索" and not arg:
                return "请提供标题关键词，例如“搜索 登录”。", "rejected"
            count = int(arg) if command == "对话" and arg else 10
            project_id = ref["data"]["project_id"] if kind == "project" else None
            rows = self.catalog.threads(count=count, search=arg if command == "搜索" else "", project_id=project_id)
            if not rows:
                return "没有找到对应的本地对话。", "confirmed"
            budget = time.monotonic() + 5.0
            for r in rows:
                r["status"] = self.backend.status(r["id"]) if time.monotonic() < budget else "状态未即时核实"
            return self._list_card(scope, "threads", rows, "搜索结果" if command == "搜索" else "最近对话",
                                   connection=meta['_desktop_identity']), "confirmed"
        if command in ("/use", "/选择"):
            row = self.catalog.thread(arg)
            if row is None:
                return "未找到可控制的本地对话。发送“对话”按标题选择即可。", "rejected"
            return self._selected_thread_card(scope, row, meta['_desktop_identity'])
        if command == "新建项目":
            return "当前 Desktop 的本机服务没有公布项目创建工具，暂不能从 QQ 登记新项目。已有项目可用于“新建对话”；不会调用隐藏方法或改写项目文件补齐。", "rejected"
        if command == "新建对话":
            if not thread_id or kind not in ('thread', 'settings'):
                return '请引用一个已有对话的回执发送“新建对话 [标题]”，再选择新项目和首条任务。已有对话只作原生调用来源，不会在里面执行新任务。', 'rejected'
            if not self.catalog.thread(thread_id):
                return '原生调用来源已不可用，请重新选择。', 'rejected'
            return creation.begin(thread_id, arg)
        if not thread_id:
            return NEEDS_QUOTE, "rejected"
        if command in ('删除', '压缩状态', '确认创建', '确认分支'):
            return '请引用对应的操作或确认卡片，不能用普通对话卡片代替；没有发送新任务。', 'rejected'
        row = self.catalog.thread(thread_id)
        if not row:
            return "目标对话不存在或不属于可控制的本地对话，请重新选择。", "rejected"
        if kind == "thread" and not text:
            return "已识别引用的 Codex 对话。\n" + self._thread_card(scope, row, connection=meta['_desktop_identity']), "confirmed"
        extended = actions.command(thread_id, command, arg)
        if extended is not None:
            return extended
        if command in ("模型", "思考档位"):
            before = self._backend_call('read_settings', thread_id, connection=meta['_desktop_identity'])
            try:
                catalog = self.catalog.models()
            except (OSError, ValueError, KeyError, TypeError):
                return "不能确认本机当前模型目录，未修改设置。请先在电脑刷新模型列表。", "rejected"
            if arg:
                parts = arg.split()
                if command == "模型" and 1 <= len(parts) <= 2:
                    changes = {"model": parts[0], **({"effort": parts[1]} if len(parts) == 2 else {})}
                elif command == "思考档位" and len(parts) == 1:
                    changes = {"effort": parts[0]}
                else:
                    return "可用“模型 模型名 [档位]”或“思考档位 档位”；不加参数可查看选项。", "rejected"
                return self._change_model_settings(scope, thread_id, changes, before, meta['_desktop_identity'])
            field = "model" if command == "模型" else "effort"
            if field == "model":
                options = catalog["options"]
            else:
                current = next((o for o in catalog["options"] if o["model"] == before["model"]), None)
                if not current:
                    return "当前模型不在已确认的目录中，未提供猜测档位。请在电脑刷新列表。", "rejected"
                options = [{"effort": e} for e in current["efforts"]]
            return self._setting_options_card(scope, row, field, {k: before.get(k) for k in ("model", "effort")}, options,
                                              catalog["source"], connection=meta['_desktop_identity']), "confirmed"
        if command in ("设置", "/current", "/当前"):
            view = self._backend_call('read_settings', thread_id, connection=meta['_desktop_identity'])
            stored_sandbox = row.get("sandbox_policy") or "{}"
            try:
                stored_sandbox = json.loads(stored_sandbox) if isinstance(stored_sandbox, str) else stored_sandbox
            except ValueError:
                stored_sandbox = {}
            permissions = view.get("permissions") or view.get("settings") or {"sandboxPolicy": stored_sandbox, "approvalPolicy": row.get("approval_mode")}
            sandbox = permissions.get("sandboxPolicy") or {}
            level = {"dangerFullAccess": "完全访问", "danger-full-access": "完全访问", "disabled": "完全访问", "workspaceWrite": "工作区写入", "workspace-write": "工作区写入", "readOnly": "只读", "read-only": "只读"}.get(sandbox.get("type"), "原生默认")
            effort = view.get("effort") or row.get("reasoning_effort") or "原生默认"
            return self._card(scope, "thread", {"thread_id": thread_id, "connection": meta['_desktop_identity']},
                              f"{self.catalog.label(row)}\n模型：{view.get('model') or '原生默认'}\n思考档位：{effort}\n权限：{level}\n审批策略：{permissions.get('approvalPolicy') or '原生默认'}\n运行状态：{self.backend.status(thread_id)}\n模型来自当前窗口；档位和权限采用窗口返回值或后台已保存设置。\nQQ 回复沿用此对话的原生设置。"), "confirmed"
        if command in ("暂停", "停止", "/pause", "/暂停"):
            if arg:
                return "暂停指令不需要附加参数。发送普通文字可用“发送 正文”。", "rejected"
            result = self._backend_call('pause', thread_id, connection=meta['_desktop_identity'])
            return result.get("reply", UNKNOWN), result["state"]
        payload = "继续" if text in ("继续", "/continue", "/继续") else arg if command in ("发送", "/send", "/发送") else text
        if not payload or len(payload) > 4000:
            return "消息不能为空，且最多允许 4,000 个字符。", "rejected"
        try:
            payload.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            return "消息包含损坏字符，未发送，请重新输入原文。", "rejected"
        # Match Desktop's composer: idle/paused starts a new native turn; a
        # running task appends to the same visible native follow-up list.
        result = self.backend.submit_message(thread_id, payload, meta['_desktop_identity'],
                                             str(uuid.uuid4()), self.queue_func)
        if result['state'] != 'confirmed':
            return result['reply'], result['state']
        if result['mode'] == 'started':
            return self._card(scope, 'thread', {'thread_id': thread_id, 'connection': meta['_desktop_identity']},
                              f"💬 {self.catalog.label(row)}\n\n已进入对话，这条新消息没有排队。\n沿用本对话的模型、思考档位和权限，完成或出错后再提醒你。\n以前的排队消息仍保留。"), 'confirmed'
        if result['mode'] == 'queued':
            if result.get('native') and result.get('entry'):
                entry = result['entry']
                body = (f"{self.catalog.label(row)}\n\n"
                        "⏳ 已加入 Codex Desktop 的可见待发送列表，会在当前任务后按顺序处理。\n"
                        f"队列编号：{entry['id']}\n"
                        "引用本条回复“队列”可查看；回复“删除”移除它，“引导”先移除再把原文交给当前任务。\n"
                        "不会把消息放入看不见的后台队列，也不会自动改成引导。")
                return self._card(scope, 'queued', {'thread_id': thread_id, 'entry': entry,
                                                    'connection': meta['_desktop_identity']}, body, ttl=None), 'confirmed'
            from submission_receipts import queue_ack, digest, status_text
            queue_id = queue_ack(result['detail'], thread_id)
            if queue_id:
                receipt = {'thread_id': thread_id, 'queue_id': queue_id,
                           'text_digest': digest(payload), 'client_id': None,
                           'connection': meta['_desktop_identity']}
                observed = self.backend.submission_status(receipt, self.catalog, meta['_desktop_identity'])
                receipt['client_id'] = observed.get('client_id')
                body = f"{self.catalog.label(row)}\n已提交给 Codex。{status_text(observed)}"
                body += '\n\n引用本条回复“状态”查看这一条；回复“删除”仅移除仍在排队的本条消息。'
                body += '\n此发送回执不支持直接转引导；引用“对话”返回的回执发“队列”，可查看并操作仍可核实的原生队列项。'
                return self._card(scope, 'submission', receipt, body, ttl=None), 'confirmed'
            return self._card(scope, 'thread', {'thread_id': thread_id, 'connection': meta['_desktop_identity']},
                              f"{self.catalog.label(row)}\n已提交给 Codex，但原生回执没有可核实的消息编号，执行状态未确认；不能据此认定已开始。\n完成或出错后会继续提醒。引用本条发送“队列”可查看当前可见的本地记录；不会重发。"), "confirmed"
        return UNKNOWN, 'unknown'

    def _change_model_settings(self, scope: str, thread_id: str, changes: dict,
                               expected: dict, connection: str) -> tuple[str, str]:
        row = self.catalog.thread(thread_id)
        if not row:
            return "目标对话已不在可控制列表，未修改。", "rejected"
        try:
            catalog = self.catalog.models()
        except (OSError, ValueError, KeyError, TypeError):
            return "模型目录暂不可确认，未修改设置。请在电脑刷新列表。", "rejected"
        model = changes.get("model", expected.get("model"))
        effort = changes.get("effort", expected.get("effort"))
        option = next((o for o in catalog["options"] if o["model"] == model), None)
        if not option or effort not in option["efforts"]:
            return "模型或档位不在当前目录的可选组合中，未修改；不会自动降档。请重新查看模型/档位选项。", "rejected"
        result = self._backend_call('update_model_settings', thread_id, changes, expected,
                                    connection=connection)
        value = result.get("settings")
        reply = self.catalog.label(row) + "\n" + result["reply"]
        if value:
            reply += f"\n模型：{value.get('model')}\n思考档位：{value.get('effort')}"
        return self._card(scope, "thread", {"thread_id": thread_id, "connection": connection}, reply), result["state"]

    def _setting_options_card(self, scope: str, row: dict, field: str, baseline: dict,
                              options: list[dict], source: str, offset: int = 0,
                              connection: str | None = None) -> str:
        title = "模型" if field == "model" else "思考档位"
        lines = [self.catalog.label(row), f"当前：{baseline['model']} / {baseline['effort']}", f"{title}选项（{source}）"]
        shown = []
        for option in options:
            label = (option["name"].replace("\n", " ")[:80] + " · " + ", ".join(option["efforts"])) if field == "model" else option["effort"]
            line = f"{offset + len(shown) + 1}. {label}"
            if sum(len(x) + 1 for x in lines) + len(line) > 1500:
                break
            lines.append(line)
            shown.append(option)
        if not shown:
            raise ValueError("model-option-too-large")
        remaining = options[len(shown):]
        lines.extend(["", "引用回复序号选择；选模型可附档位，例如“2 high”。", "只影响后续轮次，不自动降档；目录列出不保证服务当前可用。"])
        if remaining:
            lines.append("引用回复“下一页”查看更多。")
        data = {"thread_id": row["id"], "field": field, "options": shown,
                "remaining": remaining, "offset": offset, "source": source,
                "baseline": baseline, "connection": connection}
        return self._card(scope, "setting_options", data, "\n".join(lines), ttl=600)
