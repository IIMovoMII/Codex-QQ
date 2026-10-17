"""Native endpoint names and optional hook hints; neither authenticates a server."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from runtime_io import atomic_write_json

PIPE_NAME = re.compile(r'^\\\\\.\\pipe\\codex-(?:app-tools|browser-use)-[A-Za-z0-9_-]{20,100}$')
MAX_ENDPOINT_CANDIDATES = 16


def discover_native_endpoints() -> tuple[str, ...]:
    """On-demand local inventory, never a timer or a general-purpose pipe probe.

    Names are hints only. The caller must check the actual server process before
    sending even tools/list. Excess candidates fail closed rather than sampling
    an arbitrary subset. Current app-tool names precede legacy browser names.
    """
    if os.name != 'nt':
        return ()
    try:
        root = '\\\\.\\pipe\\'
        paths = [root + name for name in os.listdir(root)]
    except OSError:
        return ()
    paths = [path for path in paths if PIPE_NAME.fullmatch(path)]
    if len(paths) > MAX_ENDPOINT_CANDIDATES:
        return ()
    return tuple(sorted(paths, key=lambda p: ('codex-app-tools-' not in p, p)))


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
