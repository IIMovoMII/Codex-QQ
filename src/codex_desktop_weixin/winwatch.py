from __future__ import annotations

import ctypes
import os
import queue
import threading
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path


FILE_LIST_DIRECTORY = 0x0001
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_NOTIFY_CHANGE_FILE_NAME = 0x00000001
FILE_NOTIFY_CHANGE_DIR_NAME = 0x00000002
FILE_NOTIFY_CHANGE_SIZE = 0x00000008
FILE_NOTIFY_CHANGE_LAST_WRITE = 0x00000010
FILE_NOTIFY_CHANGE_CREATION = 0x00000040
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
ERROR_OPERATION_ABORTED = 995


_ACTIONS = {
    1: "added",
    2: "removed",
    3: "modified",
    4: "renamed_old",
    5: "renamed_new",
}


@dataclass(frozen=True)
class DirectoryChange:
    root: Path
    path: Path | None
    action: str


class WindowsDirectoryWatcher:
    def __init__(self, root: Path, output: queue.Queue[DirectoryChange], *, recursive: bool) -> None:
        if os.name != "nt":
            raise OSError("WindowsDirectoryWatcher requires Windows")
        self.root = root.resolve()
        self.output = output
        self.recursive = recursive
        self._handle: int | None = None
        self._thread: threading.Thread | None = None
        self._closed = threading.Event()

    def start(self) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        create_file.restype = wintypes.HANDLE
        handle = create_file(
            str(self.root),
            FILE_LIST_DIRECTORY,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS,
            None,
        )
        if handle == INVALID_HANDLE_VALUE:
            raise ctypes.WinError(ctypes.get_last_error())
        self._handle = handle
        self._thread = threading.Thread(target=self._run, name=f"watch:{self.root.name}", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        read_changes = kernel32.ReadDirectoryChangesW
        read_changes.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        read_changes.restype = wintypes.BOOL
        buffer = ctypes.create_string_buffer(64 * 1024)
        notify_filter = (
            FILE_NOTIFY_CHANGE_FILE_NAME
            | FILE_NOTIFY_CHANGE_DIR_NAME
            | FILE_NOTIFY_CHANGE_SIZE
            | FILE_NOTIFY_CHANGE_LAST_WRITE
            | FILE_NOTIFY_CHANGE_CREATION
        )
        while not self._closed.is_set() and self._handle is not None:
            returned = wintypes.DWORD()
            ok = read_changes(
                self._handle,
                buffer,
                len(buffer),
                self.recursive,
                notify_filter,
                ctypes.byref(returned),
                None,
                None,
            )
            if not ok:
                error = ctypes.get_last_error()
                if self._closed.is_set() or error == ERROR_OPERATION_ABORTED:
                    break
                self.output.put(DirectoryChange(self.root, None, f"watch_error:{error}"))
                break
            if returned.value == 0:
                self.output.put(DirectoryChange(self.root, None, "overflow"))
                continue
            offset = 0
            while offset < returned.value:
                next_offset = int.from_bytes(buffer.raw[offset : offset + 4], "little")
                action_number = int.from_bytes(buffer.raw[offset + 4 : offset + 8], "little")
                name_length = int.from_bytes(buffer.raw[offset + 8 : offset + 12], "little")
                name_bytes = buffer.raw[offset + 12 : offset + 12 + name_length]
                relative = name_bytes.decode("utf-16-le", errors="strict")
                self.output.put(DirectoryChange(self.root, self.root / relative, _ACTIONS.get(action_number, f"action:{action_number}")))
                if next_offset == 0:
                    break
                offset += next_offset

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        if self._handle is not None:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            try:
                kernel32.CancelIoEx(self._handle, None)
            finally:
                kernel32.CloseHandle(self._handle)
            self._handle = None
        if self._thread is not None:
            self._thread.join(timeout=2)

    def __enter__(self) -> "WindowsDirectoryWatcher":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

