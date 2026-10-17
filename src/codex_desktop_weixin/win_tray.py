"""Tiny Win32 notification-area UI. No browser, UI toolkit or polling thread."""
from __future__ import annotations

import ctypes
import hashlib
import os
import time
from ctypes import wintypes as w

APP_NAME = 'Codex QQ 助手'
WM_APP = 0x8000
WM_CALLBACK = WM_APP + 1
WM_SHOW = WM_APP + 2
WM_CLOSE, WM_DESTROY, WM_TIMER = 0x10, 2, 0x113
WM_QUERYENDSESSION, WM_ENDSESSION = 0x11, 0x16
WM_LBUTTONDBLCLK, WM_RBUTTONUP, WM_CONTEXTMENU = 0x203, 0x205, 0x7B
STATUS, STARTUP, LOGS, EXIT = 101, 102, 103, 104
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)


class WNDCLASS(ctypes.Structure):
    _fields_ = [('style', w.UINT), ('lpfnWndProc', WNDPROC), ('cbClsExtra', ctypes.c_int),
                ('cbWndExtra', ctypes.c_int), ('hInstance', w.HINSTANCE), ('hIcon', w.HICON),
                ('hCursor', w.HANDLE), ('hbrBackground', w.HANDLE),
                ('lpszMenuName', w.LPCWSTR), ('lpszClassName', w.LPCWSTR)]


class NOTIFYICONDATA(ctypes.Structure):
    _fields_ = [('cbSize', w.DWORD), ('hWnd', w.HWND), ('uID', w.UINT), ('uFlags', w.UINT),
                ('uCallbackMessage', w.UINT), ('hIcon', w.HICON), ('szTip', w.WCHAR * 128),
                ('dwState', w.DWORD), ('dwStateMask', w.DWORD), ('szInfo', w.WCHAR * 256),
                ('uTimeoutOrVersion', w.UINT), ('szInfoTitle', w.WCHAR * 64),
                ('dwInfoFlags', w.DWORD), ('guidItem', ctypes.c_byte * 16), ('hBalloonIcon', w.HICON)]


def _apis():
    user, kernel, shell = (ctypes.WinDLL(x, use_last_error=True) for x in ('user32', 'kernel32', 'shell32'))
    declarations = {
        'RegisterClassW': ([ctypes.POINTER(WNDCLASS)], w.WORD),
        'UnregisterClassW': ([w.LPCWSTR, w.HINSTANCE], w.BOOL),
        'CreateWindowExW': ([w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int,
                            ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU,
                            w.HINSTANCE, ctypes.c_void_p], w.HWND),
        'DefWindowProcW': ([w.HWND, w.UINT, w.WPARAM, w.LPARAM], LRESULT),
        'DestroyWindow': ([w.HWND], w.BOOL),
        'GetMessageW': ([ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT], w.BOOL),
        'TranslateMessage': ([ctypes.POINTER(w.MSG)], w.BOOL),
        'DispatchMessageW': ([ctypes.POINTER(w.MSG)], LRESULT),
        'PostQuitMessage': ([ctypes.c_int], None),
        'PostMessageW': ([w.HWND, w.UINT, w.WPARAM, w.LPARAM], w.BOOL),
        'LoadIconW': ([w.HINSTANCE, w.LPCWSTR], w.HICON),
        'SetTimer': ([w.HWND, ctypes.c_size_t, w.UINT, ctypes.c_void_p], ctypes.c_size_t),
        'KillTimer': ([w.HWND, ctypes.c_size_t], w.BOOL),
        'CreatePopupMenu': ([], w.HMENU),
        'AppendMenuW': ([w.HMENU, w.UINT, ctypes.c_size_t, w.LPCWSTR], w.BOOL),
        'DestroyMenu': ([w.HMENU], w.BOOL),
        'GetCursorPos': ([ctypes.POINTER(w.POINT)], w.BOOL),
        'SetForegroundWindow': ([w.HWND], w.BOOL),
        'TrackPopupMenu': ([w.HMENU, w.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                           w.HWND, ctypes.c_void_p], w.UINT),
        'RegisterWindowMessageW': ([w.LPCWSTR], w.UINT),
        'FindWindowW': ([w.LPCWSTR, w.LPCWSTR], w.HWND),
        'MessageBoxW': ([w.HWND, w.LPCWSTR, w.LPCWSTR, w.UINT], ctypes.c_int),
    }
    for name, (args, result) in declarations.items():
        function = getattr(user, name)
        function.argtypes, function.restype = args, result
    kernel.GetModuleHandleW.argtypes, kernel.GetModuleHandleW.restype = [w.LPCWSTR], w.HMODULE
    shell.Shell_NotifyIconW.argtypes = [w.DWORD, ctypes.POINTER(NOTIFYICONDATA)]
    shell.Shell_NotifyIconW.restype = w.BOOL
    return user, kernel, shell


def window_class(data_dir) -> str:
    key = os.path.normcase(str(data_dir.resolve())).encode('utf-8')
    return 'CodexQQAssistant_' + hashlib.sha256(key).hexdigest()[:16]


def message(text: str, *, error: bool = False, hwnd=None) -> None:
    user, _, _ = _apis()
    user.MessageBoxW(hwnd, text, APP_NAME, 0x10 if error else 0x40)


def show_existing(data_dir) -> bool:
    user, _, _ = _apis()
    window = user.FindWindowW(window_class(data_dir), None)
    return bool(window and user.PostMessageW(window, WM_SHOW, 0, 0))


class Tray:
    def __init__(self, app) -> None:
        self.app = app
        self.user, self.kernel, self.shell = _apis()
        self.hwnd = None
        self.class_name = window_class(app.data_dir)
        self.instance = self.kernel.GetModuleHandleW(None)
        self.callback = WNDPROC(self._callback)  # Keep alive until class teardown.
        self.icon = NOTIFYICONDATA()
        self.icon.cbSize = ctypes.sizeof(self.icon)
        self.icon.uID = 1
        self.icon.uFlags = 1 | 2 | 4
        self.icon.uCallbackMessage = WM_CALLBACK
        self.icon.hIcon = self.user.LoadIconW(None, ctypes.cast(ctypes.c_void_p(32512), w.LPCWSTR))
        self.icon.szTip = APP_NAME
        self.taskbar_created = self.user.RegisterWindowMessageW('TaskbarCreated')
        self.deadline = None
        self.error_shown = False
        self.registered = False
        self.icon_deadline = None

    def _add_icon(self) -> bool:
        self.registered = bool(self.shell.Shell_NotifyIconW(0, ctypes.byref(self.icon)))
        if self.registered:
            self.app.log('tray-ready')
            self.user.SetTimer(self.hwnd, 1, 200 if self.deadline is not None else 5000, None)
        return self.registered

    def _callback(self, hwnd, event, first, second):
        try:
            if event == WM_CALLBACK:
                if second in (WM_RBUTTONUP, WM_CONTEXTMENU):
                    self._menu()
                elif second == WM_LBUTTONDBLCLK:
                    message(self.app.status_text(), hwnd=hwnd)
                return 0
            if event == WM_SHOW:
                message(self.app.status_text(), hwnd=hwnd)
                return 0
            if event == WM_QUERYENDSESSION:
                return 1  # Never veto Windows logout/shutdown.
            if event == WM_ENDSESSION:
                if first:
                    self.user.DestroyWindow(hwnd)
                return 0
            if event == WM_CLOSE:
                self.user.DestroyWindow(hwnd)
                return 0
            if event == WM_DESTROY:
                self.shell.Shell_NotifyIconW(2, ctypes.byref(self.icon))
                self.user.PostQuitMessage(0)
                return 0
            if event == WM_TIMER:
                if self.app.stop_requested() or self.deadline is not None and time.monotonic() >= self.deadline:
                    if self.app.error_type and not self.error_shown:
                        self.error_shown = True
                        message('监听线程已停止：' + self.app.error_type + '\n请查看日志后重新开启应用。', error=True, hwnd=hwnd)
                    self.user.DestroyWindow(hwnd)
                    return 0
                if not self.registered and not self._add_icon():
                    if self.icon_deadline is not None and time.monotonic() >= self.icon_deadline:
                        self.app.error_type = 'TrayUnavailable'
                        self.app.log('tray-unavailable')
                        self.user.DestroyWindow(hwnd)
                return 0
            if event == self.taskbar_created:
                self.registered = False
                self.icon_deadline = time.monotonic() + 30
                if not self._add_icon():
                    self.user.SetTimer(hwnd, 1, 1000, None)
                return 0
        except Exception as error:
            self.app.log('tray-error', error_type=type(error).__name__)
        return self.user.DefWindowProcW(hwnd, event, first, second)

    def _menu(self) -> None:
        from resident_app import startup_enabled, set_startup
        menu = self.user.CreatePopupMenu()
        if not menu:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            checked = startup_enabled()
            for command, label in ((STATUS, '查看状态'), (STARTUP, '登录 Windows 后自启'),
                                    (LOGS, '打开日志文件夹'), (EXIT, '退出应用（不关闭 Codex）')):
                if not self.user.AppendMenuW(menu, 8 if command == STARTUP and checked else 0, command, label):
                    raise ctypes.WinError(ctypes.get_last_error())
            position = w.POINT()
            self.user.GetCursorPos(ctypes.byref(position))
            self.user.SetForegroundWindow(self.hwnd)
            selected = self.user.TrackPopupMenu(menu, 0x100 | 2, position.x, position.y, 0, self.hwnd, None)
            self.user.PostMessageW(self.hwnd, 0, 0, 0)
            if selected == STATUS:
                message(self.app.status_text(), hwnd=self.hwnd)
            elif selected == STARTUP:
                try:
                    set_startup(not checked)
                    message('已开启登录 Windows 后自启。' if not checked else '已关闭登录 Windows 后自启。', hwnd=self.hwnd)
                except (OSError, ValueError):
                    message('自启设置未能完成。已有不匹配条目不会被覆盖，请查看日志。', error=True, hwnd=self.hwnd)
            elif selected == LOGS:
                os.startfile(self.app.paths['logs'])
            elif selected == EXIT:
                self.user.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
        finally:
            self.user.DestroyMenu(menu)

    def run(self, *, duration: float | None = None) -> None:
        definition = WNDCLASS()
        definition.lpfnWndProc = self.callback
        definition.hInstance = self.instance
        definition.lpszClassName = self.class_name
        if not self.user.RegisterClassW(ctypes.byref(definition)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            self.hwnd = self.user.CreateWindowExW(0, self.class_name, APP_NAME, 0,
                                                  0, 0, 0, 0, None, None, self.instance, None)
            if not self.hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            self.icon.hWnd = self.hwnd
            self.deadline = time.monotonic() + duration if duration is not None else None
            self.icon_deadline = time.monotonic() + 30
            self._add_icon()
            # Login startup can precede Explorer's notification area. Retry
            # only icon registration, never service or model operations.
            interval = 200 if duration is not None else 5000 if self.registered else 1000
            if not self.user.SetTimer(self.hwnd, 1, interval, None):
                raise ctypes.WinError(ctypes.get_last_error())
            event = w.MSG()
            while True:
                result = self.user.GetMessageW(ctypes.byref(event), None, 0, 0)
                if result == 0:
                    break
                if result == -1:
                    raise ctypes.WinError(ctypes.get_last_error())
                self.user.TranslateMessage(ctypes.byref(event))
                self.user.DispatchMessageW(ctypes.byref(event))
        finally:
            if self.hwnd:
                self.user.KillTimer(self.hwnd, 1)
                self.shell.Shell_NotifyIconW(2, ctypes.byref(self.icon))
                self.user.DestroyWindow(self.hwnd)
            self.user.UnregisterClassW(self.class_name, self.instance)
