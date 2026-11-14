from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "src/codex_desktop_weixin"
sys.path.insert(0, str(PACKAGE))

from desktop_bridge import (
    DesktopBridge,
    MAX_FRAME_BYTES,
    serve,
    main,
    validate_request,
)
from resident_app import startup_command


class FakeApp:
    starts = 0
    stops = 0

    def __init__(self, _codex_home: Path, _data_dir: Path) -> None:
        self.worker = None

    def start(self) -> bool:
        type(self).starts += 1
        self.worker = object()
        return True

    def stop(self) -> None:
        type(self).stops += 1


class ProtocolOutputTests(unittest.TestCase):
    def test_resident_worker_event_cannot_poison_next_native_reply(self):
        class NoisyBridge:
            def handle(self, request):
                # Match Monitor._emit's worker-thread console event shape.
                worker = threading.Thread(target=lambda: print('{"state":"completed"}', flush=True))
                worker.start()
                worker.join()
                return {'requestId': request['requestId'], 'operationId': request['requestId'],
                        'status': 'confirmed', 'preview': False}

            def close(self):
                pass

        source = io.BytesIO(b'{"requestId":"first"}\n{"requestId":"after-completion"}\n')
        wire = io.BytesIO()
        console = io.StringIO()
        output = io.TextIOWrapper(wire, encoding='utf-8')
        try:
            with patch('desktop_bridge.DesktopBridge', return_value=NoisyBridge()), \
                    patch('sys.stdin', SimpleNamespace(buffer=source)), \
                    patch('sys.stdout', output), patch('sys.stderr', console):
                self.assertEqual(main(['--host-handshake', '--startup-executable', str(Path(__file__).resolve())]), 0)
            output.flush()
            replies = [json.loads(line) for line in wire.getvalue().splitlines()]
            self.assertEqual([reply['requestId'] for reply in replies], ['first', 'after-completion'])
            self.assertEqual(console.getvalue().count('"state":"completed"'), 2)
        finally:
            output.detach()


class FakeRegistryKey:
    def __init__(self, registry, path):
        self.registry = registry
        self.path = path

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class FakeRegistry:
    HKEY_CURRENT_USER = object()
    KEY_READ = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1
    REG_BINARY = 3

    def __init__(self):
        self.values = {}

    def OpenKey(self, _root, path, *_args):
        if path not in self.values:
            raise FileNotFoundError(path)
        return FakeRegistryKey(self, path)

    def CreateKeyEx(self, _root, path, *_args):
        self.values.setdefault(path, {})
        return FakeRegistryKey(self, path)

    def QueryValueEx(self, key, name):
        if name not in self.values[key.path]:
            raise FileNotFoundError(name)
        return self.values[key.path][name]

    def SetValueEx(self, key, name, _reserved, kind, value):
        self.values[key.path][name] = (value, kind)

    def DeleteValue(self, key, name):
        if name not in self.values[key.path]:
            raise FileNotFoundError(name)
        del self.values[key.path][name]


class RegistryStore:
    def __init__(self, registry): self.registry = registry
    def read(self):
        import base64, desktop_bridge as d
        run = self.registry.values.get(d.RUN_KEY, {}).get(d.RUN_VALUE)
        approval = self.registry.values.get(d.STARTUP_APPROVED_KEY, {}).get(d.RUN_VALUE)
        return {"run": {"kind": run[1], "value": run[0]} if run else None,
                "approval": {"kind": approval[1], "value": base64.b64encode(approval[0]).decode()} if approval else None}
    def write(self, value):
        import base64, desktop_bridge as d
        for field, path in (("run", d.RUN_KEY), ("approval", d.STARTUP_APPROVED_KEY)):
            item = value[field]
            values = self.registry.values.setdefault(path, {})
            if item is None: values.pop(d.RUN_VALUE, None)
            else: values[d.RUN_VALUE] = (base64.b64decode(item['value']) if field == 'approval' else item['value'], item['kind'])
        return self.read()

class DesktopBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeApp.starts = 0
        FakeApp.stops = 0
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.bridge = DesktopBridge(root / "codex", root / "data", root / "launcher.exe",
                                     app_factory=FakeApp)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_host_ready_arms_existing_enabled_install_without_erasing_logon_detection(self):
        from desktop_login import LoginEvidence
        exe = self.bridge.startup_executable
        exe.write_bytes(b'installed-program')
        evidence = LoginEvidence(self.bridge.data_dir, exe,
                                 context=lambda:dict(id='old', logonAt=100, kind=2), now=lambda:200)
        self.bridge.login_evidence = evidence
        with patch.object(self.bridge, '_startup_status', return_value={
                'enabled': True, 'verified': True, 'systemEnabled': True}):
            result = self.bridge.handle({'version': 1, 'requestId': 'manual-host', 'command': 'host_ready'})
            self.assertEqual(result['status'], 'confirmed')
            self.assertEqual(evidence.read()['stage'], 'awaiting_login')
            evidence.context = lambda:dict(id='new', logonAt=250, kind=2)
            evidence.now = lambda:260
            self.bridge.login_start = True
            result = self.bridge.handle({'version': 1, 'requestId': 'login-host', 'command': 'host_ready'})
            self.assertEqual(result['status'], 'confirmed')
            self.assertEqual(evidence.read()['stage'], 'login_detected')
            self.assertTrue(evidence.verified(startup=True, runtime_ready=True, qq_ready=True))

    def test_host_ready_does_not_arm_disabled_or_unconfirmed_startup(self):
        from desktop_login import LoginEvidence
        exe = self.bridge.startup_executable
        exe.write_bytes(b'installed-program')
        evidence = LoginEvidence(self.bridge.data_dir, exe,
                                 context=lambda:dict(id='old', logonAt=100, kind=2), now=lambda:200)
        self.bridge.login_evidence = evidence
        for index, status in enumerate((
                {'enabled': True, 'verified': True, 'systemEnabled': False},
                {'enabled': False, 'verified': True, 'systemEnabled': False},
                {'enabled': False, 'verified': False, 'systemEnabled': False})):
            with self.subTest(status=status), patch.object(self.bridge, '_startup_status', return_value=status):
                self.assertEqual(self.bridge.handle({'version': 1, 'requestId': f'disabled-{index}',
                    'command': 'host_ready'})['status'], 'confirmed')
                self.assertFalse(evidence.path.exists())

    def test_request_requires_version_id_command_and_object_params(self) -> None:
        with self.assertRaisesRegex(Exception, "protocol_version_invalid"):
            validate_request({"version": 2})
        with self.assertRaisesRegex(Exception, "command_not_allowed"):
            validate_request({"version": 1, "requestId": "x", "command": "run_shell"})
        with self.assertRaisesRegex(Exception, "params_invalid"):
            validate_request({"version": 1, "requestId": "x", "command": "set_startup", "params": []})

    def test_fresh_packaged_profile_does_not_migrate_shared_hooks(self) -> None:
        self.bridge.packaged = True
        with patch('desktop_bridge.managed_hooks_need_path_update', return_value=True) as hooks:
            self.assertFalse(self.bridge._migration_is_required())
            hooks.assert_not_called()

    def test_snapshot_reports_observable_hook_config_without_claiming_trust(self) -> None:
        with patch('desktop_bridge.hook_install_status', return_value={
                'features_hooks_enabled': True, 'managed_hooks_installed': True}), \
             patch('desktop_bridge.codex_connection_status', return_value='ready'), \
             patch('desktop_bridge.read_monitor_pid', return_value=None):
            snapshot = self.bridge.snapshot()
        self.assertEqual(snapshot['hooks'], 'ready')
        self.assertTrue(snapshot['hooksEnabled'])
        self.assertTrue(snapshot['hooksInstalled'])
        self.assertEqual(snapshot['hooksTrust'], 'unknown')

    def test_takeover_waits_for_resident_lock_after_monitor_pid_disappears(self) -> None:
        from runtime_io import acquire_file_lock
        lock_path = self.bridge.data_dir / 'state/app.lock'
        child = subprocess.Popen([
            sys.executable, '-c',
            'import os,sys,time; from pathlib import Path; '
            'from runtime_io import acquire_file_lock; '
            'lock=acquire_file_lock(Path(sys.argv[1])); '
            'assert lock is not None; print("locked",flush=True); '
            'sys.stdin.readline(); time.sleep(0.3); os.close(lock[0]); '
            'print("released",flush=True)', str(lock_path),
        ], env={**os.environ, 'PYTHONPATH': str(PACKAGE)},
           stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'locked')
            def request(*_args, **_kwargs):
                child.stdin.write('stop\n')
                child.stdin.flush()
            with patch('desktop_bridge.request_stop', side_effect=request), \
                 patch('desktop_bridge.read_monitor_pid', return_value=None), \
                 patch('desktop_bridge.acquire_file_lock', wraps=acquire_file_lock) as acquire:
                self.bridge._stop_runtime()
            self.assertGreaterEqual(acquire.call_count, 2)
            self.assertEqual(child.stdout.readline().strip(), 'released')
            self.assertEqual(child.wait(timeout=5), 0)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
            child.stdin.close()
            child.stdout.close()

    def test_takeover_refuses_to_continue_while_resident_lock_remains_busy(self) -> None:
        with patch('desktop_bridge.request_stop'), \
             patch('desktop_bridge.read_monitor_pid', return_value=None), \
             patch('desktop_bridge.acquire_file_lock', return_value=None), \
             patch('desktop_bridge.time.monotonic', side_effect=[0, 0, 16]), \
             patch('desktop_bridge.time.sleep'):
            with self.assertRaisesRegex(Exception, 'runtime_stop_not_confirmed'):
                self.bridge._stop_runtime()

    def test_duplicate_request_id_reuses_original_reply(self) -> None:
        request = {"version": 1, "requestId": "same", "command": "patch_preferences",
                   "params": {"theme": "dark"}}
        first = self.bridge.handle(request)
        second = self.bridge.handle(request)
        self.assertEqual(first, second)
        self.assertEqual(first["data"]["preferences"]["theme"], "dark")
        self.assertEqual(first["data"]["preferences"]["revision"], 1)

    def test_preference_patch_rejects_secret_like_unknown_fields(self) -> None:
        response = self.bridge.handle({"version": 1, "requestId": "x", "command": "patch_preferences",
                                       "params": {"appSecret": "must-not-be-stored"}})
        self.assertEqual(response["status"], "unknown")
        self.assertFalse(self.bridge.preference_path.exists())

    def test_frame_limit_closes_bridge_and_owned_runtime(self) -> None:
        self.bridge.app = FakeApp(Path("."), Path("."))
        source = io.BytesIO(b"x" * (MAX_FRAME_BYTES + 1))
        sink = io.BytesIO()
        result = serve(self.bridge, stdin=source, stdout=sink)
        self.assertEqual(result, 2)
        self.assertEqual(FakeApp.stops, 1)
        self.assertEqual(sink.getvalue(), b"")

    def test_protocol_responses_are_one_json_line(self) -> None:
        source = io.BytesIO((json.dumps({"version": 1, "requestId": "x", "command": "patch_preferences",
                                         "params": {"motion": False}}) + "\n").encode())
        sink = io.BytesIO()
        self.assertEqual(serve(self.bridge, stdin=source, stdout=sink), 0)
        result = json.loads(sink.getvalue().decode().splitlines()[0])
        self.assertEqual(result["status"], "confirmed")
        self.assertFalse(result["preview"])
        self.assertFalse(result["data"]["preferences"]["motion"])

    def test_startup_migration_updates_only_known_old_entry_and_enables_windows_state(self) -> None:
        import desktop_bridge

        executable = self.bridge.startup_executable
        executable.touch()
        legacy = executable.parent / "old" / "CodexQQ助手.exe"
        registry = FakeRegistry()
        registry.values[desktop_bridge.RUN_KEY] = {
            desktop_bridge.RUN_VALUE: (os.fspath(legacy), FakeRegistry.REG_SZ),
        }
        registry.values[desktop_bridge.STARTUP_APPROVED_KEY] = {
            desktop_bridge.RUN_VALUE: ((3).to_bytes(4, "little") + b"approved", FakeRegistry.REG_BINARY),
        }

        self.bridge.startup_service.store = RegistryStore(registry)
        with patch("desktop_bridge.os.name", "nt"), patch.dict(sys.modules, {"winreg": registry}):
            result = self.bridge._ensure_startup(legacy)
            state = self.bridge._startup_status()

        self.assertTrue(result["changed"])
        self.assertTrue((Path(result["backupDir"]) / "registry-before.json").is_file())
        self.assertEqual(registry.values[desktop_bridge.RUN_KEY][desktop_bridge.RUN_VALUE],
                         (startup_command(executable, start_hidden=True), FakeRegistry.REG_SZ))
        approval = registry.values[desktop_bridge.STARTUP_APPROVED_KEY][desktop_bridge.RUN_VALUE][0]
        self.assertEqual(int.from_bytes(approval[:4], "little"), 2)
        self.assertTrue(state["enabled"])
        self.assertTrue(state["systemEnabled"])

    def test_startup_migration_preserves_foreign_entry(self) -> None:
        import desktop_bridge

        executable = self.bridge.startup_executable
        executable.touch()
        registry = FakeRegistry()
        registry.values[desktop_bridge.RUN_KEY] = {
            desktop_bridge.RUN_VALUE: ("C:\\Other\\unrelated.exe", FakeRegistry.REG_SZ),
        }
        original = dict(registry.values[desktop_bridge.RUN_KEY])

        self.bridge.startup_service.store = RegistryStore(registry)
        with patch("desktop_bridge.os.name", "nt"), patch.dict(sys.modules, {"winreg": registry}):
            with self.assertRaisesRegex(Exception, "startup_entry_conflict_preserved"):
                self.bridge._ensure_startup(executable.parent / "old" / "CodexQQ助手.exe")

        self.assertEqual(registry.values[desktop_bridge.RUN_KEY], original)


if __name__ == "__main__":
    unittest.main()
