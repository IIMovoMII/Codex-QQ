import ctypes as c
from ctypes import wintypes as w
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from setup_process import PairingGateway, wait_gateway_ready
from setup_diagnostics import InstallFailure

READY = b'time=fixture level=INFO msg="qqbot: gateway READY" session_id=fixture\n'


class GatewayReadyTests(unittest.TestCase):
    def test_ready_requires_current_launch_exact_info_event(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'log'
            path.write_bytes(READY)
            old_end = path.stat().st_size
            ticks = iter([0, 0, 1, 3])
            child = Mock()
            child.poll.return_value = None
            with self.assertRaisesRegex(ValueError, 'READY.*超时'):
                wait_gateway_ready(child, path, offset=old_end, timeout=2,
                                   sleep=lambda _: None, clock=lambda: next(ticks))
            path.write_bytes(READY + b'level=INFO msg="API ready"\n' + READY)
            wait_gateway_ready(child, path, offset=old_end, timeout=2)

    def test_immediate_exit_preserves_code(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'log'
            path.write_bytes(READY)
            child = Mock()
            child.poll.return_value = 37
            with self.assertRaisesRegex(ValueError, '退出码 37'):
                wait_gateway_ready(child, path, offset=0)

    def test_cancel_during_wait_is_not_swallowed(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'log'
            path.touch()
            child = Mock()
            child.poll.return_value = None
            def cancel(_):
                raise KeyboardInterrupt
            with self.assertRaises(KeyboardInterrupt):
                wait_gateway_ready(child, path, offset=0, sleep=cancel)


@unittest.skipUnless(os.name == 'nt', 'real Windows job and process integration')
class WindowsSetupProcessTests(unittest.TestCase):
    def gateway(self, directory, log, *args):
        return PairingGateway([sys.executable, '-X', 'utf8', *args], cwd=directory, log=log)

    def assert_ended(self, handle):
        self.assertEqual(self.k.WaitForSingleObject(handle, 5000), 0, 'owned process survived job cleanup')

    def setUp(self):
        self.k = c.WinDLL('kernel32', use_last_error=True)
        self.k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        self.k.OpenProcess.restype = w.HANDLE
        self.k.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        self.k.CloseHandle.argtypes = [w.HANDLE]

    def test_forced_parent_exit_cleans_gateway_and_descendant_but_not_other_process(self):
        import json
        helper = str(ROOT / 'tests/helpers/setup_process_child.py')
        for force in (False, True, 'window-close'):
            with self.subTest(force=force), tempfile.TemporaryDirectory() as temp:
                base = Path(temp)
                handles = []
                unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],
                                              creationflags=subprocess.CREATE_NO_WINDOW)
                startup = subprocess.STARTUPINFO()
                startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startup.wShowWindow = 0
                parent = subprocess.Popen([sys.executable, '-X', 'utf8', helper, 'parent', temp],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    creationflags=subprocess.CREATE_NEW_CONSOLE if force == 'window-close' else subprocess.CREATE_NO_WINDOW,
                    startupinfo=startup)
                try:
                    deadline = time.monotonic() + 10
                    while not (base / 'parent-ready').exists() and time.monotonic() < deadline:
                        if parent.poll() is not None:
                            self.fail('isolated parent failed: ' + parent.stderr.read().decode('utf-8', 'replace'))
                        time.sleep(0.03)
                    self.assertTrue((base / 'parent-ready').exists())
                    progress = {p.name: p.read_bytes() for p in (base / 'runtime').iterdir() if p.is_file()}
                    for pid in json.loads((base / 'tree.json').read_text()).values():
                        handle = self.k.OpenProcess(0x100000, False, pid)
                        self.assertTrue(handle)
                        handles.append(handle)  # stable handles, not reused PIDs
                    if force == 'window-close':
                        window = json.loads((base / 'parent-ready').read_text())['window']
                        user = c.WinDLL('user32', use_last_error=True)
                        user.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
                        if not window:
                            self.skipTest('no private console window on this Windows host; hard-kill job test still ran')
                        self.assertTrue(user.PostMessageW(window, 0x10, 0, 0))  # WM_CLOSE, same as X
                    elif force:
                        parent.kill()  # TerminateProcess bypasses Python finally
                    else:
                        parent.stdin.write(b'exit\n')
                        parent.stdin.flush()
                    parent.wait(timeout=10)
                    for handle in handles:
                        self.assert_ended(handle)
                    self.assertIsNone(unrelated.poll())
                    self.assertEqual(progress, {p.name: p.read_bytes() for p in (base / 'runtime').iterdir() if p.is_file()})
                    # Re-run the same controlled installer directory after its
                    # old job has died; persisted binding progress is reused.
                    (base / 'parent-ready').unlink()
                    with subprocess.Popen([sys.executable, '-X', 'utf8', helper, 'parent', temp],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=subprocess.CREATE_NO_WINDOW) as resumed:
                        try:
                            deadline = time.monotonic() + 8
                            while not (base / 'parent-ready').exists() and time.monotonic() < deadline:
                                if resumed.poll() is not None:
                                    self.fail(resumed.stderr.read().decode('utf-8', 'replace'))
                                time.sleep(0.03)
                            self.assertTrue((base / 'parent-ready').exists())
                        finally:
                            resumed.communicate(b'exit\n', timeout=5)
                    self.assertEqual(progress, {p.name: p.read_bytes() for p in (base / 'runtime').iterdir() if p.is_file()})
                finally:
                    if parent.poll() is None:
                        parent.kill()
                    parent.communicate(timeout=5)
                    unrelated.terminate()
                    unrelated.wait(timeout=5)
                    for handle in handles:
                        self.k.CloseHandle(handle)

    def test_timeout_cancel_retry_and_early_exit_leave_no_owned_process(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            path = directory / 'gateway.log'
            for mode in ('timeout', 'cancel', 'ready', 'exit'):
                with self.subTest(mode=mode), path.open('ab') as log:
                    offset = log.tell()
                    script = ('raise SystemExit(19)' if mode == 'exit' else
                              ('print(\'time=t level=INFO msg="qqbot: gateway READY"\', flush=True); ' if mode == 'ready' else '')
                              + 'import time; time.sleep(120)')
                    handle = None
                    try:
                        with self.gateway(directory, log, '-c', script) as child:
                            handle = self.k.OpenProcess(0x100000, False, child.pid)
                            self.assertTrue(handle)
                            if mode == 'ready':
                                wait_gateway_ready(child, path, offset=offset, timeout=3)
                            elif mode == 'cancel':
                                def cancel(_):
                                    raise KeyboardInterrupt
                                with self.assertRaises(KeyboardInterrupt):
                                    wait_gateway_ready(child, path, offset=offset, sleep=cancel)
                            else:
                                with self.assertRaisesRegex(ValueError, '超时' if mode == 'timeout' else '退出码 19'):
                                    wait_gateway_ready(child, path, offset=offset, timeout=0.4 if mode == 'timeout' else 3)
                        self.assert_ended(handle)
                    finally:
                        if handle:
                            self.k.CloseHandle(handle)

    def test_inherited_log_redirection_cannot_hide_ready_or_change_parent_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            path = directory / 'gateway.log'
            redirected = directory / 'do-not-touch.log'
            redirected.write_bytes(b'user-owned')
            with patch.dict(os.environ, {'CC_LOG_FILE': str(redirected), 'CODEX_QQ_TEST_ENV': '中文'}), path.open('ab') as log:
                script = ('import os; assert "CC_LOG_FILE" not in os.environ; '
                          'assert os.environ["CODEX_QQ_TEST_ENV"] == "中文"; '
                          'print(\'level=INFO msg="qqbot: gateway READY"\', flush=True); '
                          'import time; time.sleep(120)')
                with self.gateway(directory, log, '-c', script) as child:
                    wait_gateway_ready(child, path, offset=0, timeout=3)
                self.assertEqual(os.environ['CC_LOG_FILE'], str(redirected))
            self.assertEqual(redirected.read_bytes(), b'user-owned')

    def test_cleanup_failure_reports_stage_and_kill_on_close_still_owns_process(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            handle = None
            with (directory / 'log').open('ab') as log:
                child = self.gateway(directory, log, '-c', 'import time; time.sleep(120)')
                try:
                    handle = self.k.OpenProcess(0x100000, False, child.pid)
                    with patch.object(child._k, 'TerminateJobObject', return_value=False), \
                            self.assertRaises(InstallFailure) as caught:
                        child.close()
                    self.assertEqual(caught.exception.stage, '临时绑定网关清理')
                    self.assertIn(str(directory / 'log'), str(caught.exception))
                    self.assert_ended(handle)
                finally:
                    child.close()
                    if handle:
                        self.k.CloseHandle(handle)


if __name__ == '__main__':
    unittest.main()
