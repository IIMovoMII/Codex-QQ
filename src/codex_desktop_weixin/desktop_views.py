"""Bounded metadata projections. Never expose message bodies or raw gateway logs."""
from __future__ import annotations
import itertools
import json
from pathlib import Path
from datetime import datetime, timezone

def notifications(data: Path, *, offset=0, limit=200):
    if not isinstance(offset, int) or not isinstance(limit, int) or offset < 0 or not 1 <= limit <= 200:
        raise ValueError("notification_page_invalid")
    entries = []
    locations = (("pending", "pending"), ("sent", "sent"), ("failed", "failed"), ("dead-letter", "terminated"))
    for folder, state in locations:
        directory = data / "outbox" / folder
        for path in itertools.islice(directory.glob("*.json"), 5000):
            try:
                item_state = state
                if path.stat().st_size > 256*1024 or path.is_symlink():
                    continue
                item = json.loads(path.read_text(encoding="utf-8"))
                delivery_state = item.get('delivery_state')
                if delivery_state in ('sent', 'unknown', 'failed', 'terminated'):
                    item_state = delivery_state
                note = item.get("notification", item)
                created = note.get("created_at")
                timestamp = datetime.fromisoformat(created.replace("Z", "+00:00")) if isinstance(created, str) else None
                if timestamp is None or timestamp.tzinfo is None:
                    continue
                local = timestamp.astimezone()
                age = max(0, (datetime.now(timezone.utc) - timestamp).total_seconds()/86400)
                raw_state = str(note.get("state", ""))
                event = "error" if raw_state in ("failed", "error") else "complete" if raw_state in ("completed", "complete") else "attention"
                timeline = [{"time": local.strftime("%Y-%m-%d %H:%M:%S"), "label": "通知已生成", "icon": "file"}]
                sent = item.get("sent_at")
                if item_state == "sent" and isinstance(sent, str):
                    timeline.append({"time": datetime.fromisoformat(sent.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M:%S"),
                                     "label": "平台确认发送", "icon": "send"})
                entries.append({"id": path.name, "time": local.strftime("%m-%d %H:%M"), "ageDays": age,
                                "event": event, "title": "任务完成" if event == "complete" else "任务出错" if event == "error" else "需要处理",
                                "target": "已配置通知会话", "state": item_state, "timeline": timeline, "_sort": timestamp.timestamp()})
            except (OSError, ValueError, TypeError, AttributeError):
                continue
    entries.sort(key=lambda row: row["_sort"], reverse=True)
    rows = entries[offset:offset+limit]
    for row in rows:
        row.pop("_sort")
    return {"deliveries": rows, "total": len(entries), "hasMore": offset + limit < len(entries)}

def logs(data: Path):
    # Only allowlisted structured lifecycle fields; no arbitrary text redaction
    # heuristic is trusted with credentials, transcript or personal paths.
    rows = []
    for filename, component in (("app.jsonl", "assistant"), ("desktop-events.jsonl", "assistant")):
        path = data / "logs" / filename
        try:
            with path.open("rb") as stream:
                stream.seek(max(0, path.stat().st_size - 64*1024))
                lines = stream.read(64*1024).decode("utf-8", errors="replace").splitlines()[-100:]
            for line in lines:
                try:
                    value = json.loads(line)
                    event = value.get("event", "")
                    if event not in {"ready", "stopped", "monitor-error", "runtime_start_failed", "service_error"}:
                        continue
                    at = value.get("at")
                    if not isinstance(at, (int, float)): continue
                    rows.append({"time": datetime.fromtimestamp(at).astimezone().strftime("%m-%d %H:%M:%S"),
                                 "component": component, "level": "ERROR" if "error" in event or "failed" in event else "INFO",
                                 "text": {"ready": "正式助手启动就绪", "stopped": "助手已停止",
                                          "monitor-error": "监听器异常，请运行诊断", "runtime_start_failed": "助手启动失败",
                                          "service_error": "本机服务异常"}[event]})
                except (ValueError, TypeError, AttributeError):
                    continue
        except OSError: pass
    return rows[-100:]
