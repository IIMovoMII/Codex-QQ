from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
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

    def test_oversized_jsonl_record_is_skipped_and_following_records_are_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = root / 'rollout-2026-01-01T00-00-00-44444444-4444-4444-4444-444444444444.jsonl'
            transcript.write_bytes(b'{' + b'x' * 600 + b'}\n' + line({'type': 'task_started', 'turn_id': 'after-large'})
                                   + line({'type': 'task_complete', 'turn_id': 'after-large'}))
            tailer = IncrementalTailer(root / 'cursors.json')
            tailer.register_from_start(transcript)
            signals = []
            for _ in range(12):
                signals.extend(tailer.read_new_signals(transcript, max_bytes=128))
                if any(signal.kind == SignalKind.TURN_COMPLETED for signal in signals):
                    break
            self.assertEqual([signal.kind for signal in signals], [SignalKind.TURN_STARTED, SignalKind.TURN_COMPLETED])
            cursor = tailer.cursor_for(transcript)
            self.assertEqual(cursor.offset, transcript.stat().st_size)
            self.assertFalse(cursor.skipping_oversized_line)

    def test_corrupt_cold_row_is_discarded_without_blocking_other_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bad = root / 'bad-rollout.jsonl'; bad.write_text('', encoding='utf-8')
            good = root / 'good-rollout.jsonl'; good.write_text('', encoding='utf-8')
            state = root / 'cursors.json'
            cold = state.with_suffix('.sqlite')
            db = sqlite3.connect(cold)
            db.execute('CREATE TABLE cursors(path TEXT PRIMARY KEY, value TEXT NOT NULL, updated REAL NOT NULL)')
            db.executemany('INSERT INTO cursors VALUES(?,?,?)', [
                (canonical_path_key(bad), '{broken', time.time()),
                (canonical_path_key(good), json.dumps({
                    'offset': 7, 'identity': 1, 'session_id': 'good',
                    'turn_id': None, 'skipping_oversized_line': False,
                    'context_incomplete': False, 'needs_context': False,
                }), time.time()),
            ])
            db.commit(); db.close()
            tailer = IncrementalTailer(state)
            self.assertIsNone(tailer.cursor_for(bad))
            self.assertEqual(tailer.cursor_for(good).offset, 7)
            with patch.object(tailer, 'register_from_start', side_effect=AssertionError('unsafe replay')):
                tailer.read_new_signals(bad)
            check = sqlite3.connect(cold)
            try:
                self.assertEqual(check.execute('SELECT COUNT(*) FROM cursors').fetchone()[0], 1)
            finally:
                check.close()

    def test_corrupt_cold_database_is_quarantined_and_scan_can_continue(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = root / 'rollout.jsonl'; transcript.write_text('', encoding='utf-8')
            cold = (root / 'cursors.json').with_suffix('.sqlite')
            cold.write_bytes(b'not a sqlite database')
            tailer = IncrementalTailer(root / 'cursors.json')
            self.assertIsNone(tailer.cursor_for(transcript))
            with patch.object(tailer, 'register_from_start', side_effect=AssertionError('unsafe replay')):
                tailer.read_new_signals(transcript)
            self.assertFalse(cold.exists())
            self.assertEqual(len(list(root.glob('cursors.sqlite.corrupt-*'))), 1)

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
