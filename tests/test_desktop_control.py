from __future__ import annotations

import json
import struct
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PACKAGE_DIR = Path(__file__).resolve().parents[1] / "src" / "codex_desktop_weixin"
sys.path.insert(0, str(PACKAGE_DIR))

from desktop_control import (
    DesktopClient, DesktopControlError, MAX_FRAME_BYTES, STOP_METHOD,
    STOP_VERSION, WindowsDesktopPipe, encode_frame, pause_desktop,
)


THREAD = "00000000-0000-4000-8000-000000000001"
TURN = "00000000-0000-4000-8000-000000000002"


class FakePipe:
    def __init__(self, *, result: dict | None = None, failure: str = "",
                 fail_method: str = STOP_METHOD, timeout: bool = False) -> None:
        self.writes: list[dict] = []
        self.pending: list[dict] = []
        self.result = result if result is not None else {"ok": True, "interruptedTurnId": TURN}
        self.failure = failure
        self.fail_method = fail_method
        self.timeout = timeout
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True

    def write(self, message: dict) -> None:
        self.writes.append(message)
        if message["type"] != "request":
            return
        method = message["method"]
        response = {
            "type": "response", "requestId": message["requestId"],
            "resultType": "success", "method": method,
            "handledByClientId": "desktop-owner",
            "result": {},
        }
        if method == "initialize":
            response["result"] = {"clientId": "bridge-client"}
        elif method == "thread-owner-discovery":
            response["result"] = {"supportsUntrustedAppInput": True}
        elif method == STOP_METHOD:
            response["result"] = self.result
        if method == self.fail_method and self.failure:
            response.update(resultType="error", error=self.failure)
        self.pending.append(response)

    def read(self) -> dict:
        if self.timeout and self.writes[-1].get("method") == self.fail_method:
            raise TimeoutError
        return self.pending.pop(0)


class DesktopControlTests(unittest.TestCase):
    def test_native_user_stop_targets_selected_owner_without_starting_an_agent(self) -> None:
        pipe = FakePipe()
        ok, detail = pause_desktop(THREAD, pipe_factory=lambda: pipe)
        self.assertTrue(ok)
        self.assertIn("原生停止", detail)
        self.assertEqual([m["method"] for m in pipe.writes], [
            "initialize", "thread-owner-discovery", STOP_METHOD,
        ])
        stop = pipe.writes[-1]
        self.assertEqual(stop["params"], {"conversationId": THREAD, "mode": "user-stop"})
        self.assertEqual(stop["targetClientId"], "desktop-owner")
        self.assertEqual(stop["sourceClientId"], "bridge-client")
        self.assertEqual(stop["version"], STOP_VERSION)
        self.assertTrue(pipe.closed)

    def test_idle_conversation_does_not_claim_it_interrupted_a_turn(self) -> None:
        pipe = FakePipe(result={"ok": True, "interruptedTurnId": None})
        ok, detail = pause_desktop(THREAD, pipe_factory=lambda: pipe)
        self.assertTrue(ok)
        self.assertIn("没有正在运行", detail)

    def test_unknown_thread_cannot_fall_back_to_other_window(self) -> None:
        pipe = FakePipe(failure="no-client-found", fail_method="thread-owner-discovery")
        ok, detail = pause_desktop(THREAD, pipe_factory=lambda: pipe)
        self.assertFalse(ok)
        self.assertIn("打开", detail)
        self.assertNotIn(STOP_METHOD, [m["method"] for m in pipe.writes])
        self.assertTrue(pipe.closed)

    def test_timeout_after_stop_is_unknown_not_success_and_never_retries(self) -> None:
        pipe = FakePipe(timeout=True)
        ok, detail = pause_desktop(THREAD, pipe_factory=lambda: pipe)
        self.assertFalse(ok)
        self.assertIn("可能已生效", detail)
        self.assertEqual(sum(m["method"] == STOP_METHOD for m in pipe.writes), 1)
        self.assertTrue(pipe.closed)

    def test_protocol_change_fails_closed(self) -> None:
        pipe = FakePipe(failure="request-version-mismatch")
        ok, detail = pause_desktop(THREAD, pipe_factory=lambda: pipe)
        self.assertFalse(ok)
        self.assertIn("版本", detail)
        self.assertEqual(sum(m["method"] == STOP_METHOD for m in pipe.writes), 1)

    def test_owner_disappearing_before_stop_does_not_guess_another_target(self) -> None:
        pipe = FakePipe(failure="no-client-found")
        ok, detail = pause_desktop(THREAD, pipe_factory=lambda: pipe)
        self.assertFalse(ok)
        self.assertIn("窗口状态或接口版本", detail)
        self.assertEqual(sum(m["method"] == STOP_METHOD for m in pipe.writes), 1)

    def test_missing_stop_confirmation_is_not_a_success(self) -> None:
        pipe = FakePipe(result={"ok": True})
        ok, detail = pause_desktop(THREAD, pipe_factory=lambda: pipe)
        self.assertFalse(ok)
        self.assertIn("确认", detail)

    def test_goal_pause_failure_is_reported_even_if_current_turn_stopped(self) -> None:
        pipe = FakePipe(result={"ok": True, "interruptedTurnId": TURN, "goalPauseError": "failed"})
        ok, detail = pause_desktop(THREAD, pipe_factory=lambda: pipe)
        self.assertTrue(ok)
        self.assertIn("可能还会自动续跑", detail)

    def test_invalid_target_does_not_open_pipe(self) -> None:
        with patch("desktop_control.WindowsDesktopPipe") as factory:
            ok, _detail = pause_desktop("../invalid", pipe_factory=factory)
        self.assertFalse(ok)
        factory.assert_not_called()

    def test_client_filters_broadcasts_unrelated_responses_and_discovery(self) -> None:
        pipe = FakePipe()
        pipe.pending.extend([
            {"type": "broadcast", "method": "thread-stream-state-changed"},
            {"type": "response", "requestId": "unrelated", "resultType": "success"},
            {"type": "client-discovery-request", "requestId": "other-window"},
        ])
        client = DesktopClient(pipe)
        client.initialize()
        self.assertEqual(client.client_id, "bridge-client")
        self.assertEqual(pipe.writes[-1], {
            "type": "client-discovery-response", "requestId": "other-window",
            "response": {"canHandle": False},
        })

    def test_utf8_frames_count_bytes_and_round_trip_chinese_and_emoji(self) -> None:
        message = {"type": "request", "params": {"text": "暂停任务😀\n中文"}}
        frame = encode_frame(message)
        self.assertEqual(struct.unpack("<I", frame[:4])[0], len(frame) - 4)
        self.assertEqual(json.loads(frame[4:].decode("utf-8")), message)
        pipe = WindowsDesktopPipe.__new__(WindowsDesktopPipe)
        with patch.object(pipe, "_read_exact", side_effect=[frame[:4], frame[4:]]):
            self.assertEqual(pipe.read(), message)

    def test_invalid_frames_are_rejected_without_large_allocation(self) -> None:
        pipe = WindowsDesktopPipe.__new__(WindowsDesktopPipe)
        with patch.object(pipe, "_read_exact", return_value=struct.pack("<I", MAX_FRAME_BYTES + 1)) as read:
            with self.assertRaises(DesktopControlError):
                pipe.read()
        read.assert_called_once_with(4)


if __name__ == "__main__":
    unittest.main()
