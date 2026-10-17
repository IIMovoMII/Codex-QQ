"""Isolated lifecycle fixture: stdin gates launch until assigned to a test job."""
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[2]
mode, home, data = sys.argv[1:]
sys.stdin.readline()
if mode == 'broker':
    command = [str(root / 'bin/CodexQQ助手.exe')]
else:
    command = [str(Path(sys.executable).with_name('pythonw.exe')), '-X', 'utf8',
               str(root / 'src/codex_desktop_weixin/cli.py'), 'app', '--brokered']
subprocess.Popen(command + ['--codex-home', home, '--data-dir', data,
                           '--headless', '--duration', '30'],
                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                 stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
sys.stdin.read()
