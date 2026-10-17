from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parents[1] / "src" / "codex_desktop_weixin"
sys.path.insert(0, str(PACKAGE_DIR))

from models import SignalKind
from rollout import parse_rollout_line


class RolloutParserTests(unittest.TestCase):
    def parse(self, payload: dict, outer: str = "event_msg"):
        line = json.dumps({"timestamp": "2026-08-23T01:02:03Z", "type": outer, "payload": payload})
        return parse_rollout_line(line, session_id="s", transcript_path="x.jsonl")

    def test_failed_completion_carries_error(self) -> None:
        signals = self.parse({"type": "task_complete", "turn_id": "t", "last_agent_message": "", "error": {"message": "401"}})
        self.assertEqual(signals[0].kind, SignalKind.TURN_COMPLETED)
        self.assertIsNotNone(signals[0].data["error"])

    def test_success_and_abort(self) -> None:
        self.assertEqual(self.parse({"type": "task_complete", "turn_id": "t", "last_agent_message": "done"})[0].kind, SignalKind.TURN_COMPLETED)
        self.assertEqual(self.parse({"type": "turn_aborted", "turn_id": "t", "reason": "interrupted"})[0].kind, SignalKind.TURN_ABORTED)

    def test_historical_user_input_is_not_treated_as_live_wait(self) -> None:
        signals = self.parse({"type": "function_call", "name": "request_user_input", "call_id": "c"}, outer="response_item")
        self.assertEqual(signals[0].kind, SignalKind.USER_INPUT_REQUESTED)
        self.assertTrue(signals[0].data["historical"])


if __name__ == "__main__":
    unittest.main()

