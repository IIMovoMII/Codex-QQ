from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src' / 'codex_desktop_weixin'))
import launcher

ENDPOINT = r'\\.\pipe\codex-browser-use-00000000-0000-0000-0000-000000000000'


class EndpointHintTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.hint = self.root / 'state' / 'native-app-tools.json'
        for name, value in (('read_cc_connect_pid', 50), ('cc_connect_api_status', 'ready')):
            mock = patch.object(launcher, name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)

    def start(self, endpoint):
        with patch.dict(os.environ, {'CODEX_APP_TOOLS_PIPE_PATH': endpoint}):
            return launcher.start_cc_connect_service(self.root)

    def test_native_hint_updated_even_when_cc_connect_is_already_running(self):
        self.assertEqual(self.start(ENDPOINT)['reason'], 'already_running')
        self.assertEqual(json.loads(self.hint.read_text(encoding='utf-8')), {'version': 1, 'path': ENDPOINT})
        changed = ENDPOINT.replace('00000000-0000', '11111111-0000', 1)
        self.start(changed)
        self.assertEqual(json.loads(self.hint.read_text(encoding='utf-8'))['path'], changed)

    def test_unchanged_hint_is_not_rewritten_each_service_check(self):
        self.start(ENDPOINT)
        with patch('endpoint_hint.atomic_write_json') as write:
            self.start(ENDPOINT)
            write.assert_not_called()

    def test_missing_or_wrong_kind_endpoint_cannot_overwrite_existing_hint(self):
        self.start(ENDPOINT)
        original = self.hint.read_bytes()
        for invalid in ('', r'\\.\pipe\another-service', 'https://example.test'):
            self.start(invalid)
            self.assertEqual(self.hint.read_bytes(), original)

    def test_hint_write_failure_never_prevents_existing_notification_service(self):
        with patch('endpoint_hint.atomic_write_json', side_effect=OSError('fixture')):
            self.assertEqual(self.start(ENDPOINT)['cc_connect_api_status'], 'ready')


if __name__ == '__main__':
    unittest.main()
