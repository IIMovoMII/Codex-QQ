"""Independent app owner. Reuses the monitor in-process, never owns Codex."""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

from endpoint_hint import persist_native_endpoint
from launcher import (read_monitor_pid, read_cc_connect_pid, request_stop,
                      start_cc_connect_service, stop_cc_connect_service,
                      write_monitor_pid, clear_monitor_pid)
from monitor import DesktopMonitor, MonitorConfig
from notifier import DeliverySettings
from runtime_io import (acquire_file_lock, append_json_line, atomic_write_json,
                        ensure_runtime_dirs)
from runtime_mode import set_runtime_mode
from winwatch import DirectoryChange

APP_NAME = 'Codex QQ 助手'
RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
RUN_VALUE = 'CodexQQAssistant'
PROJECT = Path(__file__).resolve().parents[2]
APP_EXE = PROJECT / 'bin' / 'CodexQQ助手.exe'


def startup_command(executable: Path = APP_EXE) -> str:
    if not executable.is_absolute() or not executable.is_file():
        raise ValueError('app-executable-missing')
    return subprocess.list2cmdline([str(executable)])


def startup_enabled(executable: Path = APP_EXE, *, registry=None) -> bool:
    if registry is None:
        import winreg as registry
    try:
        with registry.OpenKey(registry.HKEY_CURRENT_USER, RUN_KEY, 0, registry.KEY_READ) as key:
            value, kind = registry.QueryValueEx(key, RUN_VALUE)
        return kind == registry.REG_SZ and value == startup_command(executable)
    except FileNotFoundError:
        return False


def set_startup(enabled: bool, executable: Path = APP_EXE, *, registry=None) -> None:
    if registry is None:
        import winreg as registry
    expected = startup_command(executable)
    with registry.CreateKeyEx(registry.HKEY_CURRENT_USER, RUN_KEY, 0,
                              registry.KEY_READ | registry.KEY_SET_VALUE) as key:
        try:
            previous, kind = registry.QueryValueEx(key, RUN_VALUE)
        except FileNotFoundError:
            previous, kind = None, None
        if previous is not None and (kind != registry.REG_SZ or previous != expected):
            raise ValueError('startup-entry-conflict-preserved')
        if enabled:
            registry.SetValueEx(key, RUN_VALUE, 0, registry.REG_SZ, expected)
        elif previous is not None:
            registry.DeleteValue(key, RUN_VALUE)


def launch_app(codex_home: Path, data_dir: Path, *, headless: bool = False,
               duration: float | None = None) -> dict:
    """Use the same Explorer broker as the manual entry, never a Codex child app."""
    if os.name != 'nt' or not APP_EXE.is_file():
        raise OSError('independent-app-launcher-missing')
    persist_native_endpoint(data_dir)
    child = subprocess.Popen([str(APP_EXE),
                              '--codex-home', str(codex_home.resolve()),
                              '--data-dir', str(data_dir.resolve()),
                              *(['--headless'] if headless else []),
                              *(['--duration', str(duration)] if duration is not None else [])],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True,
                             creationflags=subprocess.CREATE_NO_WINDOW)
    return {'app_launch_requested': True, 'launcher_pid': child.pid,
            'desktop_launched': False}


class ResidentApplication:
    def __init__(self, codex_home: Path, data_dir: Path) -> None:
        self.codex_home, self.data_dir = codex_home.resolve(), data_dir.resolve()
        self.paths = ensure_runtime_dirs(self.data_dir)
        self.lock = None
        self.worker = None
        self.monitor = None
        self.ready = threading.Event()
        self.ended = threading.Event()
        self.error_type = ''
        self.cc_status = '未启动'
        self.owns_runtime = False
        self.closed = False

    def log(self, event: str, **fields) -> None:
        append_json_line(self.paths['logs'] / 'app.jsonl',
                         {'at': time.time(), 'event': event, 'pid': os.getpid(), **fields})

    def start(self) -> bool:
        self.lock = acquire_file_lock(self.paths['state'] / 'app.lock')
        if self.lock is None:
            return False
        maintenance = acquire_file_lock(self.paths['state'] / 'autostart.lock')
        if maintenance is None:
            os.close(self.lock[0])
            self.lock = None
            raise RuntimeError('maintenance-busy')
        try:
            # Barrier BEFORE stopping legacy services. Hooks and queued helpers
            # must never resurrect the app, including after a failed start.
            set_runtime_mode(self.data_dir, 'independent')
            existing = read_monitor_pid(self.data_dir)
            if existing and existing != os.getpid():
                request_stop(self.data_dir, close_desktop=False)
                deadline = time.monotonic() + 15
                while read_monitor_pid(self.data_dir) and time.monotonic() < deadline:
                    time.sleep(0.1)
                if read_monitor_pid(self.data_dir):
                    raise RuntimeError('previous-monitor-did-not-stop')
            self.owns_runtime = True
            (self.data_dir / 'stop.request').unlink(missing_ok=True)
            # Capture once, then remove inherited Desktop-generation state.
            # Future native hints come from fresh Codex hooks, not our old env.
            persist_native_endpoint(self.data_dir)
            os.environ.pop('CODEX_APP_TOOLS_PIPE_PATH', None)
            delivery = DeliverySettings.load(self.data_dir / 'settings.json')
            # Also stop leftovers when delivery has been disabled. A crash
            # must not leave a gateway active behind a shadow-mode application.
            if read_cc_connect_pid(self.data_dir):
                stopped = stop_cc_connect_service(self.data_dir, timeout_seconds=2)
                if not stopped.get('cc_connect_stopped'):
                    raise RuntimeError('previous-gateway-did-not-stop')
            if delivery.mode == 'cc-connect':
                result = start_cc_connect_service(self.data_dir)
                self.cc_status = '就绪' if result.get('cc_connect_api_status') == 'ready' else '未连接，请查看日志或重新开启应用'
            else:
                self.cc_status = '影子模式（不发送消息）'
            write_monitor_pid(self.data_dir, ready=False)
            self.monitor = DesktopMonitor(MonitorConfig(
                codex_home=self.codex_home, data_dir=self.data_dir,
                exit_with_desktop=False, manage_cc_connect=False, low_power_idle=True))
            self.worker = threading.Thread(target=self._monitor, name='codex-monitor', daemon=True)
            self.worker.start()
            if not self.ready.wait(8) or self.ended.is_set():
                raise RuntimeError('monitor-not-ready')
            atomic_write_json(self.paths['state'] / 'app.json',
                              {'version': 1, 'pid': os.getpid(), 'ready': True, 'mode': 'independent'})
            self.log('ready', cc_status=self.cc_status)
            return True
        finally:
            os.close(maintenance[0])

    def _monitor(self) -> None:
        def ready() -> None:
            write_monitor_pid(self.data_dir, ready=True)
            self.ready.set()
        try:
            self.monitor.run(on_started=ready)
        except Exception as error:
            self.error_type = type(error).__name__
            self.log('monitor-error', error_type=self.error_type)
        finally:
            self.ended.set()
            self.ready.set()
            clear_monitor_pid(self.data_dir)

    def stop_requested(self) -> bool:
        return self.ended.is_set() or (self.data_dir / 'stop.request').exists()

    def stop(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            if self.monitor is not None:
                self.monitor.exit_requested = True
                self.monitor.events.put(DirectoryChange(self.paths['incoming'], None, 'app-stop'))
            if self.worker is not None:
                self.worker.join(timeout=5)
            if self.owns_runtime:
                # Only our identity-checked gateway; never Codex or arbitrary PIDs.
                maintenance = acquire_file_lock(self.paths['state'] / 'autostart.lock')
                deadline = time.monotonic() + 3
                while maintenance is None and time.monotonic() < deadline:
                    time.sleep(0.1)
                    maintenance = acquire_file_lock(self.paths['state'] / 'autostart.lock')
                if maintenance is not None:
                    try:
                        stopped = stop_cc_connect_service(self.data_dir, timeout_seconds=2)
                        self.log('gateway-stopped', confirmed=bool(stopped.get('cc_connect_stopped')))
                    finally:
                        os.close(maintenance[0])
                else:
                    self.log('gateway-stop-unconfirmed', reason='maintenance-busy')
                clear_monitor_pid(self.data_dir)
                atomic_write_json(self.paths['state'] / 'app.json',
                                  {'version': 1, 'pid': os.getpid(), 'ready': False, 'mode': 'independent'})
                self.log('stopped', monitor_thread_stopped=self.worker is None or not self.worker.is_alive())
        finally:
            if self.lock is not None:
                os.close(self.lock[0])
                self.lock = None

    def status_text(self) -> str:
        status = self.monitor.last_process_status if self.monitor else None
        desktop = '已运行' if status and status.desktop_alive else '未运行／尚未识别'
        gateway = '进程存在' if read_cc_connect_pid(self.data_dir) else '未运行'
        pending = sum(1 for _ in (self.data_dir / 'outbox' / 'pending').glob('*.json'))
        return (f'{APP_NAME}\n\n应用：独立运行（监听与托盘共用一个进程）\nCodex：{desktop}'
                f'\nQQ 网关：{gateway}；启动时状态：{self.cc_status}\n待投递提醒：{pending}'
                '\n\n右键托盘可切换“登录 Windows 后自启”。'
                '\n退出本应用不会关闭或暂停 Codex；提醒和 QQ 控制将停止。'
                '\nCodex 未启动时 QQ 新指令作废，不在启动后补执行。')


def run_app(codex_home: Path, data_dir: Path, *, headless: bool = False,
            duration: float | None = None) -> int:
    app = ResidentApplication(codex_home, data_dir)
    if not headless:
        from win_tray import Tray, show_existing, message
    try:
        if not app.start():
            if not headless:
                show_existing(app.data_dir)
            return 0
        if headless:
            start = time.monotonic()
            while not app.stop_requested() and (duration is None or time.monotonic() - start < duration):
                app.ended.wait(0.1)
        else:
            Tray(app).run(duration=duration)
        return 1 if app.error_type else 0
    except Exception as error:
        app.log('startup-error', error_type=type(error).__name__)
        if not headless:
            message('应用未能就绪，请查看本地日志。\n错误类型：' + type(error).__name__
                    + '\n未关闭 Codex，未清空通知或引用记录。', error=True)
        return 1
    finally:
        app.stop()
