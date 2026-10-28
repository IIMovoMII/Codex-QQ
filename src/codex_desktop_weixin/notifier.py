from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from errors import redact_error_text
from delivery_ledger import DeliveryLedger, MAX_LEDGER_ROWS
from models import Notification, TurnState
from mobile_copy import notification_heading
from runtime_io import append_json_line, atomic_write_json


# A broken QQ/gateway connection must not turn the local disk into an
# unbounded retry queue. Dead-letter files remain available for inspection and
# preserve the delivery ledger's idempotency fact, but are never retried
# automatically.
MAX_PENDING_FILES = 1000
MAX_PENDING_BYTES = 64 * 1024 * 1024


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
        except FileNotFoundError:
            return cls()
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as error:
            raise ValueError("Invalid notification settings") from error
        if not isinstance(value, dict):
            raise ValueError("Invalid notification settings")
        mode_value = value.get("mode", "shadow")
        if not isinstance(mode_value, str) or not mode_value.strip():
            raise ValueError("Invalid notification settings mode")
        mode = mode_value.strip().lower()
        if mode not in ("shadow", "cc-connect"):
            raise ValueError("Invalid notification settings mode")
        timeout_value = value.get("timeout_seconds", 15)
        if isinstance(timeout_value, bool) or not isinstance(timeout_value, int):
            raise ValueError("Invalid notification settings timeout")
        timeout = min(60, max(3, timeout_value))
        string_fields = ('executable', 'project', 'session', 'data_dir', 'config_path')
        if any(field in value and not isinstance(value[field], str) for field in string_fields):
            raise ValueError("Invalid notification settings field")
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


def format_notification(notification: Notification, *, reference_token: str | None = None) -> str:
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
        if reference_token:
            footer += f'\n引用标识：{reference_token}'
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
    reference = f'\n引用标识：{reference_token}' if reference_token else ''
    footer = hint + route + reference
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
        self.sent_state = data_dir / "outbox" / "sent-state"
        self.failed = data_dir / "outbox" / "failed"
        self.dead_letter = data_dir / "outbox" / "dead-letter"
        for path in (self.logs, self.pending, self.sent, self.sent_state, self.failed, self.dead_letter):
            path.mkdir(parents=True, exist_ok=True)
        self.settings_path = data_dir / "settings.json"
        self.settings = DeliverySettings.load(self.settings_path)
        self.shadow = ShadowNotifier(self.logs / "shadow-notifications.jsonl")
        self.delivery_log = self.logs / "delivery.jsonl"
        self.sender = sender or CCConnectSender(self.settings)
        self._last_local_recovery: float | None = None
        self._local_recovery_cooldown = 60.0
        self._notification_scope = self._derive_notification_scope()
        self._control_store = self._open_control_store()
        self.ledger = DeliveryLedger(data_dir / 'state' / 'delivery.sqlite', pending=self.pending,
                                     retained=(self.dead_letter,))
        # If every durable success write fails, stop further sends until this
        # one fact is saved. Memory remains bounded even during disk failure.
        self._unrecorded_success: tuple[Path, dict] | None = None
        shadowed: set[str] = set()
        delivered: set[str] = set()
        # Migrate bounded legacy journals, including rotated success evidence.
        for path in sorted(self.logs.glob("shadow-notifications*.jsonl"),
                           key=lambda p: p.stat().st_mtime, reverse=True)[:5]:
            try:
                with path.open("r", encoding="utf-8") as stream:
                    for line in stream:
                        try:
                            row = json.loads(line)
                            key = row.get("dedupe_key") if isinstance(row, dict) else None
                            if isinstance(key, str) and key:
                                shadowed.add(hashlib.sha256(key.encode('utf-8')).hexdigest()[:32] + '.json')
                                if len(shadowed) >= MAX_LEDGER_ROWS:
                                    break
                        except (json.JSONDecodeError, UnicodeError):
                            continue
            except OSError:
                continue
            if len(shadowed) >= MAX_LEDGER_ROWS:
                break
        for path in sorted(self.logs.glob('delivery*.jsonl'),
                           key=lambda p: p.stat().st_mtime, reverse=True)[:5]:
            try:
                stream = path.open('r', encoding='utf-8')
            except OSError:
                continue
            with stream:
                for line in stream:
                    try:
                        row = json.loads(line)
                        if (isinstance(row, dict) and row.get('success') is True
                                and not row.get('recovery')
                                and row.get('event', 'delivery') == 'delivery'
                                and isinstance(row.get('item'), str)):
                            name = row['item']
                            if (self.pending / name).exists():
                                delivered.add(name)
                    except (json.JSONDecodeError, UnicodeError):
                        continue
        self.ledger.import_facts(shadowed, delivered)
        self.ledger.prune(self.pending, self.dead_letter)

    def _derive_notification_scope(self) -> str | None:
        """Return the configured QQ recipient's exact router scope."""
        session = self.settings.session.strip()
        if not session.startswith('qqbot:'):
            return None
        user_id = session.rsplit(':', 1)[-1]
        if not user_id or user_id == session:
            return None
        project = self.settings.project.strip() or 'default'
        return f'{project}|{session}|{user_id}'

    def _open_control_store(self):
        if not self._notification_scope:
            return None
        try:
            from control_store import ControlStore
            control_root = Path(self.settings.data_dir) if self.settings.data_dir else self.data_dir
            return ControlStore(control_root / 'control-state.sqlite')
        except (OSError, ValueError, sqlite3.Error, json.JSONDecodeError, TypeError):
            # A notification must still be durable when the optional routing
            # index is unavailable. It simply carries no actionable token and
            # the router will fail closed for legacy/plain references.
            return None

    def _reference_token(self, notification: Notification) -> str | None:
        if self._control_store is None or not self._notification_scope or not notification.session_id:
            return None
        kind = 'async_notice' if notification.state == TurnState.QUESTION_AVAILABLE else 'thread'
        data = {'thread_id': notification.session_id}
        if kind == 'async_notice' and notification.question_call_id:
            data['message_id'] = notification.question_call_id
        try:
            return self._control_store.reference(self._notification_scope, kind, data)
        except (OSError, ValueError, sqlite3.Error, TypeError):
            return None

    def _pending_entries(self) -> tuple[list[tuple[float, Path, int]], int]:
        entries: list[tuple[float, Path, int]] = []
        total = 0
        for path in self.pending.glob('*.json'):
            try:
                stat = path.stat()
            except OSError:
                continue
            size = max(0, int(stat.st_size))
            entries.append((float(stat.st_mtime), path, size))
            total += size
        return entries, total

    def _dead_letter(self, path: Path, reason: str) -> None:
        """Move an unsent notice to a bounded, auditable terminal state."""
        destination = self.dead_letter / path.name
        try:
            os.replace(path, destination)
        except FileNotFoundError:
            return
        # The move is the safety boundary. Annotation is best-effort so a
        # locked/invalid JSON file can still leave pending without being lost.
        try:
            value = json.loads(destination.read_text(encoding='utf-8'))
            if isinstance(value, dict):
                value['dead_letter_reason'] = reason
                value['dead_lettered_at'] = datetime.now(timezone.utc).isoformat()
                atomic_write_json(destination, value)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        try:
            append_json_line(self.delivery_log, {
                'at': datetime.now(timezone.utc).isoformat(),
                'item': path.name,
                'event': 'dead_letter',
                'reason': reason,
            })
        except OSError:
            pass

    def _enforce_pending_limits(self, *, incoming_bytes: int = 0) -> None:
        if incoming_bytes > MAX_PENDING_BYTES:
            raise OSError('notification-outbox-item-too-large')
        entries, total = self._pending_entries()
        count = len(entries)
        if count + (1 if incoming_bytes else 0) <= MAX_PENDING_FILES and total + incoming_bytes <= MAX_PENDING_BYTES:
            return
        for _modified, path, size in sorted(entries, key=lambda item: (item[0], item[1].name)):
            if count + (1 if incoming_bytes else 0) <= MAX_PENDING_FILES and total + incoming_bytes <= MAX_PENDING_BYTES:
                break
            self._dead_letter(path, 'pending-capacity')
            count -= 1
            total = max(0, total - size)
        if count + (1 if incoming_bytes else 0) > MAX_PENDING_FILES or total + incoming_bytes > MAX_PENDING_BYTES:
            raise OSError('notification-outbox-capacity')

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
        name = self._item_name(notification)
        if self.settings.mode == "cc-connect":
            if ((self.sent / name).exists() or (self.pending / name).exists()
                    or (self.dead_letter / name).exists()
                    or (self.sent_state / name).exists()
                    or self.ledger.contains(name, delivered=True)
                    or notification.state == TurnState.QUESTION_AVAILABLE and (self.data_dir / 'outbox' / 'obsolete' / name).exists()):
                return
            # The outbox is authoritative. If this atomic write fails, the
            # monitor can roll back its reducer and replay the source record.
            reference_token = self._reference_token(notification)
            item = {
                "notification": notification.to_dict(),
                "message": format_notification(notification, reference_token=reference_token),
                "attempts": 0,
                "next_attempt_at": notification.created_at.isoformat(),
            }
            encoded_size = len(json.dumps(item, ensure_ascii=False, indent=2, sort_keys=True).encode('utf-8'))
            self._enforce_pending_limits(incoming_bytes=encoded_size)
            atomic_write_json(self.pending / name, item)
        if not self.ledger.contains(name):
            self.shadow.emit(notification)
            self.ledger.record(name)

    def prune(self) -> None:
        self._enforce_pending_limits()
        self.ledger.prune(self.pending, self.dead_letter)

    def _persist_success(self, path: Path, item: dict) -> bool:
        durable = False
        try:
            self.ledger.record(path.name, delivered=True)
            durable = True
        except (OSError, sqlite3.Error):
            pass
        # Independent writes: failure of the pending update must not prevent
        # the separate success marker from being attempted.
        for target, value in ((path, item), (self.sent_state / path.name,
                              {'item': path.name, 'sent_at': item['sent_at']})):
            try:
                atomic_write_json(target, value)
                durable = True
            except OSError:
                pass
        return self._archive_sent(path) or durable

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
        # Bound legacy queues left by an older build before creating the scan
        # list. New queues are kept below the same limits by emit().
        self._enforce_pending_limits()
        if self._unrecorded_success is not None:
            path, item = self._unrecorded_success
            if not self._persist_success(path, item):
                return 0
            self._unrecorded_success = None
        delivered = 0
        attempted = 0
        now = datetime.now(timezone.utc)
        pending_rows = []
        for path in self.pending.glob("*.json"):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
                note = item.get("notification") if isinstance(item, dict) else None
                created = datetime.fromisoformat(str(note.get("created_at")).replace("Z", "+00:00")) if isinstance(note, dict) else datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                due = datetime.fromisoformat(str(item.get("next_attempt_at")).replace("Z", "+00:00"))
                pending_rows.append((created.astimezone(timezone.utc), due.astimezone(timezone.utc), path, item))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pending_rows.append((datetime.max.replace(tzinfo=timezone.utc), datetime.min.replace(tzinfo=timezone.utc), path, None))
        pending_rows.sort(key=lambda row: (row[0], row[1], row[2].name))
        for _created, _due, path, loaded_item in pending_rows:
            if attempted >= max_items:
                break
            try:
                if (self.ledger.contains(path.name, delivered=True) or (self.sent / path.name).exists()
                        or (self.sent_state / path.name).exists()):
                    self._archive_sent(path)
                    continue
                if (self.data_dir / 'outbox' / 'obsolete' / path.name).exists():
                    path.unlink(missing_ok=True)
                    continue
                item = loaded_item if loaded_item is not None else json.loads(path.read_text(encoding="utf-8"))
                if item.get('delivery_state') == 'sent':
                    self._archive_sent(path)
                    continue
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
                                "event": "recovery",
                                "recovery_success": recovered,
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
            except sqlite3.Error:
                # An unreadable success ledger is never permission to resend
                # or quarantine a potentially delivered pending notification.
                raise
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
            try:
                append_json_line(
                    self.delivery_log,
                    {"at": now.isoformat(), "item": path.name, "event": "delivery", "success": success,
                     "attempt": attempts, "detail": detail},
                )
            except OSError:
                # The pending record below still gets a durable sent marker.
                pass
            if success:
                # Persist the external success before archive movement. If a
                # Windows scanner holds the file, the next process only retries
                # the rename and never sends the notification again.
                item['delivery_state'] = 'sent'
                item['sent_at'] = now.isoformat()
                if not self._persist_success(path, item):
                    self._unrecorded_success = (path, item)
                    delivered += 1
                    break
                delivered += 1
                continue
            delay = min(300, 5 * (2 ** min(attempts - 1, 6)))
            item["attempts"] = attempts
            item["next_attempt_at"] = (now + timedelta(seconds=delay)).isoformat()
            item["last_error"] = detail
            atomic_write_json(path, item)
        return delivered

    def _archive_sent(self, path: Path) -> bool:
        try:
            os.replace(path, self.sent / path.name)
            return True
        except FileNotFoundError:
            return not path.exists()
        except OSError:
            return False


def save_delivery_settings(path: Path, settings: DeliverySettings) -> None:
    atomic_write_json(path, settings.to_dict())
