"""Offline installed-package probe; never opens QQ, changes settings or starts a worker."""
from __future__ import annotations
import hashlib
import importlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

def check(root: Path) -> None:
    manifest=json.loads((root/'build-manifest.json').read_text(encoding='utf-8'))
    for name, expected in manifest['files'].items():
        path=root/name
        if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink():
            raise ValueError('resource_path_invalid')
        if hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
            raise ValueError('resource_hash_invalid')
    for name in ('desktop_bridge','setup_service','configuration_service','desktop_uninstall','monitor','notifier','native_app_tools'):
        importlib.import_module('codex_desktop_weixin.'+name)
    with sqlite3.connect(':memory:') as db:
        assert db.execute('SELECT json_valid(?)', ('{}',)).fetchone()[0]==1
    result=subprocess.run([str(root/'gateway.exe'),'--version'],capture_output=True,timeout=10,
                          creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if result.returncode or b'1.5.0-desktop-control.6' not in result.stdout+result.stderr:
        raise ValueError('gateway_version_invalid')
    # Exercise the exact script entry points in a fresh isolated interpreter.
    # In-process package imports can hide missing embedded-Python search paths.
    with tempfile.TemporaryDirectory(prefix='codex-qq-package-probe-') as temporary:
        command = [str(root/'python/python.exe'), '-I', '-B', '-X', 'utf8', '-u']
        env = {**os.environ, 'CC_DATA_DIR': str(Path(temporary)/'qq')}
        request = b'{"jsonrpc":"2.0","id":"package-probe","method":"initialize","params":{}}\n'
        for entry in ('backend/codex_desktop_weixin/qq_desktop_bridge.py', 'tools/pairing_agent.py'):
            result = subprocess.run([*command, str(root/entry)], input=request,
                                    capture_output=True, timeout=10, cwd=temporary, env=env,
                                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            if result.returncode or json.loads(result.stdout).get('id') != 'package-probe':
                raise ValueError('acp_entry_not_ready')
        result = subprocess.run([*command, str(root/'backend/codex_desktop_weixin/cli.py'), '--help'],
                                capture_output=True, timeout=10, cwd=temporary, env=env,
                                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if result.returncode:
            raise ValueError('hook_entry_not_ready')
        result = subprocess.run([*command, str(root/'backend/codex_desktop_weixin/desktop_uninstall.py'), '--help'],
                                capture_output=True, timeout=10, cwd=temporary,
                                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if result.returncode:
            raise ValueError('uninstall_entry_not_ready')

if __name__=='__main__':
    try:
        check(Path(sys.argv[1]).resolve())
    except Exception as error:
        print(type(error).__name__) # Never print arbitrary credential-bearing exception details.
        raise SystemExit(3)
    print('package_health_ok')
