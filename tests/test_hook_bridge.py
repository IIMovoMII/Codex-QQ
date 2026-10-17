from __future__ import annotations

import json
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PACKAGE_DIR = Path(__file__).resolve().parents[1] / "src" / "codex_desktop_weixin"
sys.path.insert(0, str(PACKAGE_DIR))

from hook_bridge import (
    collect_hook_event,
    hook_event_to_signals,
    request_runtime_autostart,
    sanitize_hook_payload,
    should_autostart_runtime,
)
from models import SignalKind


class HookBridgeTests(unittest.TestCase):
    def test_user_prompt_is_not_stored(self) -> None:
        event = sanitize_hook_payload(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "s",
                "turn_id": "t",
                "cwd": "D:\\Project",
                "prompt": "this must not be stored",
            }
        )
        self.assertNotIn("prompt", event)
        self.assertNotIn("this must not be stored", json.dumps(event))

    def test_permission_input_and_stop_mapping(self) -> None:
        permission = hook_event_to_signals(sanitize_hook_payload({"hook_event_name": "PermissionRequest", "session_id": "s", "turn_id": "t", "tool_name": "Bash"}))
        self.assertEqual(permission[0].kind, SignalKind.PERMISSION_REQUESTED)
        waiting = hook_event_to_signals(sanitize_hook_payload({"hook_event_name": "PreToolUse", "session_id": "s", "turn_id": "t", "tool_name": "request_user_input"}))
        self.assertEqual(waiting[0].kind, SignalKind.USER_INPUT_REQUESTED)
        stopped = hook_event_to_signals(sanitize_hook_payload({"hook_event_name": "Stop", "session_id": "s", "turn_id": "t", "last_assistant_message": "done"}))
        self.assertEqual([item.kind for item in stopped], [SignalKind.ASSISTANT_MESSAGE, SignalKind.STOP_OBSERVED])

    def test_collector_writes_one_atomic_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = collect_hook_event('{"hook_event_name":"Stop","session_id":"s","turn_id":"t"}', data_dir=Path(temporary))
            self.assertTrue(path.exists())
            self.assertEqual(len(list((Path(temporary) / "spool" / "incoming").glob("*.json"))), 1)

    def test_runtime_autostart_only_for_desktop_hooks_when_needed(self) -> None:
        self.assertTrue(
            should_autostart_runtime(
                "SessionStart",
                monitor_running=False,
                cc_connect_running=False,
                desktop_alive=True,
            )
        )
        self.assertFalse(
            should_autostart_runtime(
                "UserPromptSubmit",
                monitor_running=True,
                cc_connect_running=True,
                desktop_alive=True,
            )
        )

    def test_runtime_autostart_delegates_process_wait_to_helper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch("launcher.read_monitor_pid", return_value=None), patch(
                "launcher.read_cc_connect_pid", return_value=None
            ), patch("hook_bridge.subprocess.Popen") as popen:
                self.assertTrue(request_runtime_autostart("SessionStart", data_dir=root))

            command = popen.call_args.args[0]
            self.assertIn("ensure-runtime", command)
            self.assertIn("--wait-seconds", command)
            self.assertIn("--trigger-event", command)
            self.assertIn("--transcript-path", command)

    def test_runtime_autostart_does_not_spawn_when_both_services_are_alive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch("launcher.read_monitor_pid", return_value=123), patch(
                "launcher.read_cc_connect_pid", return_value=456
            ), patch("hook_bridge.subprocess.Popen") as popen:
                self.assertFalse(request_runtime_autostart("UserPromptSubmit", data_dir=Path(temporary)))
            popen.assert_not_called()
        self.assertFalse(
            should_autostart_runtime(
                "SessionEnd",
                monitor_running=False,
                cc_connect_running=False,
                desktop_alive=True,
            )
        )
        self.assertFalse(
            should_autostart_runtime(
                "SessionStart",
                monitor_running=False,
                cc_connect_running=False,
                desktop_alive=False,
            )
        )


if __name__ == "__main__":
    unittest.main()
