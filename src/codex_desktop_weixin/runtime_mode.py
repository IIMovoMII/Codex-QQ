"""Lifecycle ownership, independent of delivery credentials and Codex config."""
from __future__ import annotations

import json
from pathlib import Path

from runtime_io import atomic_write_json


def runtime_mode(data_dir: Path) -> str:
    path = data_dir / 'state' / 'runtime-mode.json'
    try:
        if not 0 < path.stat().st_size <= 2048:
            return 'unknown'
        value = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        # A missing marker in an ALREADY migrated install is not a request to
        # return to hook-driven startup. Explicit app launch can repair it.
        return 'unknown' if (data_dir / 'state' / 'app.json').exists() else 'desktop'
    except (OSError, ValueError):
        return 'unknown'
    if (isinstance(value, dict) and value.get('version') == 1
            and value.get('mode') in ('desktop', 'independent')):
        return value['mode']
    return 'unknown'  # Never resurrect a manually closed app from broken state.


def set_runtime_mode(data_dir: Path, mode: str) -> None:
    if mode not in ('desktop', 'independent'):
        raise ValueError('invalid-runtime-mode')
    atomic_write_json(data_dir / 'state' / 'runtime-mode.json', {'version': 1, 'mode': mode})
