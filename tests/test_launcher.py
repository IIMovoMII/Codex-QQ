from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PACKAGE_DIR = Path(__file__).resolve().parents[1] / "src" / "codex_desktop_weixin"
sys.path.insert(0, str(PACKAGE_DIR))

from launcher import (
    cc_connect_api_socket_path,
    cc_connect_api_status,
    cleanup_stale_cc_connect_socket,
    _capture_process_identity,
    read_monitor_pid,
    read_cc_connect_pid,
    write_monitor_pid,
    wait_for_codex_desktop,
)
from notifier import DeliverySettings, save_delivery_settings
from process_watch import ProcessStatus


class CCConnectLifecycleTests(unittest.TestCase):
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

            with patch("launcher._unlink_socket_path", side_effect=OSError("special endpoint")):
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
