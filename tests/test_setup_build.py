"""Real .NET compiler validation, always outside the live installation."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from setup_diagnostics import InstallFailure, run_logged


@unittest.skipUnless(os.name == 'nt', 'requires Windows/.NET compiler')
class SetupBuildTests(unittest.TestCase):
    def setUp(self):
        compiler = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        if not compiler.exists():
            self.skipTest('existing .NET compiler is unavailable; do not install one for a test')

    def test_real_compiler_builds_only_in_temporary_output(self):
        live = ROOT / 'bin/CodexQQ助手.exe'
        before = hashlib.sha256(live.read_bytes()).hexdigest() if live.exists() else None
        with tempfile.TemporaryDirectory(prefix='codex-qq-build-') as temp:
            result = subprocess.run([sys.executable, '-X', 'utf8', str(ROOT / 'tools/build_resident_app.py'),
                                     '--output-dir', temp], capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', 'replace'))
            target = Path(temp) / 'CodexQQ助手.exe'
            self.assertTrue(target.read_bytes().startswith(b'MZ'))
            state = json.loads((Path(temp) / 'app-runtime.json').read_text(encoding='utf-8'))
            self.assertEqual(state['launcher_sha256'], hashlib.sha256(target.read_bytes()).hexdigest())
        self.assertEqual(before, hashlib.sha256(live.read_bytes()).hexdigest() if live.exists() else None)

    def test_real_compiler_error_reaches_installer_log_and_summary(self):
        script = '''
import pathlib, subprocess, sys
sys.path.insert(0, sys.argv[1])
import build_resident_app as build
build.ROOT = pathlib.Path(sys.argv[2])
sys.argv = ["build", "--output-dir", str(build.ROOT / "output")]
try:
    build.main()
except subprocess.CalledProcessError as error:
    raise SystemExit(error.returncode)
'''
        with tempfile.TemporaryDirectory(prefix='codex-qq-build-error-') as temp:
            base = Path(temp)
            (base / 'tools').mkdir()
            (base / 'tools/ResidentLauncher.cs').write_text('这不是合法的 C# 源码;', encoding='utf-8')
            with self.assertRaises(InstallFailure) as caught:
                run_logged([sys.executable, '-X', 'utf8', '-c', script, str(ROOT / 'tools'), temp],
                           stage='启动器编译', logs=base / 'logs')
            self.assertEqual(caught.exception.code, 1)
            self.assertIn('error CS', str(caught.exception))
            self.assertIn('error CS', caught.exception.log_path.read_text(encoding='utf-8'))
            self.assertFalse((base / 'output/CodexQQ助手.exe').exists())


if __name__ == '__main__':
    unittest.main()
