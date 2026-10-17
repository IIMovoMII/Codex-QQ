from __future__ import annotations

import sys
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src' / 'codex_desktop_weixin'))
import build_cc_connect
import control_contract
from desktop_control import DesktopControlError


class BuildContractTests(unittest.TestCase):
    def test_build_tags_keep_acp_and_qq_and_exclude_cli_agent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plugins = root / 'cmd' / 'cc-connect'
            plugins.mkdir(parents=True)
            for name in ('acp', 'qqbot', 'codex', 'web', 'telegram'):
                (plugins / f'plugin_{name}.go').write_text(f'//go:build !no_{name}\n', encoding='utf-8')
            tags = build_cc_connect.build_tags(root)
            self.assertNotIn('no_acp', tags)
            self.assertNotIn('no_qqbot', tags)
            self.assertIn('no_codex', tags)
            (plugins / 'plugin_acp.go').unlink()
            with self.assertRaises(ValueError):
                build_cc_connect.build_tags(root)

    def test_actual_imports_and_binary_tags_reject_missing_acp(self):
        good = '\n'.join(build_cc_connect.MODULE + x for x in ('agent/acp', 'platform/qqbot'))
        build_cc_connect.verify_dependencies(good)
        with self.assertRaises(ValueError):
            build_cc_connect.verify_dependencies(good.replace('agent/acp', 'agent/codex'))
        build_cc_connect.verify_binary_metadata('build\t-tags=no_codex,no_web')
        for wrong in ('build\t-tags=no_acp,no_web', 'build\t-tags=no_qqbot,no_codex', 'no build tags'):
            with self.assertRaises(ValueError):
                build_cc_connect.verify_binary_metadata(wrong)

    def bundle(self, root, *, table=None, module='src-new-hash.js', prefix=b''):
        table = table if table is not None else {**control_contract.BASE_METHODS,
            'thread-follower-start-turn': 2, 'thread-follower-steer-turn': 1,
            'thread-follower-compact-thread': 1, 'thread-follower-remove-queued-message': 1}
        body = prefix + b'var table=' + json.dumps(table).encode() + b';'
        node = {'files': {module: {'size': len(body), 'offset': '0'}}}
        index = {'files': {'.vite': {'files': {'build': node}}}}
        header = json.dumps(index).encode()
        bundle = struct.pack('<IIII', 4, 8 + len(header), 0, len(header)) + header + body
        image = root / 'app' / 'ChatGPT.exe'
        asar = image.parent / 'resources' / 'app.asar'
        asar.parent.mkdir(parents=True)
        image.write_bytes(b'fixture')
        asar.write_bytes(bundle)
        return image, asar

    def test_new_installation_names_module_names_and_unrelated_bytes_remain_compatible(self):
        with tempfile.TemporaryDirectory() as temp:
            for folder, module, padding in (('old-version', 'src-old.js', b'old-ui'),
                                            ('new-version', 'protocol-new.mjs', b'new-ui' * 70000)):
                image, _ = self.bundle(Path(temp) / folder, module=module, prefix=padding)
                control_contract.check_contract(str(image))
                control_contract.check_app_tools_contract(str(image))
                control_contract.check_steer_contract(str(image))
                control_contract.check_queue_contract(str(image))
            # A previous installation's capability table cannot authorize an
            # arbitrary process whose own bundle is missing.
            image = Path(temp) / 'other-app' / 'ChatGPT.exe'
            image.parent.mkdir()
            image.write_bytes(b'fixture')
            with self.assertRaises(DesktopControlError):
                control_contract.check_contract(str(image))

    def test_changed_method_disables_only_that_method(self):
        with tempfile.TemporaryDirectory() as temp:
            table = {**control_contract.BASE_METHODS, 'thread-follower-steer-turn': 99,
                     'thread-follower-start-turn': 2, 'unrelated-future-method': 999}
            image, _ = self.bundle(Path(temp), table=table)
            control_contract.check_contract(str(image))
            control_contract.check_queue_contract(str(image))
            control_contract.check_method_contract(str(image), 'thread-follower-start-turn', 2)
            with self.assertRaises(DesktopControlError):
                control_contract.check_steer_contract(str(image))
            with self.assertRaises(DesktopControlError):
                control_contract.check_method_contract(str(image), 'missing-method', 1)

    def test_changed_snapshot_source_and_truncated_bundle_reject(self):
        with tempfile.TemporaryDirectory() as temp:
            image, asar = self.bundle(Path(temp), table={**control_contract.BASE_METHODS,
                                            'thread-stream-state-changed': 12})
            with self.assertRaises(DesktopControlError):
                control_contract.check_contract(str(image))
            asar.write_bytes(asar.read_bytes()[:-1])
            with self.assertRaises(DesktopControlError):
                control_contract.capabilities(str(image))

    def test_cache_uses_actual_bundle_identity_and_never_retains_caller_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            image, asar = self.bundle(Path(temp))
            control_contract._capabilities.cache_clear()
            first = control_contract.capabilities(str(image))
            first['thread-follower-start-turn'] = 999
            self.assertEqual(control_contract.capabilities(str(image))['thread-follower-start-turn'], 2)
            self.assertEqual(control_contract._capabilities.cache_info().hits, 1)
            asar.write_bytes(asar.read_bytes()[:-1])
            with self.assertRaises(DesktopControlError):
                control_contract.capabilities(str(image))


if __name__ == '__main__':
    unittest.main()
