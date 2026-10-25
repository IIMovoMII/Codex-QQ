from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


PACKAGE_DIR = Path(__file__).resolve().parents[1] / "src" / "codex_desktop_weixin"
sys.path.insert(0, str(PACKAGE_DIR))

from cli import _acquire_autostart_lock, command_ensure_runtime
from installer import build_hook_command
from launcher import _pid_alive, read_monitor_pid
from notifier import DeliverySettings, save_delivery_settings
from process_watch import ProcessStatus, get_codex_desktop_process_status
from runtime_io import atomic_write_json


class AutostartTests(unittest.TestCase):
    def logs(self, root: Path) -> list[dict]:
        return [json.loads(line) for line in (root / "logs" / "autostart.jsonl").read_text(encoding="utf-8").splitlines()]

    def test_lock_is_not_taken_twice_and_is_released_without_deleting_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = _acquire_autostart_lock(root)
            self.assertIsNotNone(first)
            descriptor, lock_path = first
            try:
                code = (
                    "import os, sys; "
                    f"sys.path.insert(0, {str(PACKAGE_DIR)!r}); "
                    "from cli import _acquire_autostart_lock; "
                    "from pathlib import Path; "
                    "lock = _acquire_autostart_lock(Path(sys.argv[1])); "
                    "print('busy' if lock is None else 'acquired'); "
                    "os.close(lock[0]) if lock is not None else None"
                )
                contender = subprocess.run(
                    [sys.executable, "-c", code, str(root)],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(contender.returncode, 0, contender.stderr)
                self.assertEqual(contender.stdout.strip(), "busy")
            finally:
                os.close(descriptor)
            self.assertTrue(lock_path.exists())
            second = _acquire_autostart_lock(root)
            self.assertIsNotNone(second)
            os.close(second[0])

    def test_missing_desktop_never_starts_services_and_logs_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch("cli.wait_for_codex_desktop", return_value={"desktop_detected": False, "attempts": 4}), patch(
                "cli.start_background_monitor"
            ) as monitor, patch("cli.start_cc_connect_service") as cc:
                result = command_ensure_runtime(root / ".codex", root, wait_seconds=0, trigger_event="SessionStart")
            self.assertEqual(result, 0)
            monitor.assert_not_called()
            cc.assert_not_called()
            self.assertEqual(self.logs(root)[-1]["reason"], "desktop_not_detected")
            self.assertIsNotNone(lock := _acquire_autostart_lock(root))
            os.close(lock[0])

    def test_competing_helper_cannot_enter_startup_while_lock_is_owned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock = _acquire_autostart_lock(root)
            self.assertIsNotNone(lock)
            try:
                # Exercise the helper entry point in another process, with
                # every possible service launch replaced by a failing stub.
                code = "\n".join([
                    "import sys",
                    f"sys.path.insert(0, {str(PACKAGE_DIR)!r})",
                    "from pathlib import Path",
                    "from unittest.mock import patch",
                    "from cli import command_ensure_runtime",
                    "root = Path(sys.argv[1])",
                    "with patch('cli.wait_for_codex_desktop', side_effect=AssertionError('entered detection')), patch('cli.start_cc_connect_service', side_effect=AssertionError('started gateway')), patch('cli.start_background_monitor', side_effect=AssertionError('started monitor')):",
                    "    raise SystemExit(command_ensure_runtime(root / '.codex', root, wait_seconds=0))",
                ])
                contender = subprocess.run([sys.executable, "-c", code, str(root)], capture_output=True, text=True, timeout=10)
                self.assertEqual(contender.returncode, 0, contender.stderr)
                self.assertEqual(self.logs(root)[-1]["status"], "lock_busy")
            finally:
                os.close(lock[0])

    def test_shadow_mode_starts_monitor_without_cc_connect(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch("cli.wait_for_codex_desktop", return_value={"desktop_detected": True, "attempts": 2}), patch(
                "cli.start_background_monitor", return_value={"monitor_pid": 123}
            ), patch("cli.start_cc_connect_service") as cc:
                result = command_ensure_runtime(root / ".codex", root, trigger_event="SessionStart")
            self.assertEqual(result, 0)
            cc.assert_not_called()
            self.assertEqual(self.logs(root)[-1]["status"], "ready")

    def test_transient_bridge_failure_retries_and_reports_full_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            save_delivery_settings(root / "settings.json", DeliverySettings(mode="cc-connect"))
            with patch("cli.wait_for_codex_desktop", return_value={"desktop_detected": True}), patch(
                "cli.start_cc_connect_service",
                side_effect=[{"reason": "api_unavailable"}, {"cc_connect_pid": 456, "cc_connect_api_status": "ready"}],
            ) as cc, patch("cli.start_background_monitor", return_value={"monitor_pid": 123}), patch(
                "cli.get_codex_desktop_process_status", return_value=ProcessStatus(True, True)
            ), patch("cli.time.sleep"):
                result = command_ensure_runtime(root / ".codex", root)
            self.assertEqual(result, 0)
            self.assertEqual(cc.call_count, 2)
            self.assertEqual([row["status"] for row in self.logs(root)], ["failed", "ready"])

    def test_unconfigured_bridge_does_not_retry_forever_or_report_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            save_delivery_settings(root / "settings.json", DeliverySettings(mode="cc-connect"))
            with patch("cli.wait_for_codex_desktop", return_value={"desktop_detected": True}), patch(
                "cli.start_cc_connect_service", return_value={"reason": "config_missing"}
            ) as cc, patch("cli.start_background_monitor", return_value={"monitor_pid": 123}):
                result = command_ensure_runtime(root / ".codex", root)
            self.assertEqual(result, 1)
            self.assertEqual(cc.call_count, 1)
            self.assertEqual(self.logs(root)[-1]["status"], "failed")

    def test_startup_exception_is_logged_without_exception_contents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch("cli.wait_for_codex_desktop", side_effect=OSError("private prompt and secret")):
                result = command_ensure_runtime(root / ".codex", root)
            self.assertEqual(result, 1)
            record = self.logs(root)[-1]
            self.assertEqual(record["reason"], "startup_exception")
            self.assertNotIn("private prompt", json.dumps(record))
            self.assertIsNotNone(lock := _acquire_autostart_lock(root))
            os.close(lock[0])

    @unittest.skipUnless(os.name == "nt", "Windows Desktop hook integration")
    def test_real_quoted_hook_starts_isolated_shadow_monitor_and_stops(self) -> None:
        if not get_codex_desktop_process_status().desktop_alive:
            self.skipTest("Codex Desktop is not running")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "中文测试目录"
            home = root / ".codex"
            transcript = home / "sessions" / "rollout.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text(
                '{"type":"session_meta","payload":{"source":"vscode","id":"test"}}\n',
                encoding="utf-8",
            )
            local_app_data = root / "local"
            data = local_app_data / "CodexDesktopWeixin"
            environment = {**os.environ, "LOCALAPPDATA": str(local_app_data), "CODEX_HOME": str(home), "PYTHONUTF8": "0"}
            started = time.monotonic()
            result = subprocess.run(
                [
                    "powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
                    "-Command", build_hook_command(PACKAGE_DIR / "cli.py"),
                ],
                input=json.dumps(
                    {"hook_event_name": "SessionStart", "session_id": "test", "transcript_path": str(transcript), "cwd": str(root)},
                    ensure_ascii=False,
                ),
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=environment,
                timeout=5,
            )
            hook_elapsed = time.monotonic() - started
            monitor_pid = None
            try:
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertLess(hook_elapsed, 3.0)
                # Three bounded native startup attempts may outlast the old
                # 8-second fixture limit. Wait for the production ready marker,
                # not the first PID write; short reader locks can be transient.
                deadline = time.monotonic() + 15.0
                while not read_monitor_pid(data) and time.monotonic() < deadline:
                    time.sleep(0.1)
                monitor_pid = read_monitor_pid(data)
                diagnostics = {}
                if monitor_pid is None:
                    for name in ('autostart.jsonl', 'autostart-console.log', 'monitor-console.log'):
                        path = data / 'logs' / name
                        if path.exists():
                            diagnostics[name] = path.read_text(encoding='utf-8', errors='replace')[-4000:]
                self.assertIsNotNone(monitor_pid, diagnostics)
                self.assertFalse((data / "state" / "cc-connect.json").exists())
                while (
                    not (data / "logs" / "autostart.jsonl").exists()
                    or not self.logs(data)
                    or self.logs(data)[-1].get("status") != "ready"
                ) and time.monotonic() < deadline:
                    time.sleep(0.1)
                self.assertEqual(self.logs(data)[-1]["status"], "ready", self.logs(data))
                events = list((data / "spool" / "processed").glob("*.json")) or list((data / "spool" / "incoming").glob("*.json"))
                event = json.loads(events[0].read_text(encoding="utf-8"))
                self.assertEqual(event["project_name"], "中文测试目录")
                stopped = subprocess.run(
                    ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", build_hook_command(PACKAGE_DIR / "cli.py")],
                    input=json.dumps({"hook_event_name": "Stop", "session_id": "test", "turn_id": "turn-test", "transcript_path": str(transcript)}),
                    capture_output=True, text=True, encoding="utf-8", env=environment, timeout=5,
                )
                self.assertEqual(stopped.returncode, 0, stopped.stderr)
                self.assertEqual(json.loads(stopped.stdout), {"continue": True})
            finally:
                atomic_write_json(data / "stop.request", {"requested_at": time.time()})
                deadline = time.monotonic() + 8.0
                while read_monitor_pid(data) and time.monotonic() < deadline:
                    time.sleep(0.1)
                while monitor_pid and _pid_alive(monitor_pid) and time.monotonic() < deadline:
                    time.sleep(0.1)
            self.assertIsNone(read_monitor_pid(data))


if __name__ == "__main__":
    unittest.main()
