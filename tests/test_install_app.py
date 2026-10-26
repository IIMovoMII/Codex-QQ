from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import configure
import install_app


class InstallWizardTests(unittest.TestCase):
    def test_first_run_prints_qq_bot_setup_guide(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            install_app.print_qq_bot_setup_guide()
        text = output.getvalue()
        for expected in ('q.qq.com', '创建机器人', 'AppID', 'AppSecret',
                         '开发设置', '沙箱配置', 'QQ 登录密码', 'bot.q.qq.com/wiki'):
            self.assertIn(expected, text)

    def make_formal(self, root, runtime):
        binary = root / install_app.GATEWAY_RELATIVE
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b'fixture')
        configure.prepare(root, runtime, root / 'cc', '12345678', 'test-only', False)
        configure.bind(root, runtime, 'TEST_OPENID_0001')

    def test_check_is_read_only_even_when_runtime_is_absent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'source'
            runtime = Path(temp) / 'not-created'
            codex = Path(temp) / 'codex'
            root.mkdir()
            codex.mkdir()
            (codex / 'config.toml').write_text('model = "fixture"\n', encoding='utf-8')
            before = sorted(str(path) for path in Path(temp).rglob('*'))
            result = install_app.check(root, runtime, codex)
            self.assertEqual(result['setup_state'], 'new')
            self.assertTrue(result['codex_config_ok'])
            self.assertEqual(before, sorted(str(path) for path in Path(temp).rglob('*')))
            self.assertFalse(runtime.exists())

    def test_formal_install_keeps_binding_and_does_not_prompt_or_start_pairing(self):
        with tempfile.TemporaryDirectory() as temp:
            root, runtime = Path(temp), Path(temp) / 'runtime'
            self.make_formal(root, runtime)
            before = {p.name: p.read_bytes() for p in runtime.iterdir() if p.is_file()}
            with patch.object(install_app, 'PairingGateway') as child, contextlib.redirect_stdout(io.StringIO()):
                self.assertTrue(install_app.pair(root, runtime, ask=Mock(side_effect=AssertionError),
                                                 hidden=Mock(side_effect=AssertionError)))
            child.assert_not_called()
            self.assertEqual(before, {p.name: p.read_bytes() for p in runtime.iterdir() if p.is_file()})

    def test_install_from_second_directory_is_rejected_without_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            root, runtime = Path(temp) / 'one', Path(temp) / 'runtime'
            self.make_formal(root, runtime)
            original = (runtime / 'settings.json').read_bytes()
            with self.assertRaises(ValueError):
                install_app.setup_state(Path(temp) / 'two', runtime)
            self.assertEqual(original, (runtime / 'settings.json').read_bytes())

    def test_formal_install_rejects_config_path_outside_runtime(self):
        with tempfile.TemporaryDirectory() as temp:
            root, runtime = Path(temp), Path(temp) / 'runtime'
            self.make_formal(root, runtime)
            settings = runtime / 'settings.json'
            value = json.loads(settings.read_text(encoding='utf-8'))
            value['config_path'] = str(Path(temp) / 'foreign.toml')
            settings.write_text(json.dumps(value), encoding='utf-8')
            with self.assertRaises(ValueError):
                install_app.setup_state(root, runtime)

    def test_pending_tamper_is_rejected_before_gateway_launch(self):
        with tempfile.TemporaryDirectory() as temp:
            root, runtime = Path(temp), Path(temp) / 'runtime'
            config = configure.prepare(root, runtime, root / 'cc', '12345678', 'test-only', False)
            config.write_text(config.read_text(encoding='utf-8') + '\n# modified', encoding='utf-8')
            with patch.object(install_app, 'PairingGateway') as child, self.assertRaises(ValueError):
                install_app.pair(root, runtime)
            child.assert_not_called()

    def test_resume_cancel_stops_only_owned_pairing_gateway_and_keeps_progress(self):
        with tempfile.TemporaryDirectory() as temp:
            root, runtime = Path(temp), Path(temp) / 'runtime'
            config = configure.prepare(root, runtime, root / 'cc', '12345678', 'test-only', False)
            original = config.read_bytes()
            child = Mock()
            child.poll.return_value = None
            with patch.object(install_app, 'PairingGateway') as gateway, \
                    patch.object(install_app, 'wait_gateway_ready'), contextlib.redirect_stdout(io.StringIO()):
                gateway.return_value.__enter__.return_value = child
                self.assertFalse(install_app.pair(root, runtime, ask=lambda _: '取消',
                                                  hidden=Mock(side_effect=AssertionError)))
            gateway.return_value.__exit__.assert_called_once()
            self.assertEqual(original, config.read_bytes())
            self.assertTrue((runtime / 'setup-pending.json').is_file())
            self.assertFalse((runtime / 'settings.json').exists())

    def test_resume_stops_gateway_before_binding_and_requires_explicit_confirmation(self):
        with tempfile.TemporaryDirectory() as temp:
            root, runtime = Path(temp), Path(temp) / 'runtime'
            configure.prepare(root, runtime, root / 'cc', '12345678', 'test-only', False)
            child, events = Mock(), []
            child.poll.return_value = None
            answers = iter(['123456789', 'TEST_OPENID_0001', 'yes'])
            with patch.object(install_app, 'PairingGateway') as gateway, \
                    patch.object(install_app, 'wait_gateway_ready'), \
                    patch.object(configure, 'bind', side_effect=lambda *a: events.append('bind')), \
                    contextlib.redirect_stdout(io.StringIO()):
                gateway.return_value.__enter__.return_value = child
                gateway.return_value.__exit__.side_effect = lambda *args: events.append('stop') or False
                self.assertTrue(install_app.pair(root, runtime, ask=lambda _: next(answers)))
            self.assertEqual(events, ['stop', 'bind'])

    def test_noninteractive_install_refuses_before_any_preparation(self):
        status = dict(windows_x64=True, python_ok=True, pythonw_ok=True,
                      launcher_prerequisite_ok=True, setup_state='new', codex_config_ok=True)
        with patch.object(sys, 'argv', ['install_app.py']), patch.object(sys, 'stdin') as stdin, \
                patch.object(install_app, 'check', return_value=status), \
                patch.object(install_app, 'prepare_files') as prepare, \
                patch.object(install_app, 'acquire_file_lock') as lock, contextlib.redirect_stderr(io.StringIO()):
            stdin.isatty.return_value = False
            self.assertEqual(install_app.main(), 1)
            prepare.assert_not_called()
            lock.assert_not_called()

    def test_prepare_mode_does_not_pair_or_start_or_install_hooks(self):
        with tempfile.TemporaryDirectory() as temp:
            status = dict(windows_x64=True, python_ok=True, pythonw_ok=True,
                          launcher_prerequisite_ok=True, setup_state='new', codex_config_ok=False)
            with patch.object(sys, 'argv', ['install_app.py', '--prepare']), \
                    patch.object(install_app, 'default_data_dir', return_value=Path(temp)), \
                    patch.object(install_app, 'check', return_value=status), \
                    patch.object(install_app, 'prepare_files') as prepare, \
                    patch.object(install_app, 'pair') as pair, \
                    patch.object(install_app, 'finish') as finish, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(install_app.main(), 0)
            prepare.assert_called_once()
            pair.assert_not_called()
            finish.assert_not_called()

    def test_finish_installs_hooks_only_after_ready_and_consent(self):
        events = []
        with patch.object(install_app, 'start_application', side_effect=lambda *a: events.append('start') or True), \
                patch.object(install_app, 'hooks_current', return_value=False), \
                patch.object(install_app, 'install_hooks', side_effect=lambda *a, **kw: events.append('hooks')), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(install_app.finish(Path('.'), Path('.'), Path('.'), ask=lambda _: 'yes'))
            self.assertEqual(events, ['start', 'hooks'])
            events.clear()
            self.assertFalse(install_app.finish(Path('.'), Path('.'), Path('.'), ask=lambda _: ''))
            self.assertEqual(events, ['start'])

    def test_existing_current_hooks_are_not_rewritten_or_backed_up_again(self):
        with patch.object(install_app, 'start_application', return_value=True), \
                patch.object(install_app, 'hooks_current', return_value=True), \
                patch.object(install_app, 'install_hooks') as hooks, contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(install_app.finish(Path('.'), Path('.'), Path('.'),
                                                ask=Mock(side_effect=AssertionError)))
        hooks.assert_not_called()


if __name__ == '__main__':
    unittest.main()
