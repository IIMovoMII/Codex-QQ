from __future__ import annotations
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/codex_desktop_weixin'))
from desktop_uninstall import remove_integrations
from installer import build_hook_command, managed_hook_groups
from resident_app import startup_command


class Store:
    def __init__(self, value): self.value = copy.deepcopy(value)
    def read(self): return copy.deepcopy(self.value)
    def write(self, value): self.value = copy.deepcopy(value); return self.read()


class DesktopUninstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # Startup registration receives a canonical executable from the host.
        # Resolve TEMP's possible 8.3 alias before building that fixture.
        self.root = Path(self.temp.name).resolve()
        self.target = self.root / '安装 空间'
        self.target.mkdir()
        self.exe = self.target / 'codex-qq-desktop.exe'
        self.exe.write_bytes(b'owned-program')
        self.home = self.root / 'codex'
        self.home.mkdir()
        self.data = self.root / 'data'
        self.data.mkdir()
        (self.data / 'settings.json').write_bytes(b'original-binding')
        (self.home / 'config.toml').write_bytes(b'unchanged-codex-settings')
        self.store = Store({'run': {'kind': 1, 'value': startup_command(self.exe, start_hidden=True)},
                            'approval': {'kind': 3, 'value': 'AgAAAA=='}})

    def test_owned_startup_and_exact_hooks_removed_other_installation_and_data_kept(self):
        command = build_hook_command(self.target / 'backend/codex_desktop_weixin/cli.py')
        other = build_hook_command(self.root / 'other/backend/codex_desktop_weixin/cli.py')
        hooks = managed_hook_groups(command)
        hooks['Stop'][0]['custom'] = 'keep-group-metadata'
        hooks['Stop'][0]['hooks'].extend([{'command': 'other-tool'}, {'command': other}])
        path = self.home / 'hooks.json'
        path.write_text(json.dumps({'hooks': hooks, 'metadata': {'keep': True}}))
        result = remove_integrations(self.target, self.home, self.data, store=self.store)
        self.assertEqual(result, {'startupRemoved': True, 'hooksRemoved': True})
        self.assertEqual(self.store.read(), {'run': None, 'approval': None})
        self.assertEqual(json.loads(path.read_text()), {'metadata': {'keep': True}, 'hooks': {
            'Stop': [{'custom': 'keep-group-metadata', 'hooks': [{'command': 'other-tool'}, {'command': other}]}]}})
        self.assertEqual((self.data / 'settings.json').read_bytes(), b'original-binding')
        self.assertEqual((self.home / 'config.toml').read_bytes(), b'unchanged-codex-settings')
        self.assertEqual(remove_integrations(self.target, self.home, self.data, store=self.store),
                         {'startupRemoved': False, 'hooksRemoved': False})
        self.assertEqual(len(list((self.data / 'install-backups').iterdir())), 1)

    def test_other_install_startup_and_mixed_hook_commands_are_preserved(self):
        other_executable = self.root / 'other/app.exe'
        other_executable.parent.mkdir()
        other_executable.write_bytes(b'other-installation')
        self.store.value['run']['value'] = startup_command(other_executable, start_hidden=True)
        before = self.store.read()
        command = build_hook_command(self.target / 'backend/codex_desktop_weixin/cli.py')
        path = self.home / 'hooks.json'
        original = json.dumps({'hooks': {'Stop': [{'hooks': [{'command': command, 'commandWindows': 'unknown-command'}]}]}}).encode()
        path.write_bytes(original)
        self.assertEqual(remove_integrations(self.target, self.home, self.data, store=self.store),
                         {'startupRemoved': False, 'hooksRemoved': False})
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.store.read(), before)

    def test_unreadable_hooks_abort_before_removing_startup(self):
        (self.home / 'hooks.json').write_bytes(b'{broken')
        before = self.store.read()
        with self.assertRaises(ValueError):
            remove_integrations(self.target, self.home, self.data, store=self.store)
        self.assertEqual(self.store.read(), before)

    def test_failed_registry_write_restores_hooks_and_owned_startup(self):
        command = build_hook_command(self.target / 'backend/codex_desktop_weixin/cli.py')
        original = {'hooks': managed_hook_groups(command)}
        path = self.home / 'hooks.json'
        path.write_text(json.dumps(original))
        before = self.store.read()
        def failing(value):
            self.store.value = copy.deepcopy(value)
            if value['run'] is None: raise OSError('test-write-failed')
            return self.store.read()
        self.store.write = failing
        with self.assertRaises(OSError):
            remove_integrations(self.target, self.home, self.data, store=self.store)
        self.assertEqual(self.store.read(), before)
        self.assertEqual(json.loads(path.read_text()), original)

    def test_concurrent_edit_is_preserved_without_writing_integrations(self):
        before = self.store.read()
        changed = {**before, 'run': {'kind': 1, 'value': 'user-edit'}}
        with patch.object(self.store, 'read', side_effect=[before, changed, changed]):
            with self.assertRaisesRegex(ValueError, 'integration_changed'):
                remove_integrations(self.target, self.home, self.data, store=self.store)
        self.assertEqual(self.store.value, before)

    def test_partial_registry_write_is_restored_without_touching_other_entries(self):
        before = self.store.read()
        def partial(value):
            if value['run'] is None:
                self.store.value['run'] = None
                raise OSError('test-partial-write')
            self.store.value = copy.deepcopy(value)
            return self.store.read()
        self.store.write = partial
        with self.assertRaises(OSError):
            remove_integrations(self.target, self.home, self.data, store=self.store)
        self.assertEqual(self.store.read(), before)
