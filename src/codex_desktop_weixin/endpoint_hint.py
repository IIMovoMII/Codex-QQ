"""Small hook-provided endpoint hint. Not authentication or a server scan."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from runtime_io import atomic_write_json

PIPE_NAME = re.compile(r'^\\\\\.\\pipe\\codex-browser-use-[A-Za-z0-9_-]{20,100}$')


def persist_native_endpoint(data_dir: Path) -> None:
    candidate = os.environ.get('CODEX_APP_TOOLS_PIPE_PATH', '')
    if not PIPE_NAME.fullmatch(candidate):
        return
    path = data_dir / 'state' / 'native-app-tools.json'
    expected = {'version': 1, 'path': candidate}
    try:
        previous = json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
        if previous != expected:
            atomic_write_json(path, expected)
    except (OSError, ValueError):
        pass  # Optional hint must never block Codex or normal notifications.
