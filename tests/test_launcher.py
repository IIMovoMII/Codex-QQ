from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


PACKAGE_DIR = Path(__file__).resolve().parents[1] / "src" / "codex_desktop_weixin"
sys.path.insert(0, str(PACKAGE_DIR))

import launcher
from launcher import (
    cc_connect_api_socket_path,
    cc_connect_api_status,
    cleanup_stale_cc_connect_socket,
    _capture_process_identity,
    read_monitor_pid,
    clear_monitor_pid,
    read_cc_connect_pid,
    write_monitor_pid,
    wait_for_codex_desktop,
)
from notifier import DeliverySettings, save_delivery_settings
from process_watch import ProcessStatus


class _Win32Call:
    def __init__(self, call):
        self.call = call

    def __call__(self, *args):
        return self.call(*args)


class _StopKernel:
    """An opened handle stays bound to its original simulated process."""
    def __init__(self, identity, *, waits=(258, 258, 0)):
        self.identity = identity
        self.waits = iter(waits)
        self.opened = []
        self.terminated = []
        self.closed = []
        self.OpenProcess = _Win32Call(self._open)
        self.QueryFullProcessImageNameW = _Win32Call(self._query_image)
        self.GetProcessTimes = _Win32Call(self._times)
        self.WaitForSingleObject = _Win32Call(lambda handle, timeout: next(self.waits))
        self.TerminateProcess = _Win32Call(lambda handle, code: self.terminated.append(handle) or True)
        self.CloseHandle = _Win32Call(lambda handle: self.closed.append(handle) or True)

    def _open(self, access, inherit, pid):
        self.opened.append((access, pid))
        return 123

    def _query_image(self, handle, flags, buffer, size):
        buffer.value = self.identity['process_image']
        return True

    def _times(self, handle, creation, exit_time, kernel_time, user_time):
        ticks = round((self.identity['process_start_time'] + launcher.WINDOWS_EPOCH_OFFSET_SECONDS) * 10_000_000)
        creation._obj.dwLowDateTime = ticks & 0xffffffff
        creation._obj.dwHighDateTime = ticks >> 32
        return True


class CCConnectLifecycleTests(unittest.TestCase):
    def _stop_with_kernel(self, kernel, state):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / 'state' / 'cc-connect.json'
            state_path.parent.mkdir()
            state_path.write_text(json.dumps(state), encoding='utf-8')
            with patch('launcher.os.name', 'nt'), \
                 patch('launcher.ctypes.WinDLL', return_value=kernel, create=True), \
                 patch('launcher.ctypes.WinError', side_effect=lambda code: OSError(code, 'simulated Windows failure'), create=True), \
                 patch('launcher.ctypes.get_last_error', return_value=5, create=True), \
                 patch('launcher._read_service_state', return_value=state), \
                 patch('launcher.read_cc_connect_pid', return_value=42), \
                 patch('launcher._process_identity_matches', return_value=True), \
                 patch('launcher._pid_alive', return_value=True), \
                 patch('launcher._send_windows_ctrl_break') as graceful, \
                 patch('launcher.cleanup_stale_cc_connect_socket', return_value={}), \
                 patch('launcher.time.monotonic', return_value=0):
                result = launcher.stop_cc_connect_service(root, timeout_seconds=0)
            self.assertEqual(state_path.exists(), not result['cc_connect_stopped'])
        return result, graceful

    def test_force_stop_verifies_the_opened_handle_before_terminating(self):
        state = {'identity_version': 1, 'pid': 42, 'process_image': r'C:\\gateway.exe', 'process_start_time': 1000.0}
        kernel = _StopKernel({**state, 'process_start_time': 2000.0})
        result, graceful = self._stop_with_kernel(kernel, state)
        self.assertEqual(kernel.terminated, [], 'a PID reused after the last identity check must not be killed')
        self.assertFalse(result['cc_connect_stopped'])
        self.assertEqual(result['reason'], 'managed-process-identity-changed')
        graceful.assert_not_called()
        self.assertEqual(kernel.closed, [123])

    def test_force_stop_rejects_same_image_restarted_within_a_quarter_second(self):
        state = {'identity_version': 1, 'pid': 42, 'process_image': r'C:\\gateway.exe', 'process_start_time': 1000.0}
        kernel = _StopKernel({**state, 'process_start_time': 1000.1})
        result, graceful = self._stop_with_kernel(kernel, state)
        self.assertFalse(result['cc_connect_stopped'])
        self.assertEqual(result['reason'], 'managed-process-identity-changed')
        self.assertEqual(kernel.terminated, [])
        graceful.assert_not_called()

    def test_force_stop_confirms_exit_on_the_same_verified_handle(self):
        state = {'identity_version': 1, 'pid': 42, 'process_image': r'C:\\gateway.exe', 'process_start_time': 1000.0}
        kernel = _StopKernel(state)
        result, graceful = self._stop_with_kernel(kernel, state)
        self.assertTrue(result['cc_connect_stopped'])
        self.assertEqual(result['reason'], 'forced_after_graceful_timeout')
        self.assertEqual(kernel.terminated, [123])
        self.assertEqual(kernel.closed, [123])
        self.assertEqual(kernel.opened, [(0x101001, 42)])
        graceful.assert_called_once_with(42)

    def test_force_stop_timeout_preserves_state_and_closes_handle(self):
        state = {'identity_version': 1, 'pid': 42, 'process_image': r'C:\\gateway.exe', 'process_start_time': 1000.0}
        kernel = _StopKernel(state, waits=(258, 258, 258))
        result, _ = self._stop_with_kernel(kernel, state)
        self.assertFalse(result['cc_connect_stopped'])
        self.assertEqual(result['reason'], 'force_timeout')
        self.assertEqual(kernel.terminated, [123])
        self.assertEqual(kernel.closed, [123])

    def test_graceful_stop_confirms_exit_on_the_verified_handle(self):
        state = {'identity_version': 1, 'pid': 42, 'process_image': r'C:\\gateway.exe', 'process_start_time': 1000.0}
        kernel = _StopKernel(state, waits=(258, 0))
        result, graceful = self._stop_with_kernel(kernel, state)
        self.assertTrue(result['cc_connect_stopped'])
        self.assertEqual(result['reason'], 'graceful')
        self.assertEqual(kernel.terminated, [])
        self.assertEqual(kernel.closed, [123])
        graceful.assert_called_once_with(42)

    def test_exit_between_grace_timeout_and_terminate_is_confirmed_by_handle(self):
        state = {'identity_version': 1, 'pid': 42, 'process_image': r'C:\\gateway.exe', 'process_start_time': 1000.0}
        kernel = _StopKernel(state)
        kernel.TerminateProcess = _Win32Call(lambda handle, code: False)
        result, _ = self._stop_with_kernel(kernel, state)
        self.assertTrue(result['cc_connect_stopped'])
        self.assertEqual(result['reason'], 'graceful_during_force_race')
        self.assertEqual(kernel.closed, [123])

    def test_process_already_exited_does_not_receive_console_signal(self):
        state = {'identity_version': 1, 'pid': 42, 'process_image': r'C:\\gateway.exe', 'process_start_time': 1000.0}
        kernel = _StopKernel(state, waits=(0,))
        result, graceful = self._stop_with_kernel(kernel, state)
        self.assertTrue(result['cc_connect_stopped'])
        self.assertEqual(kernel.terminated, [])
        self.assertEqual(kernel.closed, [123])
        graceful.assert_not_called()

    def test_stop_open_failure_preserves_state_without_signalling(self):
        state = {'identity_version': 1, 'pid': 42, 'process_image': r'C:\\gateway.exe', 'process_start_time': 1000.0}
        kernel = _StopKernel(state)
        kernel.OpenProcess = _Win32Call(lambda *args: 0)
        result, graceful = self._stop_with_kernel(kernel, state)
        self.assertFalse(result['cc_connect_stopped'])
        self.assertEqual(kernel.terminated, [])
        self.assertEqual(kernel.closed, [])
        graceful.assert_not_called()

    def test_stop_wait_failure_preserves_state_and_closes_handle(self):
        state = {'identity_version': 1, 'pid': 42, 'process_image': r'C:\\gateway.exe', 'process_start_time': 1000.0}
        kernel = _StopKernel(state, waits=(0xffffffff,))
        result, graceful = self._stop_with_kernel(kernel, state)
        self.assertFalse(result['cc_connect_stopped'])
        self.assertEqual(kernel.terminated, [])
        self.assertEqual(kernel.closed, [123])
        graceful.assert_not_called()

    @unittest.skipUnless(os.name == 'nt', 'Windows process handles required')
    def test_real_windows_stop_uses_verified_handle_and_preserves_other_child(self):
        children = []
        try:
            for _ in range(2):
                children.append(subprocess.Popen(
                    [sys.executable, '-c', 'import time; time.sleep(120)'],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW))
            target, other = children
            state = launcher._capture_process_identity(target.pid)
            # Isolate the forced-stop path without signalling any console.
            # Identity lookup, process opening, termination and wait are real Win32.
            with patch('launcher._send_windows_ctrl_break'):
                reason = launcher._stop_windows_process(target.pid, state, 0.01)
            self.assertEqual(reason, 'forced_after_graceful_timeout')
            self.assertEqual(target.wait(timeout=5), 1)
            self.assertIsNone(other.poll())
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)

    def test_api_status_tolerates_unavailable_local_socket_support(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch('launcher.socket.socket', side_effect=PermissionError('AF_UNIX is unavailable')):
                self.assertIsNone(launcher._socket_is_reachable(root / 'api.sock'))
                self.assertEqual(cc_connect_api_status(root), 'missing')

    def test_unavailable_probe_preserves_existing_socket(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            socket_path = cc_connect_api_socket_path(root)
            socket_path.parent.mkdir(parents=True)
            socket_path.touch()
            stream = Mock()
            stream.settimeout.side_effect = PermissionError('socket options unavailable')
            with patch.object(launcher.socket, 'AF_UNIX', 1, create=True), \
                    patch('launcher.socket.socket', return_value=stream):
                self.assertIsNone(launcher._socket_is_reachable(socket_path))
                result = cleanup_stale_cc_connect_socket(root)
            self.assertEqual(result['socket_cleanup'], 'failed')
            self.assertTrue(socket_path.exists())
            stream.connect.assert_not_called()
            self.assertEqual(stream.close.call_count, 2)

    def test_monitor_ready_flag_distinguishes_starting_from_running(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_monitor_pid(root, ready=False)
            self.assertIsNotNone(read_monitor_pid(root))
            self.assertIsNone(read_monitor_pid(root, require_ready=True))
            write_monitor_pid(root, ready=True)
            self.assertIsNotNone(read_monitor_pid(root, require_ready=True))

    def test_pid_reuse_does_not_count_as_managed_monitor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_monitor_pid(root, ready=True)
            state_path = root / "state" / "monitor.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["process_image"] = "C:\\Windows\\System32\\unrelated.exe"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            self.assertIsNone(read_monitor_pid(root, require_ready=True))

    def test_legacy_pid_state_without_identity_is_not_trusted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "state" / "monitor.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(json.dumps({"pid": os.getpid(), "ready": True}), encoding="utf-8")
            self.assertIsNone(read_monitor_pid(root, require_ready=True))

    def test_non_object_monitor_state_is_preserved_and_treated_as_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / 'state' / 'monitor.json'; state_path.parent.mkdir(parents=True)
            state_path.write_text('[]', encoding='utf-8')
            self.assertIsNone(read_monitor_pid(root))
            launcher.clear_monitor_pid(root)
            self.assertEqual(state_path.read_text(encoding='utf-8'), '[]')

    def test_non_object_monitor_state_is_preserved_and_treated_as_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / 'state' / 'monitor.json'; state_path.parent.mkdir(parents=True)
            state_path.write_text('[]', encoding='utf-8')
            self.assertIsNone(read_monitor_pid(root))
            clear_monitor_pid(root)
            self.assertEqual(state_path.read_text(encoding='utf-8'), '[]')

    def test_pid_reuse_does_not_count_as_managed_cc_connect(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "state" / "cc-connect.json"
            state_path.parent.mkdir(parents=True)
            state = {"pid": os.getpid(), **_capture_process_identity(os.getpid())}
            state_path.write_text(json.dumps(state), encoding="utf-8")
            self.assertEqual(read_cc_connect_pid(root), os.getpid())
            state["process_start_time"] = (state.get("process_start_time") or 0) + 3600
            state["process_image"] = "C:\\Windows\\System32\\unrelated.exe"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            self.assertIsNone(read_cc_connect_pid(root))

    def test_wait_for_desktop_retries_until_process_tree_is_visible(self) -> None:
        with patch(
            "launcher.get_codex_desktop_process_status",
            side_effect=[
                ProcessStatus(desktop_alive=False, app_server_alive=False),
                ProcessStatus(desktop_alive=True, app_server_alive=False),
            ],
        ) as status:
            result = wait_for_codex_desktop(timeout_seconds=1.0, poll_seconds=0.0)

        self.assertTrue(result["desktop_detected"])
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(status.call_count, 2)

    def test_wait_for_desktop_rejects_cli_rollout_before_process_check(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            transcript = Path(temporary) / "rollout.jsonl"
            transcript.write_text(
                '{"type":"session_meta","payload":{"source":"exec"}}\n',
                encoding="utf-8",
            )
            with patch("launcher.get_codex_desktop_process_status") as status:
                result = wait_for_codex_desktop(transcript_path=transcript, timeout_seconds=0.1)

        self.assertFalse(result["desktop_detected"])
        self.assertEqual(result["reason"], "non_desktop_session")
        status.assert_not_called()

    def test_wait_retries_incomplete_rollout_even_with_desktop_running(self) -> None:
        with patch("launcher.is_desktop_rollout", side_effect=[None, True]), patch(
            "launcher.get_codex_desktop_process_status", return_value=ProcessStatus(True, True)
        ), patch("launcher.time.sleep"):
            result = wait_for_codex_desktop(transcript_path="pending.jsonl", timeout_seconds=1.0)
        self.assertTrue(result["desktop_detected"])
        self.assertEqual(result["attempts"], 2)

    def test_wait_does_not_start_from_stale_desktop_transcript_after_exit(self) -> None:
        with patch("launcher.is_desktop_rollout", return_value=True), patch(
            "launcher.get_codex_desktop_process_status", return_value=ProcessStatus(False, False)
        ):
            result = wait_for_codex_desktop(transcript_path="old.jsonl", timeout_seconds=0)
        self.assertFalse(result["desktop_detected"])
        self.assertEqual(result["reason"], "desktop_not_detected")

    def test_wait_times_out_without_busy_loop(self) -> None:
        with patch("launcher.get_codex_desktop_process_status", return_value=ProcessStatus(False, False)):
            result = wait_for_codex_desktop(timeout_seconds=0)
        self.assertFalse(result["desktop_detected"])
        self.assertEqual(result["attempts"], 1)

    def test_stale_project_socket_is_removed_when_service_is_not_running(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cc_data = root / "cc-data"
            save_delivery_settings(
                root / "settings.json",
                DeliverySettings(mode="cc-connect", data_dir=str(cc_data)),
            )
            socket_path = cc_connect_api_socket_path(root)
            socket_path.parent.mkdir(parents=True)
            socket_path.write_bytes(b"")

            with patch('launcher._socket_is_reachable', return_value=False):
                result = cleanup_stale_cc_connect_socket(root)

            self.assertEqual(result["socket_cleanup"], "removed")
            self.assertFalse(socket_path.exists())

    def test_unremovable_windows_socket_rotates_run_directory_and_preserves_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cc_data = root / "cc-data"
            save_delivery_settings(
                root / "settings.json",
                DeliverySettings(mode="cc-connect", data_dir=str(cc_data)),
            )
            socket_path = cc_connect_api_socket_path(root)
            socket_path.parent.mkdir(parents=True)
            socket_path.write_bytes(b"")
            cache_path = socket_path.parent / "qqbot_message_cache.json"
            cache_path.write_text('{"seen": true}', encoding="utf-8")

            with patch("launcher._unlink_socket_path", side_effect=OSError("special endpoint")), \
                 patch('launcher._socket_is_reachable', return_value=False):
                result = cleanup_stale_cc_connect_socket(root)

            if sys.platform == "win32":
                self.assertEqual(result["socket_cleanup"], "rotated_run_directory")
                self.assertFalse(socket_path.exists())
                self.assertEqual(cache_path.read_text(encoding="utf-8"), '{"seen": true}')
                self.assertTrue(Path(result["stale_run_directory"]).is_dir())
            else:
                self.assertEqual(result["socket_cleanup"], "failed")

    def test_latest_api_log_state_wins(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            save_delivery_settings(
                root / "settings.json",
                DeliverySettings(mode="cc-connect", data_dir=str(root / "cc-data")),
            )
            log = root / "logs" / "cc-connect.log"
            log.parent.mkdir(parents=True)
            log.write_text('level=INFO msg="api server started"\n', encoding="utf-8")
            state_path = root / "state" / "cc-connect.json"
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(
                json.dumps({"pid": os.getpid(), "log_offset": 0, **_capture_process_identity(os.getpid())}),
                encoding="utf-8",
            )
            self.assertEqual(cc_connect_api_status(root), "ready")

            with log.open("a", encoding="utf-8") as stream:
                stream.write('level=WARN msg="api server unavailable"\n')
            self.assertEqual(cc_connect_api_status(root), "unavailable")

    def test_historical_api_ready_log_is_not_live_without_managed_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            save_delivery_settings(
                root / "settings.json",
                DeliverySettings(mode="cc-connect", data_dir=str(root / "cc-data")),
            )
            log = root / "logs" / "cc-connect.log"
            log.parent.mkdir(parents=True)
            log.write_text('level=INFO msg="api server started"\n', encoding="utf-8")
            self.assertNotEqual(cc_connect_api_status(root), "ready")


if __name__ == "__main__":
    unittest.main()
