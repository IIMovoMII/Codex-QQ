"""Remove only integrations pointing at this exact installation; keep user data."""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import sys
import uuid

PACKAGE = Path(__file__).resolve().parent
if str(PACKAGE) not in sys.path:
    sys.path.insert(0, str(PACKAGE))

from desktop_startup import SystemStartupStore
from installer import build_hook_command
from resident_app import startup_command
from runtime_io import atomic_write_json, default_data_dir


def remove_integrations(target: Path, codex_home: Path, data: Path, *, store=None):
    target = target.resolve()
    executable = target / 'codex-qq-desktop.exe'
    if not executable.is_file():
        raise ValueError('uninstall_program_missing')
    store = store or SystemStartupStore()
    registry = store.read()
    owned = registry['run'] in [
        {'kind': 1, 'value': startup_command(executable, start_hidden=hidden)}
        for hidden in (False, True)
    ]
    hooks_path = codex_home.resolve() / 'hooks.json'
    original = None
    cleaned = None
    if hooks_path.exists():
        if hooks_path.is_symlink() or hooks_path.stat().st_size > 1024 * 1024:
            raise ValueError('uninstall_hooks_invalid')
        original = hooks_path.read_bytes()
        document = json.loads(original.decode('utf-8-sig'))
        if not isinstance(document, dict):
            raise ValueError('uninstall_hooks_invalid')
        cleaned = copy.deepcopy(document)
        desired = build_hook_command(target / 'backend/codex_desktop_weixin/cli.py')
        hooks = cleaned.get('hooks')
        if isinstance(hooks, dict):
            for event, groups in list(hooks.items()):
                if not isinstance(groups, list):
                    continue
                kept = []
                for group in groups:
                    if not isinstance(group, dict) or not isinstance(group.get('hooks'), list):
                        kept.append(group)
                        continue
                    handlers = []
                    for handler in group['hooks']:
                        commands = [handler[key] for key in ('command', 'commandWindows', 'command_windows')
                                    if isinstance(handler, dict) and key in handler]
                        if not commands or any(command != desired for command in commands):
                            handlers.append(handler)
                    if handlers or not group['hooks']:
                        kept.append({**group, 'hooks': handlers})
                if kept:
                    hooks[event] = kept
                else:
                    del hooks[event]
        if cleaned == document:
            cleaned = None
    if not owned and cleaned is None:
        return {'startupRemoved': False, 'hooksRemoved': False}
    backup = data / 'install-backups' / ('desktop-uninstall-' + uuid.uuid4().hex)
    backup.mkdir(parents=True, exist_ok=False)
    atomic_write_json(backup / 'registry-before.json', registry)
    if cleaned is not None:
        (backup / 'hooks.before.json').write_bytes(original)
    after = {'run': None, 'approval': None}
    try:
        if store.read() != registry or (cleaned is not None and hooks_path.read_bytes() != original):
            raise ValueError('uninstall_integration_changed')
        if cleaned is not None:
            atomic_write_json(hooks_path, cleaned)
            if json.loads(hooks_path.read_text(encoding='utf-8')) != cleaned:
                raise OSError('uninstall_hooks_readback_failed')
        if owned and store.write(after) != after:
            raise OSError('uninstall_startup_readback_failed')
    except Exception:
        # Never overwrite a concurrent edit while recovering our own writes.
        if owned:
            current = store.read()
            if current != registry and all(current.get(key) in (registry[key], after[key]) for key in after):
                store.write(registry)
        if cleaned is not None and json.loads(hooks_path.read_text(encoding='utf-8-sig')) == cleaned:
            atomic_write_json(hooks_path, json.loads(original.decode('utf-8-sig')))
        raise
    return {'startupRemoved': owned, 'hooksRemoved': cleaned is not None}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('target', type=Path)
    args = parser.parse_args()
    try:
        remove_integrations(args.target, Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex'), default_data_dir())
    except (OSError, ValueError, KeyError, TypeError):
        print('uninstall_integrations_failed', file=sys.stderr)
        raise SystemExit(4)
