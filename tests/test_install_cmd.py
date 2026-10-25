from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(os.name == 'nt', 'requires Windows cmd.exe and Python launcher')
class InstallCommandTests(unittest.TestCase):
    def test_wizard_runs_once_and_its_exit_code_is_preserved(self) -> None:
        if not shutil.which('py') or not shutil.which('python'):
            self.skipTest('requires both py and python to exercise interpreter fallback')
        source = Path(__file__).resolve().parents[1] / 'install.cmd'
        for code in (0, 7):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                shutil.copyfile(source, root / 'install.cmd')
                (root / 'tools').mkdir()
                # This isolated fake wizard cannot start services or read user
                # configuration; both interpreter commands would reach it.
                (root / 'tools' / 'install_app.py').write_text(
                    'from pathlib import Path\n'
                    'with (Path(__file__).resolve().parents[1] / "calls.txt").open("a") as stream:\n'
                    '    stream.write("called\\n")\n'
                    f'raise SystemExit({code})\n', encoding='utf-8')
                result = subprocess.run(
                    [os.environ.get('COMSPEC', 'cmd.exe'), '/d', '/c', 'install.cmd', '--check'],
                    cwd=root, capture_output=True, timeout=30,
                )
                self.assertEqual(result.returncode, code, result.stderr.decode('utf-8', 'replace'))
                self.assertEqual((root / 'calls.txt').read_text().splitlines(), ['called'])
                self.assertNotIn(b'Python 3.11+ is required', result.stdout)


if __name__ == '__main__':
    unittest.main()
