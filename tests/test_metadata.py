from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parents[1] / "src" / "codex_desktop_weixin"
sys.path.insert(0, str(PACKAGE_DIR))

from metadata import ThreadMetadataResolver


class MetadataTests(unittest.TestCase):
    def test_reads_title_from_database_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            connection = sqlite3.connect(home / "state_5.sqlite")
            connection.execute(
                "CREATE TABLE threads (id TEXT PRIMARY KEY, name TEXT, title TEXT, first_user_message TEXT, cwd TEXT)"
            )
            connection.execute(
                "INSERT INTO threads VALUES (?, ?, ?, ?, ?)",
                ("session", "任务名", "旧标题", "首条消息", "D:/Project"),
            )
            connection.commit()
            connection.close()
            info = ThreadMetadataResolver(home).resolve("session")
            self.assertEqual(info.title, "任务名")


if __name__ == "__main__":
    unittest.main()
