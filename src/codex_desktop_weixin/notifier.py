from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from errors import redact_error_text
from models import Notification, TurnState
from mobile_copy import notification_heading
from runtime_io import append_json_line, atomic_write_json


@dataclass(frozen=True)
class DeliverySettings:
    mode: str = "shadow"
    executable: str = "cc-connect"
    project: str = ""
    session: str = ""
    data_dir: str = ""
    config_path: str = ""
    timeout_seconds: int = 15

    @classmethod
    def load(cls, path: Path) -> "DeliverySettings":
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return cls()
        if not isinstance(value, dict):
            return cls()
        mode = str(value.get("mode") or "shadow").strip().lower()
        if mode not in ("shadow", "cc-connect"):
            mode = "shadow"
        try:
            timeout = min(60, max(3, int(value.get("timeout_seconds", 15))))
        except (TypeError, ValueError):
            timeout = 15
        return cls(
            mode=mode,
            executable=str(value.get("executable") or "cc-connect"),
            project=str(value.get("project") or ""),
            session=str(value.get("session") or ""),
            data_dir=str(value.get("data_dir") or ""),
            config_path=str(value.get("config_path") or ""),
            timeout_seconds=timeout,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "executable": self.executable,
            "project": self.project,
            "session": self.session,
            "data_dir": self.data_dir,
            "config_path": self.config_path,
            "timeout_seconds": self.timeout_seconds,
        }


class MessageSender(Protocol):
    def send(self, message: str) -> tuple[bool, str]: ...


def is_local_api_delivery_error(detail: str) -> bool:
    lowered = detail.lower()
    return (
        "api.sock" in lowered
        or "dial unix" in lowered
        or "local api" in lowered
        or "internal api" in lowered
        or ('cc-connect is not running' in lowered and 'socket not found' in lowered)
    )


def recover_local_cc_connect(data_dir: Path) -> tuple[bool, str]:
    # Imported lazily because launcher also reads DeliverySettings from here.
    from launcher import recover_cc_connect_service

    result = recover_cc_connect_service(data_dir)
    return bool(result.get("recovered")), str(result.get("reason") or "unknown")


class CCConnectSender:
    def __init__(self, settings: DeliverySettings) -> None:
        self.settings = settings

    def command(self) -> list[str]:
        executable = shutil.which(self.settings.executable) or self.settings.executable
        command = [executable, "send", "--stdin"]
        if self.settings.project:
            command.extend(["--project", self.settings.project])
        if self.settings.session:
            command.extend(["--session", self.settings.session])
        if self.settings.data_dir:
            command.extend(["--data-dir", self.settings.data_dir])
        return command

    def send(self, message: str) -> tuple[bool, str]:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            result = subprocess.run(
                self.command(),
                # `cc-connect send` 直接调用平台出站接口，不会经过
                # QQ→Desktop ACP 桥接器。这里必须原样发送，否则内部
                # 控制标记会被用户在 QQ 中看到。
                input=message,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.settings.timeout_seconds,
                creationflags=flags,
                check=False,
            )
        except FileNotFoundError:
            return False, "找不到 cc-connect 可执行文件。"
        except subprocess.TimeoutExpired:
            return False, "cc-connect 发送超时。"
        except OSError as error:
            return False, redact_error_text(str(error), limit=300)
        detail = (result.stderr or result.stdout or "").strip()
        return result.returncode == 0, redact_error_text(detail, limit=500)


class ShadowNotifier:
    """Records exactly what would have been sent, without an external call."""

    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path

    def emit(self, notification: Notification) -> None:
        append_json_line(self.log_path, notification.to_dict())


def format_notification(notification: Notification) -> str:
    lines = [notification_heading(notification.title, notification.state)]
    if notification.message.strip():
        lines.extend(["", notification.message.strip()])
    if notification.model:
        lines.extend(["", f"模型：{notification.model}"])
    if notification.state == TurnState.QUESTION_AVAILABLE:
        # Not a general thread card: answering this notice must never quietly
        # become a normal model task. User chose PC-only async answers.
        footer = ('\n\n引用本条发送“待办”可查看仍有效的提问；请在电脑回答，QQ 仅提醒。'
                  f'\n\n异步问题对话：{notification.session_id}\n提问编号：{notification.question_call_id}')
        body = '\n'.join(lines)
        budget = max(0, 2000 - len(footer))
        return (body if len(body) <= budget else body[:max(0, budget - 1)] + '…') + footer
    # QQ 的“引用”事件会把被引用消息正文交给桥接器。保留完整线程编号，
    # 让用户引用任意一条提醒即可准确定位 Desktop 对话，而不是依赖“最近会话”。
    if notification.session_id.strip():
        lines.extend(["", f"对话编号：{notification.session_id.strip()}"])
    # Reserve the routing footer before trimming the body. A long reply must not
    # erase the thread identity that QQ quotation uses.
    route = f"\n\n对话编号：{notification.session_id.strip()}" if notification.session_id.strip() else ""
    if route:
        lines = lines[:-2]
    hint = '\n\n引用本条发送“待办”，查询仍有效的问题或审批；原生身份验证或不支持的请求须在电脑处理。' if notification.state in (TurnState.WAITING_INPUT, TurnState.WAITING_APPROVAL) else ''
    footer = hint + route
    body = "\n".join(lines)
    budget = max(0, 2000 - len(footer))
    if len(body) > budget:
        body = body[:max(0, budget - 1)] + "…"
    return body + footer


class NotificationDispatcher:
    """Always logs locally; optionally drains a durable CC Connect outbox."""

    def __init__(self, data_dir: Path, *, sender: MessageSender | None = None) -> None:
        self.data_dir = data_dir
        self.logs = data_dir / "logs"
        self.pending = data_dir / "outbox" / "pending"
        self.sent = data_dir / "outbox" / "sent"
        self.failed = data_dir / "outbox" / "failed"
        for path in (self.logs, self.pending, self.sent, self.failed):
            path.mkdir(parents=True, exist_ok=True)
        self.settings_path = data_dir / "settings.json"
        self.settings = DeliverySettings.load(self.settings_path)
        self.shadow = ShadowNotifier(self.logs / "shadow-notifications.jsonl")
        self.delivery_log = self.logs / "delivery.jsonl"
        self.sender = sender or CCConnectSender(self.settings)
        self._last_local_recovery: float | None = None
        self._local_recovery_cooldown = 60.0

    @property
    def log_path(self) -> Path:
        return self.shadow.log_path

    def _item_name(self, notification: Notification) -> str:
        # A dedupe key identifies the event, not one rendering of its message.
        # Titles can be resolved late and error wording can improve after a
        # restart; including either would turn one incident into several sends.
        material = notification.dedupe_key or (
            f"{notification.session_id}:{notification.turn_id}:{notification.state.value}"
        )
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
        return digest[:32] + ".json"

    def emit(self, notification: Notification) -> None:
        if notification.state == TurnState.CRASHED:
            return  # Obsolete process-exit inference is never sent or requeued.
        self.shadow.emit(notification)
        if self.settings.mode != "cc-connect":
            return
        name = self._item_name(notification)
        if ((self.sent / name).exists() or (self.pending / name).exists()
                or notification.state == TurnState.QUESTION_AVAILABLE and (self.data_dir / 'outbox' / 'obsolete' / name).exists()):
            return
        atomic_write_json(
            self.pending / name,
            {
                "notification": notification.to_dict(),
                "message": format_notification(notification),
                "attempts": 0,
                "next_attempt_at": notification.created_at.isoformat(),
            },
        )

    def resolve_async_answers(self, session_id: str, answers: list[dict]) -> int:
        """Retire only this batch's unsent notice once ALL its questions were answered.

        Runs on actual incremental answer events, never a polling/timeout scan.
        Sent messages are not recalled; no answer text is stored or forwarded.
        """
        from async_questions import CALL_ID, batch_key
        grouped: dict[str, set[int]] = {}
        for row in answers:
            if not isinstance(row, dict):
                continue
            call, index = row.get('call_id'), row.get('index')
            if (isinstance(call, str) and CALL_ID.fullmatch(call) and isinstance(index, int)
                    and not isinstance(index, bool) and 0 <= index <= 2):
                grouped.setdefault(call, set()).add(index)
        retired = 0
        for call, new_indices in grouped.items():
            name = hashlib.sha256(batch_key(session_id, call).encode('utf-8')).hexdigest()[:32] + '.json'
            path = self.pending / name
            try:
                if not 0 < path.stat().st_size <= 128 * 1024:
                    continue
                item = json.loads(path.read_text(encoding='utf-8'))
                note = item.get('notification') if isinstance(item, dict) else None
                if (not isinstance(note, dict) or note.get('state') != TurnState.QUESTION_AVAILABLE.value
                        or note.get('session_id') != session_id or note.get('question_call_id') != call):
                    continue
                count = note.get('question_count')
                old_indices = item.get('answered_question_indices', [])
                if (not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 3
                        or not isinstance(old_indices, list) or len(old_indices) > 3
                        or any(not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < count for i in old_indices)):
                    continue
                indices = set(old_indices) | {i for i in new_indices if i < count}
                if indices == set(old_indices):
                    continue
                item['answered_question_indices'] = sorted(indices)
                if len(indices) == count:
                    item['obsolete_reason'] = 'async-questions-answered'
                    self._retire_async_notice(path, note, item['obsolete_reason'])
                    retired += 1
                else:
                    atomic_write_json(path, item)
            except (OSError, ValueError, TypeError):
                # A malformed optional notice must not stop ordinary delivery.
                continue
        return retired

    def _retire_async_notice(self, path: Path, note: dict, reason: str) -> None:
        destination = self.data_dir / 'outbox' / 'obsolete' / path.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        # A small tombstone is durable BEFORE pending deletion. A crash cannot
        # cause this expired question to be sent on the next flush.
        atomic_write_json(destination, {k: note.get(k) for k in
                          ('session_id', 'turn_id', 'question_call_id', 'dedupe_key')} | {'reason': reason})
        path.unlink(missing_ok=True)
        append_json_line(self.delivery_log, {'at': datetime.now(timezone.utc).isoformat(),
                        'item': path.name, 'status': 'obsolete', 'reason': reason})

    def close_async_turn(self, session_id: str, turn_id: str) -> int:
        """An actual native end/interrupt expires this turn's UNSENT questions.

        Inspects the existing small outbox only on a terminal event, never every
        second, and never reads Codex history or recalls already sent messages.
        """
        retired = 0
        for path in self.pending.glob('*.json'):
            try:
                if not 0 < path.stat().st_size <= 128 * 1024:
                    continue
                item = json.loads(path.read_text(encoding='utf-8'))
                note = item.get('notification') if isinstance(item, dict) else None
                if (isinstance(note, dict) and note.get('state') == TurnState.QUESTION_AVAILABLE.value
                        and note.get('session_id') == session_id and note.get('turn_id') == turn_id):
                    self._retire_async_notice(path, note, 'native-turn-ended')
                    retired += 1
            except (OSError, ValueError, TypeError):
                continue
        return retired

    def flush(self, *, max_items: int = 1) -> int:
        if self.settings.mode != "cc-connect":
            return 0
        delivered = 0
        attempted = 0
        now = datetime.now(timezone.utc)
        for path in sorted(self.pending.glob("*.json")):
            if attempted >= max_items:
                break
            try:
                if (self.data_dir / 'outbox' / 'obsolete' / path.name).exists():
                    path.unlink(missing_ok=True)
                    continue
                item = json.loads(path.read_text(encoding="utf-8"))
                note = item.get('notification') or {}
                if isinstance(note, dict) and note.get('state') == TurnState.CRASHED.value:
                    self._retire_async_notice(path, note, 'process-exit-notifications-disabled')
                    continue
                next_attempt = datetime.fromisoformat(str(item.get("next_attempt_at")).replace("Z", "+00:00")).astimezone(timezone.utc)
                if next_attempt > now:
                    continue
                attempted += 1
                success, detail = self.sender.send(str(item.get("message") or ""))
                if not success and is_local_api_delivery_error(detail):
                    monotonic_now = time.monotonic()
                    if (
                        self._last_local_recovery is None
                        or monotonic_now - self._last_local_recovery >= self._local_recovery_cooldown
                    ):
                        self._last_local_recovery = monotonic_now
                        recovered, recovery_detail = recover_local_cc_connect(self.data_dir)
                        append_json_line(
                            self.delivery_log,
                            {
                                "at": now.isoformat(),
                                "item": path.name,
                                "success": recovered,
                                "attempt": int(item.get("attempts", 0)),
                                "recovery": "cc_connect_local_api",
                                "detail": recovery_detail,
                            },
                        )
                        if recovered:
                            success, detail = self.sender.send(str(item.get("message") or ""))
                        else:
                            detail = f"{detail}; 自动恢复失败：{recovery_detail}"
                attempts = int(item.get("attempts", 0)) + 1
            except Exception as error:
                detail = redact_error_text(str(error), limit=300)
                append_json_line(
                    self.delivery_log,
                    {"at": now.isoformat(), "item": path.name, "success": False, "attempt": 0, "detail": detail},
                )
                try:
                    os.replace(path, self.failed / path.name)
                except OSError:
                    pass
                attempted += 1
                continue
            append_json_line(
                self.delivery_log,
                {
                    "at": now.isoformat(),
                    "item": path.name,
                    "success": success,
                    "attempt": attempts,
                    "detail": detail,
                },
            )
            if success:
                os.replace(path, self.sent / path.name)
                delivered += 1
                continue
            delay = min(300, 5 * (2 ** min(attempts - 1, 6)))
            item["attempts"] = attempts
            item["next_attempt_at"] = (now + timedelta(seconds=delay)).isoformat()
            item["last_error"] = detail
            atomic_write_json(path, item)
        return delivered


def save_delivery_settings(path: Path, settings: DeliverySettings) -> None:
    atomic_write_json(path, settings.to_dict())
