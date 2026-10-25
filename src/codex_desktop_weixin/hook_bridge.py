from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from models import Signal, SignalKind
from runtime_io import append_json_line, atomic_write_json, default_data_dir, ensure_runtime_dirs, rotate_log_file


HOOK_FORMAT_VERSION = 1
MAX_HOOK_INPUT_BYTES = 1024 * 1024
AUTOSTART_WAIT_SECONDS = 30.0
_RUNTIME_AUTOSTART_EVENTS = {
    "SessionStart",
    "UserPromptSubmit",
    "PermissionRequest",
    "PreToolUse",
    "PostToolUse",
    "Stop",
}


def _validate_hook_input(raw_input: str) -> None:
    # Check characters first so the UTF-8 size check never makes a second
    # unbounded copy of an oversized input supplied by an in-process caller.
    if len(raw_input) > MAX_HOOK_INPUT_BYTES or len(raw_input.encode("utf-8")) > MAX_HOOK_INPUT_BYTES:
        raise ValueError("Hook input exceeds size limit")


def _read_hook_input() -> str:
    binary = getattr(sys.stdin, "buffer", None)
    if binary is not None:
        raw = binary.read(MAX_HOOK_INPUT_BYTES + 1)
        if len(raw) > MAX_HOOK_INPUT_BYTES:
            raise ValueError("Hook input exceeds size limit")
        return raw.decode("utf-8")
    # StringIO and embedded callers may expose only a text stream. Bound that
    # read too, then enforce the same byte limit before parsing or writing.
    raw = sys.stdin.read(MAX_HOOK_INPUT_BYTES + 1)
    _validate_hook_input(raw)
    return raw


def _safe_text(value: Any, *, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value[:limit]


def sanitize_hook_payload(payload: dict[str, Any]) -> dict[str, Any]:
    event_name = _safe_text(payload.get("hook_event_name"), limit=80)
    cwd = _safe_text(payload.get("cwd"), limit=2048)
    project_name = Path(cwd).name if cwd else ""
    sanitized: dict[str, Any] = {
        "format_version": HOOK_FORMAT_VERSION,
        "event_id": uuid.uuid4().hex,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "hook_event_name": event_name,
        "session_id": _safe_text(payload.get("session_id"), limit=160),
        "turn_id": _safe_text(payload.get("turn_id"), limit=160),
        "transcript_path": _safe_text(payload.get("transcript_path"), limit=32768),
        "project_name": project_name[:200],
        "model": _safe_text(payload.get("model"), limit=200),
        "permission_mode": _safe_text(payload.get("permission_mode"), limit=80),
    }
    if event_name in ("PreToolUse", "PostToolUse", "PermissionRequest"):
        sanitized["tool_name"] = _safe_text(payload.get("tool_name"), limit=300)
    if event_name == "Stop":
        sanitized["last_assistant_message"] = _safe_text(payload.get("last_assistant_message"), limit=100_000)
        sanitized["stop_hook_active"] = bool(payload.get("stop_hook_active"))
    if event_name == "SessionStart":
        sanitized["source"] = _safe_text(payload.get("source"), limit=80)
    if event_name == "SessionEnd":
        sanitized["reason"] = _safe_text(payload.get("reason"), limit=120)
    return sanitized


def collect_hook_event(raw_input: str, *, data_dir: Path | None = None) -> Path:
    _validate_hook_input(raw_input)
    payload = json.loads(raw_input)
    if not isinstance(payload, dict):
        raise ValueError("Hook input must be a JSON object")
    event = sanitize_hook_payload(payload)
    paths = ensure_runtime_dirs(data_dir or default_data_dir())
    filename = f"{time.time_ns():020d}-{event['event_id']}.json"
    destination = paths["incoming"] / filename
    atomic_write_json(destination, event)
    return destination


def should_autostart_runtime(
    event_name: str,
    *,
    monitor_running: bool,
    cc_connect_running: bool,
    desktop_alive: bool,
) -> bool:
    return (
        event_name in _RUNTIME_AUTOSTART_EVENTS
        and desktop_alive
        and not (monitor_running and cc_connect_running)
    )


def request_runtime_autostart(
    event_name: str,
    *,
    data_dir: Path | None = None,
    transcript_path: str | None = None,
) -> bool:
    if event_name not in _RUNTIME_AUTOSTART_EVENTS:
        return False
    root = data_dir or default_data_dir()
    try:
        from runtime_mode import runtime_mode
        if runtime_mode(root) != 'desktop':
            return False
        from launcher import read_cc_connect_pid, read_monitor_pid
        from notifier import DeliverySettings
        delivery_mode = DeliverySettings.load(root / 'settings.json').mode

        # Desktop/session classification and retries belong to the helper, not
        # the synchronous hook that can race Desktop's first process snapshot.
        if not should_autostart_runtime(
            event_name,
            monitor_running=read_monitor_pid(root, require_ready=True) is not None,
            # Shadow delivery has no gateway to start.  Requiring a CC
            # Connect PID here used to spawn one helper for every hook event
            # even though the monitor was already healthy.
            cc_connect_running=(delivery_mode != 'cc-connect'
                                or read_cc_connect_pid(root) is not None),
            desktop_alive=True,
        ):
            return False
        cli_path = Path(__file__).with_name("cli.py").resolve()
        codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).resolve()
        flags = 0
        if os.name == "nt":
            flags = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            )
        command = [
            sys.executable,
            "-X",
            "utf8",
            str(cli_path),
            "ensure-runtime",
            "--codex-home",
            str(codex_home),
            "--data-dir",
            str(root.resolve()),
            "--wait-seconds",
            str(AUTOSTART_WAIT_SECONDS),
            "--trigger-event",
            event_name,
            "--transcript-path",
            str(transcript_path or ""),
        ]
        log_path = ensure_runtime_dirs(root)["logs"] / "autostart-console.log"
        rotate_log_file(log_path)
        with log_path.open("ab") as log:
            subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                creationflags=flags,
                close_fds=True,
            )
        return True
    except (OSError, ValueError) as error:
        try:
            append_json_line(
                ensure_runtime_dirs(root)["logs"] / "autostart.jsonl",
                {
                    "at": time.time(),
                    "status": "hook_launch_failed",
                    "trigger_event": event_name,
                    "error_type": type(error).__name__,
                    "error_code": getattr(error, "winerror", None) or getattr(error, "errno", None),
                },
            )
        except OSError:
            pass
        return False


def _parse_observed_at(value: Any) -> datetime:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def hook_event_to_signals(event: dict[str, Any]) -> list[Signal]:
    name = str(event.get("hook_event_name") or "")
    observed_at = _parse_observed_at(event.get("observed_at"))
    common = {
        "observed_at": observed_at,
        "session_id": str(event.get("session_id") or "") or None,
        "turn_id": str(event.get("turn_id") or "") or None,
        "transcript_path": str(event.get("transcript_path") or "") or None,
        "source": "hook",
    }
    metadata = {"project_name": event.get("project_name"), "model": event.get("model")}
    if name == "SessionStart":
        return [Signal(SignalKind.SESSION_STARTED, data={**metadata, "source": event.get("source")}, **common)]
    if name == "UserPromptSubmit":
        return [Signal(SignalKind.TURN_STARTED, data=metadata, **common)]
    if name == "PermissionRequest":
        return [Signal(SignalKind.PERMISSION_REQUESTED, data={**metadata, "tool_name": event.get("tool_name")}, **common)]
    if name == "PreToolUse" and event.get("tool_name") == "request_user_input":
        return [Signal(SignalKind.USER_INPUT_REQUESTED, data=metadata, **common)]
    if name == "PostToolUse" and event.get("tool_name") == "request_user_input":
        return [Signal(SignalKind.USER_INPUT_RESOLVED, data=metadata, **common)]
    if name == "Stop":
        signals: list[Signal] = []
        message = event.get("last_assistant_message")
        if isinstance(message, str) and message:
            signals.append(Signal(SignalKind.ASSISTANT_MESSAGE, data={**metadata, "message": message}, **common))
        signals.append(Signal(SignalKind.STOP_OBSERVED, data={**metadata, "stop_hook_active": event.get("stop_hook_active")}, **common))
        return signals
    if name == "SessionEnd":
        return [Signal(SignalKind.SESSION_ENDED, data={**metadata, "reason": event.get("reason")}, **common)]
    return [Signal(SignalKind.ACTIVITY, data={**metadata, "hook_event_name": name}, **common)]


def hook_main() -> int:
    # Until the event is decoded it may be Stop. A failed/oversized read must
    # still produce its harmless JSON response and never escape this guard.
    is_stop = True
    try:
        raw = _read_hook_input()
        payload = json.loads(raw)
        is_stop = isinstance(payload, dict) and payload.get("hook_event_name") == "Stop"
        collect_hook_event(raw)
        if isinstance(payload, dict):
            from endpoint_hint import persist_native_endpoint
            persist_native_endpoint(default_data_dir())
            request_runtime_autostart(
                str(payload.get("hook_event_name") or ""),
                transcript_path=str(payload.get("transcript_path") or ""),
            )
    except Exception as error:
        # A notification helper must never break the Codex turn.  Keep a small
        # local diagnostic so a failed hook launch is explainable later.
        try:
            append_json_line(
                ensure_runtime_dirs(default_data_dir())["logs"] / "autostart.jsonl",
                {
                    "at": time.time(),
                    "status": "hook_failed",
                    "error_type": type(error).__name__,
                },
            )
        except OSError:
            pass
    if is_stop:
        # Stop requires valid JSON on stdout. This explicitly leaves the turn
        # unchanged; the collector is observational only.
        sys.stdout.write('{"continue":true}\n')
    return 0


if __name__ == "__main__":
    raise SystemExit(hook_main())
