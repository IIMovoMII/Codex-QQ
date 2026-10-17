from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from errors import redact_error_text


@dataclass(frozen=True)
class ThreadMetadata:
    title: str = ""
    cwd: str = ""


class ThreadMetadataResolver:
    """Read display-only thread metadata without writing Codex databases."""

    def __init__(self, codex_home: Path) -> None:
        self.database_paths = [codex_home / "state_5.sqlite", codex_home / "sqlite" / "state_5.sqlite"]

    def resolve(self, session_id: str) -> ThreadMetadata:
        for path in self.database_paths:
            if not path.exists():
                continue
            try:
                connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2)
                try:
                    row = connection.execute(
                        "SELECT name, title, first_user_message, cwd FROM threads WHERE id = ? LIMIT 1",
                        (session_id,),
                    ).fetchone()
                finally:
                    connection.close()
            except (sqlite3.Error, OSError):
                continue
            if row:
                preferred = next((value for value in row[:3] if isinstance(value, str) and value.strip()), "")
                return ThreadMetadata(
                    title=redact_error_text(preferred.strip(), limit=48) if preferred else "",
                    cwd=str(row[3] or ""),
                )
        return ThreadMetadata()
