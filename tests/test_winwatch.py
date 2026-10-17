from __future__ import annotations

import os
import queue
import sys
import tempfile
import time
import unittest
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parents[1] / "src" / "codex_desktop_weixin"
sys.path.insert(0, str(PACKAGE_DIR))

from winwatch import WindowsDirectoryWatcher


@unittest.skipUnless(os.name == "nt", "Windows watcher test")
class WindowsWatcherTests(unittest.TestCase):
    def test_receives_file_change_without_directory_polling(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            events = queue.Queue()
            watcher = WindowsDirectoryWatcher(root, events, recursive=True)
            watcher.start()
            try:
                target = root / "事件.json"
                target.write_text("{}", encoding="utf-8")
                deadline = time.monotonic() + 3
                seen = False
                while time.monotonic() < deadline:
                    try:
                        event = events.get(timeout=0.2)
                    except queue.Empty:
                        continue
                    if event.path and event.path.name == target.name:
                        seen = True
                        break
                self.assertTrue(seen)
            finally:
                watcher.close()


if __name__ == "__main__":
    unittest.main()

