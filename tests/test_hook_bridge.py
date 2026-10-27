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
    MAX_HOOK_INPUT_BYTES,
    collect_hook_event,
    hook_event_to_signals,
    hook_main,
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

    def test_missing_hook_identity_is_dropped_instead_of_using_a_placeholder(self) -> None:
        self.assertEqual(hook_event_to_signals({'hook_event_name': 'UserPromptSubmit', 'session_id': 's'}), [])
        self.assertEqual(hook_event_to_signals({'hook_event_name': 'Stop', 'turn_id': 't'}), [])
        self.assertEqual(hook_event_to_signals({'hook_event_name': 'SessionStart'}), [])

    def test_collector_writes_one_atomic_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = collect_hook_event('{"hook_event_name":"Stop","session_id":"s","turn_id":"t"}', data_dir=Path(temporary))
            self.assertTrue(path.exists())
            self.assertEqual(len(list((Path(temporary) / "spool" / "incoming").glob("*.json"))), 1)

    def test_collector_accepts_limit_but_rejects_oversized_input_before_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = '{"hook_event_name":"Stop"}'
            raw = payload + " " * (MAX_HOOK_INPUT_BYTES - len(payload))
            path = collect_hook_event(raw, data_dir=root)
            self.assertTrue(path.exists())
            with patch("hook_bridge.json.loads") as loads, patch("hook_bridge.atomic_write_json") as write:
                with self.assertRaisesRegex(ValueError, "size limit"):
                    collect_hook_event(raw + "x", data_dir=root)
            loads.assert_not_called()
            write.assert_not_called()
            self.assertEqual(len(list((root / "spool" / "incoming").glob("*.json"))), 1)

    def test_collector_size_limit_counts_utf8_bytes(self) -> None:
        # Character count is below the limit, but this is more than 1 MiB in
        # UTF-8. Tool outputs and assistant messages often contain Chinese.
        raw = json.dumps({"hook_event_name": "Stop", "ignored": "中" * (MAX_HOOK_INPUT_BYTES // 3)}, ensure_ascii=False)
        self.assertLess(len(raw), MAX_HOOK_INPUT_BYTES)
        with tempfile.TemporaryDirectory() as temporary, patch("hook_bridge.json.loads") as loads:
            with self.assertRaisesRegex(ValueError, "size limit"):
                collect_hook_event(raw, data_dir=Path(temporary))
            self.assertEqual(list(Path(temporary).iterdir()), [])
        loads.assert_not_called()

    def test_hook_reads_stdin_with_a_bound_and_keeps_stop_response(self) -> None:
        class BoundedInput(io.StringIO):
            def read(self, size: int = -1) -> str:
                self.requested_size = size
                if size < 0:
                    raise AssertionError("unbounded hook read")
                return super().read(size)

        incoming = BoundedInput('{"hook_event_name":"Stop"}')
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary, patch("hook_bridge.default_data_dir", return_value=Path(temporary)), patch(
            "hook_bridge.sys.stdin", incoming
        ), patch("hook_bridge.sys.stdout", output), patch("hook_bridge.request_runtime_autostart"):
            self.assertEqual(hook_main(), 0)
            self.assertEqual(len(list((Path(temporary) / "spool" / "incoming").glob("*.json"))), 1)
        self.assertEqual(incoming.requested_size, MAX_HOOK_INPUT_BYTES + 1)
        self.assertEqual(json.loads(output.getvalue()), {"continue": True})

    def test_oversized_binary_hook_does_not_parse_collect_or_launch(self) -> None:
        # A valid prefix followed by extra input must be rejected in full,
        # never accepted as a truncated JSON event.
        raw = b'{"hook_event_name":"Stop"}'
        raw += b" " * (MAX_HOOK_INPUT_BYTES - len(raw)) + b"x"
        incoming = io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8")
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary, patch("hook_bridge.default_data_dir", return_value=Path(temporary)), patch(
            "hook_bridge.sys.stdin", incoming
        ), patch("hook_bridge.sys.stdout", output), patch("hook_bridge.json.loads") as loads, patch(
            "hook_bridge.collect_hook_event"
        ) as collect, patch("hook_bridge.request_runtime_autostart") as launch, patch("endpoint_hint.persist_native_endpoint") as hint:
            self.assertEqual(hook_main(), 0)
            self.assertEqual(list((Path(temporary) / "spool" / "incoming").glob("*.json")), [])
            diagnostic = (Path(temporary) / "logs" / "autostart.jsonl").read_text(encoding="utf-8")
            self.assertIn('"error_type": "ValueError"', diagnostic)
            loads.assert_not_called()
        collect.assert_not_called()
        launch.assert_not_called()
        hint.assert_not_called()
        self.assertEqual(json.loads(output.getvalue()), {"continue": True})

    def test_hook_stdin_error_cannot_break_turn_or_leak_contents(self) -> None:
        incoming = io.StringIO()
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary, patch("hook_bridge.default_data_dir", return_value=Path(temporary)), patch(
            "hook_bridge.sys.stdin", incoming
        ), patch.object(incoming, "read", side_effect=OSError("private payload")), patch("hook_bridge.sys.stdout", output):
            self.assertEqual(hook_main(), 0)
            diagnostic = (Path(temporary) / "logs" / "autostart.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("private payload", diagnostic)
            self.assertIn('"error_type": "OSError"', diagnostic)
        self.assertEqual(json.loads(output.getvalue()), {"continue": True})

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
        with tempfile.TemporaryDirectory() as temporary:
            # Shadow delivery has no CC Connect process to start. A healthy
            # monitor must not launch an ensure-runtime helper per hook event.
            with patch("launcher.read_monitor_pid", return_value=123), patch(
                "launcher.read_cc_connect_pid", return_value=None
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
