from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import configure
import pairing_agent


class PublicSetupTests(unittest.TestCase):
    def test_config_round_trips_chinese_paths_secret_escaping_and_disabled_commands(self):
        text = configure.config_text(Path('D:/中文 路径'), Path('C:/test/data'), '12345678', 'test-"\\value')
        config = tomllib.loads(text)
        project = config['projects'][0]
        self.assertEqual(project['platforms'][0]['options']['app_secret'], 'test-"\\value')
        self.assertEqual(Path(project['agent']['options']['cmd'][-1]).name, 'pairing_agent.py')
        self.assertIn('shell', project['disabled_commands'])
        self.assertEqual(project['admin_from'], '__no_remote_admin__')
        self.assertEqual(project['platforms'][0]['options']['group_allow_from'], '')
        self.assertNotIn('qq_desktop_bridge.py', text)

    def test_first_setup_and_explicit_binding_never_overwrite_existing_install(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'app'
            data, cc = Path(temp) / 'data', Path(temp) / 'cc'
            exe = root / 'vendor/cc-connect/v1.5.0-desktop-control/bin/cc-connect-desktop-control.exe'
            exe.parent.mkdir(parents=True)
            exe.write_bytes(b'fixture-not-executable')
            config = configure.prepare(root, data, cc, '12345678', 'test-only', False)
            with self.assertRaises(ValueError):
                configure.prepare(root, data, cc, '12345678', 'new-secret', False)
            configure.bind(root, data, 'TEST_OPENID_0001')
            result = tomllib.loads(config.read_text(encoding='utf-8'))['projects'][0]
            self.assertEqual(result['platforms'][0]['options']['allow_from'], 'TEST_OPENID_0001')
            self.assertTrue(result['agent']['options']['cmd'][-1].endswith('qq_desktop_bridge.py'))
            self.assertEqual(json.loads((data / 'settings.json').read_text())['session'], 'qqbot:TEST_OPENID_0001')
            self.assertFalse((data / 'setup-pending.json').exists())
            before = config.read_bytes()
            with self.assertRaises(ValueError):
                configure.bind(root, data, 'TEST_OPENID_0002')
            self.assertEqual(before, config.read_bytes())

    def test_changed_pending_config_and_qq_number_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = configure.prepare(root, root / 'data', root / 'cc', '12345678', 'test-only', True)
            with self.assertRaises(ValueError):
                configure.bind(root, root / 'data', '123456789')
            config.write_text(config.read_text() + '\n# changed', encoding='utf-8')
            with self.assertRaises(ValueError):
                configure.bind(root, root / 'data', 'TEST_OPENID_0001')

    def test_pairing_is_identity_only_and_private_chat_only(self):
        user = 'TEST_OPENID_0001'
        meta = {'version': 1, 'platform': 'qqbot', 'session_key': 'qqbot:' + user,
                'user_id': user, 'text': '绑定'}
        params = {'_meta': {'cc_connect': meta}}
        self.assertIn(user, pairing_agent.pairing_reply(params, 'qqbot:' + user))
        self.assertNotIn(user, pairing_agent.pairing_reply(params, 'qqbot:group:' + user))
        meta['text'] = '帮我修改文件'
        self.assertIn('不能操作 Codex', pairing_agent.pairing_reply(params, 'qqbot:' + user))
        replies = pairing_agent.handle({'jsonrpc': '2.0', 'id': 3, 'method': 'session/prompt',
                                        'params': params}, 'qqbot:' + user)
        self.assertEqual(replies[-1]['result']['stopReason'], 'end_turn')
