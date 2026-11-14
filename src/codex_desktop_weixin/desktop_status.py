"""Read-only component evidence, distinct from process and local API health."""
from __future__ import annotations
from pathlib import Path
import time

def codex_connection_status():
    from process_watch import get_codex_desktop_process_status
    from control_backend import NativeBackend
    from desktop_control import WindowsDesktopPipe
    if not get_codex_desktop_process_status().desktop_alive:
        return "offline"
    try:
        NativeBackend(pipe_factory=lambda: WindowsDesktopPipe(timeout_seconds=2)).probe()
        return "ready"
    except (OSError, RuntimeError, ValueError, TimeoutError):
        return "unknown"

def qq_connection_status(data: Path):
    from launcher import read_cc_connect_pid, _read_service_state
    if read_cc_connect_pid(data) is None:
        return "offline"
    state = _read_service_state(data)
    try:
        offset = int(state["log_offset"])
        return qq_log_status(data / 'logs/cc-connect.log', offset)
    except (OSError, ValueError, KeyError):
        return "unknown"


def qq_log_status(path: Path, offset: int):
    try:
        size = path.stat().st_size
        if size < offset:
            return "unknown"
        with path.open("rb") as stream:
            stream.seek(max(offset, size - 1024*1024))
            lines = stream.read(1024*1024).decode("utf-8", errors="replace").splitlines()
    except (OSError, ValueError, KeyError):
        return "unknown"
    result = "unknown"
    for line in lines:
        lower = line.lower()
        if any(f'msg="qqbot: {event}"' in lower for event in ('gateway ready', 'session resumed successfully')) and "level=info" in lower:
            result = "ready"
        elif "qqbot:" in lower and any(word in lower for word in ("disconnected", "connection closed", "reconnect", "level=error")):
            result = "offline"
    return result
