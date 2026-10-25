"""Controlled process tree for installer tests; no network or user config."""
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
from setup_process import PairingGateway, wait_gateway_ready
import configure

mode, directory = sys.argv[1:3]
base = Path(directory)
if mode == 'leaf':
    time.sleep(120)
elif mode == 'gateway':
    leaf = subprocess.Popen([sys.executable, '-X', 'utf8', __file__, 'leaf', directory])
    (base / 'tree.json').write_text(json.dumps({'gateway': __import__('os').getpid(), 'leaf': leaf.pid}))
    print('time=fixture level=INFO msg="qqbot: gateway READY" session_id=fixture', flush=True)
    time.sleep(120)
elif mode == 'parent':
    runtime = base / 'runtime'
    if (runtime / 'setup-pending.json').exists():
        configure.recover_setup(ROOT, runtime)
    else:
        configure.prepare(ROOT, runtime, base / 'cc', '12345678', 'test-only', False)
    with (base / 'gateway.log').open('ab') as log:
        with PairingGateway([sys.executable, '-X', 'utf8', __file__, 'gateway', directory], cwd=base, log=log) as child:
            wait_gateway_ready(child, base / 'gateway.log', offset=0, timeout=8)
            import ctypes
            k = ctypes.WinDLL('kernel32')
            k.GetConsoleWindow.restype = ctypes.c_void_p
            (base / 'parent-ready').write_text(json.dumps({'window': k.GetConsoleWindow()}))
            sys.stdin.readline()
