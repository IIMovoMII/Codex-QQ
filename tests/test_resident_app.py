from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

PACKAGE = Path(__file__).resolve().parents[1] / 'src/codex_desktop_weixin'
sys.path.insert(0, str(PACKAGE))
from resident_app import (ResidentApplication, set_startup, startup_enabled,
                          startup_command, RUN_VALUE, launch_app)
from runtime_mode import set_runtime_mode, runtime_mode
from hook_bridge import request_runtime_autostart, hook_main
from launcher import read_monitor_pid, request_stop
from monitor import DesktopMonitor, MonitorConfig
from process_watch import ProcessStatus
from notifier import DeliverySettings, save_delivery_settings


class ModeTests(unittest.TestCase):
    def test_missing_marker_after_migration_is_not_legacy_autostart_or_codex_launch(self):
        import io
        from cli import command_start
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'state').mkdir()
            (root / 'state/app.json').write_text('{"version":1,"mode":"independent"}', encoding='utf-8')
            self.assertEqual(runtime_mode(root), 'unknown')
            with patch('cli.launch_codex_desktop') as codex, patch('cli.sys.stdout', io.StringIO()):
                self.assertNotEqual(command_start(root / '.codex', root), 0)
                codex.assert_not_called()

    def test_missing_is_legacy_but_unknown_or_corrupt_never_autostarts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(runtime_mode(root), 'desktop')
            set_runtime_mode(root, 'independent')
            self.assertEqual(runtime_mode(root), 'independent')
            path = root / 'state/runtime-mode.json'
            for content in ('[]', 'broken', json.dumps({'version': 8, 'mode': 'desktop'}), 'x' * 2049):
                path.write_text(content, encoding='utf-8')
                self.assertEqual(runtime_mode(root), 'unknown')
                with patch('hook_bridge.subprocess.Popen') as spawn:
                    self.assertFalse(request_runtime_autostart('SessionStart', data_dir=root))
                    spawn.assert_not_called()
            with self.assertRaises(ValueError):
                set_runtime_mode(root, 'bad')

    def test_closed_app_hooks_and_queued_legacy_helper_cannot_resurrect(self):
        from cli import command_ensure_runtime
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            set_runtime_mode(root, 'independent')
            with patch('hook_bridge.subprocess.Popen') as spawn:
                for event in ('SessionStart', 'UserPromptSubmit', 'Stop', 'PermissionRequest'):
                    self.assertFalse(request_runtime_autostart(event, data_dir=root))
                spawn.assert_not_called()
            with patch('cli.wait_for_codex_desktop') as detect, patch('cli.start_background_monitor') as start:
                self.assertEqual(command_ensure_runtime(root / '.codex', root), 0)
                detect.assert_not_called()
                start.assert_not_called()

    def test_hook_still_collects_and_updates_endpoint_without_starting(self):
        import io
        endpoint = r'\\.\pipe\codex-browser-use-11111111-0000-0000-0000-000000000000'
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            set_runtime_mode(root, 'independent')
            with patch('hook_bridge.default_data_dir', return_value=root), \
                 patch.dict(os.environ, {'CODEX_APP_TOOLS_PIPE_PATH': endpoint}), \
                 patch('hook_bridge.sys.stdin', io.StringIO('{"hook_event_name":"Stop","session_id":"test"}')), \
                 patch('hook_bridge.sys.stdout', io.StringIO()) as output, patch('hook_bridge.subprocess.Popen') as spawn:
                self.assertEqual(hook_main(), 0)
                self.assertEqual(json.loads(output.getvalue()), {'continue': True})
                spawn.assert_not_called()
            self.assertEqual(len(list((root / 'spool/incoming').glob('*.json'))), 1)
            self.assertEqual(json.loads((root / 'state/native-app-tools.json').read_text(encoding='utf-8'))['path'], endpoint)


class Key:
    def __enter__(self): return self
    def __exit__(self, *args): pass


class Registry:
    HKEY_CURRENT_USER, KEY_READ, KEY_SET_VALUE, REG_SZ = 1, 2, 4, 1
    def __init__(self): self.values = {'OtherApp': ('untouched', 1)}
    def OpenKey(self, *args): return Key()
    CreateKeyEx = OpenKey
    def QueryValueEx(self, key, name):
        if name not in self.values: raise FileNotFoundError(name)
        return self.values[name]
    def SetValueEx(self, key, name, reserved, kind, value): self.values[name] = (value, kind)
    def DeleteValue(self, key, name): del self.values[name]


class StartupTests(unittest.TestCase):
    def test_opt_in_and_remove_only_our_exact_value_with_unicode_path(self):
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / '中文 空格 助手.exe'
            executable.touch()
            registry = Registry()
            self.assertFalse(startup_enabled(executable, registry=registry))
            set_startup(True, executable, registry=registry)
            self.assertTrue(startup_enabled(executable, registry=registry))
            self.assertEqual(registry.values[RUN_VALUE][0], startup_command(executable))
            set_startup(False, executable, registry=registry)
            self.assertEqual(registry.values, {'OtherApp': ('untouched', 1)})
            set_startup(False, executable, registry=registry)

    def test_conflicting_or_invalid_entry_is_preserved_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / 'app.exe'
            executable.touch()
            registry = Registry()
            registry.values[RUN_VALUE] = ('some other command', 1)
            original = dict(registry.values)
            for enabled in (True, False):
                with self.assertRaises(ValueError): set_startup(enabled, executable, registry=registry)
            self.assertEqual(registry.values, original)
            with self.assertRaises(ValueError): startup_command(Path('relative.exe'))


class FakeWatcher:
    def __init__(self, *args, **kwargs): pass
    def start(self): pass
    def close(self): pass


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = ResidentApplication(self.root / '.codex', self.root / 'data')
        self.addCleanup(self.app.stop)
        for target, value in (('monitor.WindowsDirectoryWatcher', FakeWatcher),
                              ('monitor.get_codex_desktop_process_status', lambda: ProcessStatus(False, False))):
            context = patch(target, value)
            context.start()
            self.addCleanup(context.stop)

    def test_starts_without_codex_no_extra_monitor_process_and_survives_absence(self):
        with patch('resident_app.start_cc_connect_service') as cc, patch('resident_app.set_startup') as startup:
            self.assertTrue(self.app.start())
            self.assertEqual(read_monitor_pid(self.app.data_dir, require_ready=True), os.getpid())
            self.assertFalse(self.app.monitor.config.exit_with_desktop)
            self.assertFalse(self.app.monitor.config.manage_cc_connect)
            self.assertTrue(self.app.monitor.config.low_power_idle)
            self.assertFalse(self.app.ended.is_set())
            cc.assert_not_called()  # Isolated shadow mode.
            startup.assert_not_called()

    def test_close_preserves_mode_and_data_does_not_close_or_pause_codex(self):
        self.assertTrue(self.app.start())
        record = self.app.data_dir / 'outbox/pending/keep.json'
        record.write_text('{"keep":true}', encoding='utf-8')
        with patch('resident_app.stop_cc_connect_service', return_value={'cc_connect_stopped': True}) as stop, \
             patch('launcher.request_codex_desktop_close') as desktop:
            self.app.stop()
            self.app.stop()
            stop.assert_called_once()
            desktop.assert_not_called()
        self.assertFalse(self.app.worker.is_alive())
        self.assertEqual(record.read_text(encoding='utf-8'), '{"keep":true}')
        self.assertEqual(runtime_mode(self.app.data_dir), 'independent')
        self.assertIsNone(read_monitor_pid(self.app.data_dir))

    def test_same_data_second_app_does_not_start_monitor_or_take_ownership(self):
        self.assertTrue(self.app.start())
        second = ResidentApplication(self.app.codex_home, self.app.data_dir)
        self.assertFalse(second.start())
        second.stop()
        self.assertFalse(self.app.ended.is_set())
        self.assertEqual(read_monitor_pid(self.app.data_dir), os.getpid())

    def test_orphan_gateway_rotates_before_start_not_reused_after_app_restart(self):
        save_delivery_settings(self.app.data_dir / 'settings.json', DeliverySettings(mode='cc-connect'))
        order = []
        with patch('resident_app.read_cc_connect_pid', return_value=100), \
             patch('resident_app.stop_cc_connect_service', side_effect=lambda *a, **k: order.append('stop') or {'cc_connect_stopped': True}), \
             patch('resident_app.start_cc_connect_service', side_effect=lambda *a: order.append('start') or {'cc_connect_api_status': 'ready'}):
            self.assertTrue(self.app.start())
            self.assertEqual(order, ['stop', 'start'])
            self.app.stop()

    def test_unconfirmed_gateway_stop_does_not_launch_duplicate(self):
        save_delivery_settings(self.app.data_dir / 'settings.json', DeliverySettings(mode='cc-connect'))
        with patch('resident_app.read_cc_connect_pid', return_value=100), \
             patch('resident_app.stop_cc_connect_service', return_value={'cc_connect_stopped': False}), \
             patch('resident_app.start_cc_connect_service') as start:
            with self.assertRaises(RuntimeError): self.app.start()
            start.assert_not_called()
            self.app.stop()
        self.assertEqual(runtime_mode(self.app.data_dir), 'independent')

    def test_low_power_only_without_codex_active_tasks_or_delivery_backlog(self):
        monitor = DesktopMonitor(MonitorConfig(codex_home=self.root / '.codex', data_dir=self.root / 'data', low_power_idle=True))
        monitor.last_process_status = ProcessStatus(False, False)
        self.assertTrue(monitor._standby())
        monitor.last_process_status = ProcessStatus(True, True)
        self.assertFalse(monitor._standby())
        monitor.last_process_status = ProcessStatus(False, False)
        (monitor.notifier.pending / 'pending.json').write_text('{}', encoding='utf-8')
        self.assertFalse(monitor._standby())

    def test_launch_uses_shared_explorer_broker_and_no_codex_launch(self):
        with patch('resident_app.subprocess.Popen') as spawn:
            spawn.return_value.pid = 42
            result = launch_app(self.app.codex_home, self.app.data_dir)
        args, kwargs = spawn.call_args
        self.assertEqual(Path(args[0][0]).name, 'CodexQQ助手.exe')
        self.assertIn('--codex-home', args[0])
        self.assertIn('--data-dir', args[0])
        self.assertNotIn('shell', kwargs)
        self.assertFalse(result['desktop_launched'])


@unittest.skipUnless(os.name == 'nt', 'Windows integration')
class WindowsIntegrationTests(unittest.TestCase):
    def test_tray_waits_for_notification_area_and_does_not_veto_logout(self):
        from win_tray import Tray, WM_QUERYENDSESSION, WM_ENDSESSION
        class App:
            error_type = ''
            def __init__(self, root): self.data_dir = root; self.events = []
            def log(self, event, **fields): self.events.append(event)
            def stop_requested(self): return False
        with tempfile.TemporaryDirectory() as temp:
            app = App(Path(temp))
            tray = Tray(app)
            original = tray.shell.Shell_NotifyIconW
            calls = []
            def icon(operation, data):
                if operation == 0:
                    calls.append(operation)
                    if len(calls) == 1: return False
                return original(operation, data)
            tray.shell.Shell_NotifyIconW = icon
            self.assertEqual(tray._callback(None, WM_QUERYENDSESSION, 0, 0), 1)
            self.assertEqual(tray._callback(None, WM_ENDSESSION, 0, 0), 0)
            tray.run(duration=0.5)
            self.assertGreaterEqual(len(calls), 2)
            self.assertIn('tray-ready', app.events)
            self.assertEqual(app.error_type, '')

    def test_native_registry_roundtrip_in_test_key_never_changes_real_startup(self):
        import winreg
        import uuid
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / '中文 空格.exe'
            executable.touch()
            test_key = r'Software\CodexQQAssistantTests' + '\\' + uuid.uuid4().hex
            try:
                with patch('resident_app.RUN_KEY', test_key):
                    set_startup(True, executable)
                    self.assertTrue(startup_enabled(executable))
                    set_startup(False, executable)
                    self.assertFalse(startup_enabled(executable))
            finally:
                # Exact isolated key, no recursive removal and no Run changes.
                try: winreg.DeleteKey(winreg.HKEY_CURRENT_USER, test_key)
                except FileNotFoundError: pass

    def test_compiled_windowed_launcher_preserves_unicode_and_space_arguments(self):
        executable = PACKAGE.parents[1] / 'bin/CodexQQ助手.exe'
        if not executable.is_file(): self.skipTest('local launcher not built')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / '启动 中文 & 空格'
            result = subprocess.run([str(executable), '--codex-home', str(root / '.codex'),
                                     '--data-dir', str(root / 'data'), '--headless', '--duration', '0.5'],
                                    capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0)
            log_path = root / 'data/logs/app.jsonl'
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if log_path.exists():
                    rows = [json.loads(x) for x in log_path.read_text(encoding='utf-8').splitlines()]
                    if rows and rows[-1]['event'] == 'stopped': break
                time.sleep(0.1)
            self.assertTrue(log_path.exists())
            self.assertEqual(rows[-1]['event'], 'stopped')
            self.assertTrue(rows[-1]['monitor_thread_stopped'])
            self.assertIsNone(read_monitor_pid(root / 'data'))
            # Explorer-brokered startup is independent of this test process;
            # wait for the actual process, not merely its final log record.
            import ctypes
            import ctypes.wintypes as wintypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            handle = kernel.OpenProcess(0x100000, False, rows[-1]['pid'])
            if handle:
                try:
                    self.assertEqual(kernel.WaitForSingleObject(handle, 8000), 0)
                finally:
                    kernel.CloseHandle(handle)

    def test_isolated_headless_app_duplicate_and_explicit_stop(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / '中文 隔离目录'
            home, data = root / '.codex', root / 'data'
            command = [sys.executable, '-X', 'utf8', str(PACKAGE / 'cli.py'), 'app',
                       '--brokered', '--codex-home', str(home), '--data-dir', str(data), '--headless']
            process = subprocess.Popen(command + ['--duration', '15'], stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                deadline = time.monotonic() + 8
                while not read_monitor_pid(data, require_ready=True) and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.1)
                self.assertEqual(read_monitor_pid(data, require_ready=True), process.pid)
                duplicate = subprocess.run(command + ['--duration', '0'], capture_output=True, timeout=5)
                self.assertEqual(duplicate.returncode, 0, duplicate.stderr)
                self.assertEqual(read_monitor_pid(data), process.pid)
                self.assertEqual(runtime_mode(data), 'independent')
                request_stop(data, close_desktop=False)
                self.assertEqual(process.wait(timeout=8), 0)
                self.assertIsNone(read_monitor_pid(data))
            finally:
                if process.poll() is None:
                    request_stop(data, close_desktop=False)
                    try: process.wait(timeout=8)
                    except subprocess.TimeoutExpired: process.kill(); process.wait()

    def test_real_native_tray_add_remove_in_isolated_shadow_app(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / '中文托盘'
            result = subprocess.run([sys.executable, '-X', 'utf8', str(PACKAGE / 'cli.py'), 'app',
                                     '--brokered',
                                     '--codex-home', str(root / '.codex'), '--data-dir', str(root / 'data'),
                                     '--duration', '0.4'], capture_output=True, timeout=12)
            self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
            log = [json.loads(x) for x in (root / 'data/logs/app.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertIn('tray-ready', [x['event'] for x in log])
            self.assertTrue(log[-1]['monitor_thread_stopped'])
            self.assertIsNone(read_monitor_pid(root / 'data'))


if __name__ == '__main__':
    unittest.main()
