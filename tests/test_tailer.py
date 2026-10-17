from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parents[1] / "src" / "codex_desktop_weixin"
sys.path.insert(0, str(PACKAGE_DIR))

from models import SignalKind
from tailer import IncrementalTailer, canonical_path_key


def line(payload: dict, outer: str = "event_msg") -> bytes:
    return (json.dumps({"timestamp": "2026-08-23T00:00:00Z", "type": outer, "payload": payload}) + "\n").encode()


class TailerTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows short path aliases')
    def test_short_and_long_windows_paths_share_the_same_key(self):
        short = r'C:\Temp\EXAMPL~1\rollout.jsonl'
        long = r'C:\Temp\Example Person\rollout.jsonl'
        with patch('tailer.os.path.realpath', side_effect=lambda p: long if 'EXAMPL~1' in p else p):
            self.assertEqual(canonical_path_key(short), canonical_path_key(long))

    def test_reads_only_complete_new_lines_and_persists_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = root / "rollout-2026-01-01T00-00-00-11111111-1111-1111-1111-111111111111.jsonl"
            transcript.write_bytes(line({"type": "task_started", "turn_id": "t"}) + b'{"incomplete"')
            tailer = IncrementalTailer(root / "cursors.json")
            tailer.register_from_start(transcript)
            first = tailer.read_new_signals(transcript)
            self.assertEqual(first[0].kind, SignalKind.TURN_STARTED)
            first_offset = tailer.cursor_for(transcript).offset
            self.assertLess(first_offset, transcript.stat().st_size)
            with transcript.open("ab") as stream:
                stream.write(b':true}\n' + line({"type": "task_complete", "turn_id": "t", "last_agent_message": "done"}))
            second = tailer.read_new_signals(transcript)
            self.assertEqual(second[-1].kind, SignalKind.TURN_COMPLETED)
            reloaded = IncrementalTailer(root / "cursors.json")
            self.assertEqual(reloaded.read_new_signals(transcript), [])

    def test_lookback_starts_on_record_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = root / "rollout-2026-01-01T00-00-00-22222222-2222-2222-2222-222222222222.jsonl"
            old = line({"type": "task_complete", "turn_id": "old", "last_agent_message": "old"})
            current = line({"type": "task_started", "turn_id": "current"}) + line(
                {"type": "task_complete", "turn_id": "current", "last_agent_message": "done"}
            )
            transcript.write_bytes(old + current)
            tailer = IncrementalTailer(root / "cursors.json")
            tailer.register_with_lookback(transcript, lookback_bytes=len(current) + 5)
            signals = tailer.read_new_signals(transcript)
            self.assertEqual([item.turn_id for item in signals], ["current", "current"])

    @unittest.skipUnless(os.name == "nt", "Windows path aliases only")
    def test_windows_device_prefix_reuses_and_migrates_the_same_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "cursors.json"
            transcript = root / "rollout-2026-01-01T00-00-00-33333333-3333-3333-3333-333333333333.jsonl"
            transcript.write_bytes(line({"type": "task_started", "turn_id": "old"}))
            normal = str(transcript.resolve())
            device = "\\\\?\\" + normal
            identity = int(getattr(transcript.stat(), "st_ino", 0))
            state.write_text(
                json.dumps(
                    {
                        normal: {"offset": 1, "identity": identity, "session_id": "normal"},
                        device: {"offset": transcript.stat().st_size, "identity": identity, "session_id": "device"},
                    }
                ),
                encoding="utf-8",
            )

            tailer = IncrementalTailer(state)
            self.assertEqual(len(tailer.cursors), 1)
            self.assertEqual(tailer.cursor_for(transcript).offset, transcript.stat().st_size)
            self.assertTrue(tailer.is_known(Path(device)))
            self.assertEqual(canonical_path_key(normal), canonical_path_key(device))

            with transcript.open("ab") as stream:
                stream.write(line({"type": "task_complete", "turn_id": "new", "last_agent_message": "done"}))
            signals = tailer.read_new_signals(Path(device))
            self.assertEqual([item.turn_id for item in signals], ["new"])
            self.assertEqual(len(tailer.cursors), 1)


if __name__ == "__main__":
    unittest.main()
