from __future__ import annotations
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/codex_desktop_weixin'))
from desktop_bridge import DesktopBridge
from package_install import InstallTransaction

class DesktopUpdateTests(unittest.TestCase):
    def test_update_receipt_survives_restart_and_requires_matching_committed_install(self):
        with tempfile.TemporaryDirectory() as temporary, patch('package_install.registry_snapshot', return_value={}):
            root = Path(temporary)
            with patch.dict(os.environ, {'LOCALAPPDATA': str(root / 'local')}):
                program = root / 'program'
                program.mkdir()
                manifest = program / 'build-manifest.json'
                manifest.write_text(json.dumps({'version':'1.0.0', 'files':{}}))
                bridge = DesktopBridge(root/'codex', root/'data', program/'codex-qq-desktop.exe')
                bridge.packaged = True
                request = dict(version=1, requestId='upgrade', command='begin_update',
                               params=dict(version='1.0.1', sourceVersion='1.0.0', sha256='a'*64))
                self.assertEqual(bridge.handle(request)['status'], 'confirmed')
                self.assertTrue(bridge.operations.has_unresolved())
                self.assertEqual(bridge.handle(request)['status'], 'unknown')
                reopened = DesktopBridge(root/'codex', root/'data', program/'codex-qq-desktop.exe')
                reopened.update_receipt.reconcile()  # No installer was started.
                self.assertEqual(reopened.operations.get('upgrade')['status'], 'unknown')
                incoming = root / 'incoming.json'
                incoming.write_text(json.dumps({'version':'1.0.1', 'files':{}}))
                bootstrap = root/'bootstrap'
                bootstrap.mkdir()
                tx = InstallTransaction(program)
                tx.prepare(incoming, bootstrap, 123, watch=False)
                manifest.write_text(json.dumps({'version':'1.0.1', 'files':{}}))
                reopened.update_receipt.reconcile()  # New files alone do not prove commit.
                self.assertTrue(reopened.operations.has_unresolved())
                tx.commit()
                reopened.update_receipt.reconcile()
                self.assertEqual(reopened.operations.get('upgrade')['data']['update']['status'], 'installed')
                self.assertFalse(reopened.operations.has_unresolved())

    @patch('package_install.registry_restore')
    @patch('package_install.registry_snapshot', return_value={})
    def test_rollback_finishes_original_intent_without_reinstalling(self, *_):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.dict(os.environ, {'LOCALAPPDATA': str(root/'local')}):
                program = root/'program'
                program.mkdir()
                (program/'build-manifest.json').write_text(json.dumps({'version':'1.0.0', 'files':{}}))
                bridge = DesktopBridge(root/'codex', root/'data', program/'codex-qq-desktop.exe')
                bridge.packaged = True
                request = dict(version=1, requestId='rollback', command='begin_update',
                               params=dict(version='1.0.1', sourceVersion='1.0.0', sha256='a'*64))
                bridge.handle(request)
                incoming = root/'incoming.json'
                incoming.write_text(json.dumps({'version':'1.0.1', 'files':{}}))
                bootstrap=root/'bootstrap'
                bootstrap.mkdir()
                tx=InstallTransaction(program)
                tx.prepare(incoming, bootstrap, 123, watch=False)
                (program/'build-manifest.json').write_text(json.dumps({'version':'1.0.1', 'files':{}}))
                tx.rollback()
                bridge.update_receipt.reconcile()
                self.assertEqual(bridge.operations.get('rollback')['errorCode'], 'update_rolled_back')
                self.assertFalse(bridge.operations.has_unresolved())
                self.assertEqual(bridge.handle(request)['errorCode'], 'update_rolled_back')
