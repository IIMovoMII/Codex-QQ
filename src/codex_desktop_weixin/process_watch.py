from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass


TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
MAX_PATH = 260
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
WM_CLOSE = 0x0010


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * MAX_PATH),
    ]


@dataclass(frozen=True)
class ProcessStatus:
    desktop_alive: bool
    app_server_alive: bool


def _snapshot() -> dict[int, tuple[int, str]]:
    if os.name != "nt":
        return {}
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_snapshot = kernel32.CreateToolhelp32Snapshot
    create_snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    create_snapshot.restype = wintypes.HANDLE
    first = kernel32.Process32FirstW
    first.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    first.restype = wintypes.BOOL
    next_process = kernel32.Process32NextW
    next_process.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    next_process.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    handle = create_snapshot(TH32CS_SNAPPROCESS, 0)
    if handle == INVALID_HANDLE_VALUE:
        return {}
    processes: dict[int, tuple[int, str]] = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if not first(handle, ctypes.byref(entry)):
            return {}
        while True:
            processes[int(entry.th32ProcessID)] = (int(entry.th32ParentProcessID), str(entry.szExeFile).lower())
            if not next_process(handle, ctypes.byref(entry)):
                break
    finally:
        close_handle(handle)
    return processes


def _has_chatgpt_ancestor(pid: int, processes: dict[int, tuple[int, str]]) -> bool:
    seen: set[int] = set()
    current = processes.get(pid, (0, ""))[0]
    for _ in range(8):
        if current in seen or current not in processes:
            return False
        seen.add(current)
        parent, name = processes[current]
        if name == "chatgpt.exe":
            return True
        current = parent
    return False


def get_codex_desktop_process_status() -> ProcessStatus:
    processes = _snapshot()
    roots = codex_desktop_root_pids(processes)
    desktop_alive = bool(roots)
    app_server_alive = any(
        name in ("codex.exe", "codex-code-mode-host.exe") and _has_any_ancestor(pid, roots, processes)
        for pid, (_, name) in processes.items()
    )
    return ProcessStatus(desktop_alive=desktop_alive, app_server_alive=app_server_alive)


def _process_image_path(pid: int) -> str:
    if os.name != "nt":
        return ""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    query_path = kernel32.QueryFullProcessImageNameW
    query_path.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    query_path.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    handle = open_process(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not query_path(handle, 0, buffer, ctypes.byref(size)):
            return ""
        return buffer.value
    finally:
        close_handle(handle)


def _has_any_ancestor(pid: int, ancestors: set[int], processes: dict[int, tuple[int, str]]) -> bool:
    current = processes.get(pid, (0, ""))[0]
    seen: set[int] = set()
    for _ in range(12):
        if current in ancestors:
            return True
        if current in seen or current not in processes:
            return False
        seen.add(current)
        current = processes[current][0]
    return False


def _is_codex_desktop_image(image: str) -> bool:
    """Recognize the packaged Desktop executable across versioned install paths."""
    normalized = image.replace("/", "\\").lower()
    if not normalized.endswith("\\app\\chatgpt.exe"):
        return False
    # Microsoft Store package directories currently use ``openai.codex_<ver>``;
    # accepting the unversioned/dash forms keeps upgrades from breaking startup
    # detection without matching an unrelated ChatGPT installation.
    return any(marker in normalized for marker in ("\\openai.codex_", "\\openai.codex\\", "\\openai.codex-"))


def codex_desktop_root_pids(processes: dict[int, tuple[int, str]] | None = None) -> set[int]:
    processes = processes or _snapshot()
    result: set[int] = set()
    for pid, (_, name) in processes.items():
        if name != "chatgpt.exe" or _has_chatgpt_ancestor(pid, processes):
            continue
        image = _process_image_path(pid).lower()
        if _is_codex_desktop_image(image):
            result.add(pid)
    return result


def request_codex_desktop_close() -> bool:
    """Ask Codex windows to close normally; never force-kill the process."""
    if os.name != "nt":
        return False
    pids = codex_desktop_root_pids()
    if not pids:
        return False
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    posted = False
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    enum_windows = user32.EnumWindows
    enum_windows.argtypes = [callback_type, wintypes.LPARAM]
    enum_windows.restype = wintypes.BOOL
    get_window_pid = user32.GetWindowThreadProcessId
    get_window_pid.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    get_window_pid.restype = wintypes.DWORD
    post_message = user32.PostMessageW
    post_message.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    post_message.restype = wintypes.BOOL

    @callback_type
    def visit(window: int, _: int) -> bool:
        nonlocal posted
        pid = wintypes.DWORD()
        get_window_pid(window, ctypes.byref(pid))
        if int(pid.value) in pids:
            posted = bool(post_message(window, WM_CLOSE, 0, 0)) or posted
        return True

    enum_windows(visit, 0)
    return posted
