from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any


def default_data_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "CodexDesktopWeixin"
    return Path.home() / "AppData" / "Local" / "CodexDesktopWeixin"


def ensure_runtime_dirs(data_dir: Path) -> dict[str, Path]:
    paths = {
        "root": data_dir,
        "incoming": data_dir / "spool" / "incoming",
        "processed": data_dir / "spool" / "processed",
        "failed": data_dir / "spool" / "failed",
        "state": data_dir / "state",
        "logs": data_dir / "logs",
        "outbox": data_dir / "outbox",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # On Windows a simultaneous ordinary reader can briefly open without
        # FILE_SHARE_DELETE. Keep the already flushed temporary file and retry
        # only the atomic rename, bounded to 150 ms; never replay a command.
        delays = (0.01, 0.02, 0.04, 0.08)
        for attempt in range(len(delays) + 1):
            try:
                os.replace(temporary, path)
                break
            except PermissionError as error:
                if getattr(error, 'winerror', None) not in (5, 32, 33) or attempt == len(delays):
                    raise
                time.sleep(delays[attempt])
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def append_json_line(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    with path.open("ab") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def acquire_file_lock(path: Path) -> tuple[int, Path] | None:
    """OS-owned lifetime lock; never unlink the file while other users wait."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.name == 'nt':
            import msvcrt
            if os.path.getsize(path) == 0:
                os.write(descriptor, b'0')
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return descriptor, path
    except OSError:
        os.close(descriptor)
        return None

