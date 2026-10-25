"""Fault injection uses temporary installs only; never the logged-in user's data."""
import contextlib
import io
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import configure
import install_app


class SetupRecoveryTests(unittest.TestCase):
    def fixture(self, base):
        root = Path(base)
        binary = root / install_app.GATEWAY_RELATIVE
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b'not-executed')
        return root, root / 'runtime'

    def test_pending_write_failure_can_restart_without_orphan_config(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = root / 'runtime'
            with patch.object(configure, 'atomic_write_json', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    configure.prepare(root, runtime, root / 'cc', '12345678', 'test-only', False)
            self.assertEqual(install_app.setup_state(root, runtime), 'new')
            configure.prepare(root, runtime, root / 'cc', '12345678', 'test-only', False)
            self.assertEqual(install_app.setup_state(root, runtime), 'pairing')

    def test_pairing_waits_for_ready_before_asking_for_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = root / 'runtime'
            configure.prepare(root, runtime, root / 'cc', '12345678', 'test-only', False)
            events = []
            with patch.object(install_app, 'PairingGateway') as gateway, \
                    patch.object(install_app, 'wait_gateway_ready', side_effect=lambda *a, **k: events.append('ready')), \
                    contextlib.redirect_stdout(io.StringIO()):
                gateway.return_value.__enter__.return_value.poll.return_value = None
                def ask(_):
                    events.append('ask')
                    return '取消'
                self.assertFalse(install_app.pair(root, runtime, ask=ask))
            self.assertEqual(events, ['ready', 'ask'])

    def test_publish_failure_and_crash_after_config_publish_recover(self):
        for crash_after_publish in (False, True):
            with self.subTest(after=crash_after_publish), tempfile.TemporaryDirectory() as temp:
                root, runtime = self.fixture(temp)
                original = configure.publish_new
                def publish(staged, target):
                    if target.name == 'cc-connect.toml':
                        if crash_after_publish:
                            original(staged, target)
                        raise KeyboardInterrupt('simulated process interruption')
                    return original(staged, target)
                with patch.object(configure, 'publish_new', side_effect=publish), self.assertRaises(KeyboardInterrupt):
                    configure.prepare(root, runtime, root / 'cc', '12345678', 'test-only', False)
                before = (runtime / 'setup-pending.json').read_bytes()
                self.assertEqual(install_app.setup_state(root, runtime), 'preparing')
                self.assertEqual(before, (runtime / 'setup-pending.json').read_bytes())  # check is read-only
                configure.recover_setup(root, runtime)
                self.assertEqual(install_app.setup_state(root, runtime), 'pairing')
                config = (runtime / 'cc-connect.toml').read_bytes()
                configure.recover_setup(root, runtime)
                self.assertEqual(config, (runtime / 'cc-connect.toml').read_bytes())

    def test_config_staging_write_failure_leaves_new_install(self):
        with tempfile.TemporaryDirectory() as temp:
            root, runtime = self.fixture(temp)
            with patch.object(configure.os, 'fsync', side_effect=OSError('disk full')), self.assertRaises(OSError):
                configure.prepare(root, runtime, root / 'cc', '12345678', 'test-only', False)
            self.assertEqual(install_app.setup_state(root, runtime), 'new')
            self.assertEqual(list(runtime.glob('.setup-*')), [])

    def test_pending_atomic_replace_failure_is_restartable(self):
        with tempfile.TemporaryDirectory() as temp:
            root, runtime = self.fixture(temp)
            with patch('runtime_io.os.replace', side_effect=OSError('replace failed')), self.assertRaises(OSError):
                configure.prepare(root, runtime, root / 'cc', '12345678', 'test-only', False)
            self.assertEqual(install_app.setup_state(root, runtime), 'new')
            configure.prepare(root, runtime, root / 'cc', '12345678', 'test-only', False)
            self.assertEqual(install_app.setup_state(root, runtime), 'pairing')

    def test_bound_config_settings_and_pending_cleanup_interruptions_recover(self):
        for point in ('config', 'settings', 'unlink'):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as temp:
                root, runtime = self.fixture(temp)
                configure.prepare(root, runtime, root / 'cc', '12345678', 'test-only', False)
                real_replace, real_publish, real_unlink = os.replace, configure.publish_new, Path.unlink
                def replace(source, target):
                    real_replace(source, target)
                    if Path(target).name == 'cc-connect.toml':
                        raise KeyboardInterrupt
                def publish(source, target):
                    if target.name == 'settings.json':
                        raise KeyboardInterrupt
                    return real_publish(source, target)
                def unlink(path, *args, **kwargs):
                    if path.name == 'setup-pending.json':
                        raise KeyboardInterrupt
                    return real_unlink(path, *args, **kwargs)
                with patch.object(configure.os, 'replace', side_effect=replace if point == 'config' else real_replace), \
                        patch.object(configure, 'publish_new', side_effect=publish if point == 'settings' else real_publish), \
                        patch.object(Path, 'unlink', new=unlink if point == 'unlink' else real_unlink), \
                        self.assertRaises(KeyboardInterrupt):
                    configure.bind(root, runtime, 'TEST_OPENID_0001')
                self.assertEqual(install_app.setup_state(root, runtime), 'finalizing')
                configure.recover_setup(root, runtime)
                self.assertEqual(install_app.setup_state(root, runtime), 'configured')
                self.assertEqual(json.loads((runtime / 'settings.json').read_text())['session'], 'qqbot:TEST_OPENID_0001')
                saved = {p.name: p.read_bytes() for p in runtime.iterdir() if p.is_file()}
                configure.recover_setup(root, runtime)
                self.assertEqual(saved, {p.name: p.read_bytes() for p in runtime.iterdir() if p.is_file()})

    def test_foreign_config_and_damaged_journal_are_preserved(self):
        for damage in ('journal', 'stage', 'foreign'):
            with self.subTest(damage=damage), tempfile.TemporaryDirectory() as temp:
                root, runtime = self.fixture(temp)
                publish = configure.publish_new
                def fail_config(staged, target):
                    if target.name == 'cc-connect.toml':
                        raise OSError('interrupted')
                    return publish(staged, target)
                with patch.object(configure, 'publish_new', side_effect=fail_config), self.assertRaises(OSError):
                    configure.prepare(root, runtime, root / 'cc', '12345678', 'test-only', False)
                pending = runtime / 'setup-pending.json'
                state = json.loads(pending.read_text())
                if damage == 'journal':
                    pending.write_text('{broken', encoding='utf-8')
                elif damage == 'stage':
                    (runtime / state['staged']).write_bytes(b'corrupt')
                else:
                    (runtime / 'cc-connect.toml').write_text('my-user-config', encoding='utf-8')
                before = {p.name: p.read_bytes() for p in runtime.iterdir() if p.is_file()}
                with self.assertRaises((ValueError, OSError)):
                    configure.recover_setup(root, runtime)
                self.assertEqual(before, {p.name: p.read_bytes() for p in runtime.iterdir() if p.is_file()})

    def test_legacy_version_one_pending_remains_bindable(self):
        with tempfile.TemporaryDirectory() as temp:
            root, runtime = self.fixture(temp)
            configure.prepare(root, runtime, root / 'cc', '12345678', 'test-only', False)
            pending = runtime / 'setup-pending.json'
            state = json.loads(pending.read_text())
            state['version'] = 1
            state.pop('phase')
            pending.write_text(json.dumps(state), encoding='utf-8')
            self.assertEqual(install_app.setup_state(root, runtime), 'pairing')
            configure.bind(root, runtime, 'TEST_OPENID_0001')
            self.assertEqual(install_app.setup_state(root, runtime), 'configured')

    def test_ready_failure_or_cancel_never_binds_and_closes_owned_job(self):
        for error in (ValueError('timeout'), KeyboardInterrupt(), EOFError()):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as temp:
                root, runtime = self.fixture(temp)
                configure.prepare(root, runtime, root / 'cc', '12345678', 'test-only', False)
                before = (runtime / 'setup-pending.json').read_bytes()
                with patch.object(install_app, 'PairingGateway') as gateway, \
                        patch.object(install_app, 'wait_gateway_ready', side_effect=error), \
                        patch.object(configure, 'bind') as bind, contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(type(error)):
                        install_app.pair(root, runtime, ask=lambda _: self.fail('asked before ready'))
                gateway.return_value.__exit__.assert_called_once()
                bind.assert_not_called()
                self.assertEqual(before, (runtime / 'setup-pending.json').read_bytes())

    def test_real_process_exit_after_publication_is_recovered(self):
        tools = str(Path(__file__).resolve().parents[1] / 'tools')
        script = '''
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import configure
root, point = Path(sys.argv[2]), sys.argv[3]
runtime = root / 'runtime'
original = configure.publish_new
def interrupt(staged, target):
    original(staged, target)
    if target.name == point:
        os._exit(91)  # bypass finally, not a mock exception
configure.publish_new = interrupt
configure.prepare(root, runtime, root / 'cc', '12345678', 'test-only', False)
configure.bind(root, runtime, 'TEST_OPENID_0001')
'''
        for point, state in (('cc-connect.toml', 'pairing'), ('settings.json', 'configured')):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as temp:
                root, runtime = self.fixture(temp)
                result = subprocess.run([sys.executable, '-X', 'utf8', '-c', script, tools, temp, point],
                                        capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 91, result.stderr.decode('utf-8', 'replace'))
                configure.recover_setup(root, runtime)
                self.assertEqual(install_app.setup_state(root, runtime), state)

    def test_bind_transaction_cannot_overwrite_foreign_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            root, runtime = self.fixture(temp)
            configure.prepare(root, runtime, root / 'cc', '12345678', 'test-only', False)
            publish = configure.publish_new
            def interrupted(source, target):
                if target.name == 'settings.json':
                    raise OSError('interrupted')
                return publish(source, target)
            with patch.object(configure, 'publish_new', side_effect=interrupted), self.assertRaises(OSError):
                configure.bind(root, runtime, 'TEST_OPENID_0001')
            settings = runtime / 'settings.json'
            settings.write_bytes(b'{"mode":"different-user"}')
            before = {p.name: p.read_bytes() for p in runtime.iterdir() if p.is_file()}
            with self.assertRaises(ValueError):
                configure.recover_setup(root, runtime)
            self.assertEqual(before, {p.name: p.read_bytes() for p in runtime.iterdir() if p.is_file()})


if __name__ == '__main__':
    unittest.main()
