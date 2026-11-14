from __future__ import annotations
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import subprocess
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/codex_desktop_weixin'))
from package_install import InstallTransaction, sha, child, installer_identity

class PackageInstallTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt' and (Path(__file__).resolve().parents[1] / 'desktop-app/src-tauri/resources/python/python.exe').is_file(), 'packaged Windows runtime required')
    def test_real_recovery_worker_recovers_after_installer_ends(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            with patch.dict(os.environ, {'LOCALAPPDATA':str(root/'local')}):
                target=root/'program'
                target.mkdir()
                (target/'component.py').write_bytes(b'old')
                old={'version':'1.0.0','files':{'component.py':sha(target/'component.py')}}
                (target/'build-manifest.json').write_text(json.dumps(old))
                incoming=root/'incoming.json'
                incoming.write_text(json.dumps({'version':'1.0.1','files':{'component.py':'a'*64}}))
                bootstrap=Path(__file__).resolve().parents[1]/'desktop-app/src-tauri/resources/python'
                dummy=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],creationflags=subprocess.CREATE_NO_WINDOW)
                try:
                    tx=InstallTransaction(target)
                    tx.prepare(incoming,bootstrap,dummy.pid)
                    self.assertEqual(tx.read()[0]['installerCreated'],installer_identity(dummy.pid)[0])
                    (target/'component.py').write_bytes(b'interrupted-new')
                    dummy.terminate()
                    dummy.wait(timeout=5)
                    tx.watcher.wait(timeout=10)
                    self.assertEqual(tx.read()[0]['stage'],'rolled_back')
                    self.assertEqual((target/'component.py').read_bytes(),b'old')
                finally:
                    if dummy.poll() is None:
                        dummy.terminate();dummy.wait(timeout=5)
    def prepare(self, root):
        target = root / '安装 空间'
        target.mkdir()
        (target / 'component.py').write_bytes(b'old-program')
        (target / 'codex-qq-desktop.exe').write_bytes(b'old-native')
        (target / 'user-note.txt').write_bytes(b'not-owned')
        data = root / 'runtime'
        data.mkdir()
        (data / 'binding.json').write_bytes(b'original-binding')
        manifest = dict(version='1.0.0', files={'component.py':sha(target / 'component.py')})
        (target / 'build-manifest.json').write_text(json.dumps(manifest))
        incoming = root / 'incoming.json'
        incoming.write_text(json.dumps(dict(version='1.0.1', files={'new.py':'a'*64, 'component.py':'b'*64})))
        bootstrap = root / 'bootstrap'
        bootstrap.mkdir()
        (bootstrap / 'python.exe').write_bytes(b'isolated-test-runtime')
        tx = InstallTransaction(target, root / 'installer-state')
        with patch('package_install.registry_snapshot', return_value={}):
            tx.prepare(incoming, bootstrap, 123, watch=False)
        return tx, target, data

    @patch('package_install.registry_restore')
    def test_interrupted_install_restores_program_without_rolling_back_data(self, _registry):
        with tempfile.TemporaryDirectory() as temporary:
            tx, target, data = self.prepare(Path(temporary))
            (target / 'component.py').write_bytes(b'torn-new-program')
            (target / 'new.py').write_bytes(b'partial-new-file')
            (data / 'binding.json').write_bytes(b'current-binding')
            tx.rollback()
            tx.rollback()  # Recovery is idempotent, never runs an installer again.
            self.assertEqual((target / 'component.py').read_bytes(), b'old-program')
            self.assertFalse((target / 'new.py').exists())
            self.assertEqual((target / 'user-note.txt').read_bytes(), b'not-owned')
            self.assertEqual((data / 'binding.json').read_bytes(), b'current-binding')
            self.assertEqual(tx.read()[0]['stage'], 'rolled_back')

    def test_committed_install_is_not_reverted_by_late_watchdog(self):
        with tempfile.TemporaryDirectory() as temporary:
            tx, target, _ = self.prepare(Path(temporary))
            (target / 'component.py').write_bytes(b'new-program')
            tx.commit()
            tx.rollback()
            self.assertEqual((target / 'component.py').read_bytes(), b'new-program')

    def test_corrupt_backup_refuses_overwrite_and_paths_cannot_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tx, target, _ = self.prepare(root)
            _, backup = tx.read()
            (backup / 'files/component.py').write_bytes(b'tampered')
            with self.assertRaisesRegex(ValueError, 'backup_integrity'):
                tx.rollback()
            self.assertEqual((target / 'component.py').read_bytes(), b'old-program')
            for relative in ('../outside', 'C:/outside', '/outside', r'..\outside'):
                with self.assertRaises(ValueError): child(target, relative)
