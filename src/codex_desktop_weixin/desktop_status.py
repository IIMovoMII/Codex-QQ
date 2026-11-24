"""Read-only component evidence, distinct from process and local API health."""
from __future__ import annotations
from pathlib import Path

from control_contract import check_contract, supports_queue_command
from desktop_control import (DesktopClient, DesktopControlError, WindowsDesktopPipe,
                             pipe_server_identity)
from native_app_tools import NativeAppTools, desktop_identities
from process_watch import codex_desktop_root_pids


def codex_connection_status(app_tools: NativeAppTools) -> str:
    pids = codex_desktop_root_pids()
    if not pids:
        return 'offline'

    processes = desktop_identities(pids)
    if not processes:
        return 'unknown'

    try:
        with WindowsDesktopPipe(timeout_seconds=2) as pipe:
            identity, _, image = pipe_server_identity(pipe)
            if any(identity == process[0] for process in processes):
                DesktopClient(pipe).initialize()
                check_contract(image)
                return 'ready'
    except (OSError, TimeoutError, ValueError, DesktopControlError):
        pass

    try:
        (_, _, image), capabilities = app_tools.discover(processes)
    except DesktopControlError:
        return 'unknown'
    if 'navigate_to_codex_page' in capabilities and supports_queue_command(image):
        return 'ready'
    return 'limited'


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
