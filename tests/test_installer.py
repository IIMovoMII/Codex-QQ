from __future__ import annotations

import sys
import json
import os
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parents[1] / "src" / "codex_desktop_weixin"
sys.path.insert(0, str(PACKAGE_DIR))

from installer import (
    build_hook_command,
    enable_hooks_feature,
    hooks_feature_enabled,
    install_hooks,
    merge_hooks_document,
    remove_managed_hooks,
    uninstall_hooks,
)


class InstallerTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows PowerShell command syntax")
    def test_powershell_preserves_literal_script_path_and_hook_argument(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            probe = Path(temporary) / "中文 $literal & O'Brien" / "probe.py"
            probe.parent.mkdir()
            probe.write_text(
                "import json, sys\nprint(json.dumps(sys.argv, ensure_ascii=False))\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", build_hook_command(probe)],
                # A clean GitHub Windows runner can spend several seconds
                # starting PowerShell before the tiny probe runs. This is a
                # process-start budget, not a hook execution retry.
                capture_output=True, encoding="utf-8", timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), [str(probe.resolve()), "hook"])

    def test_hook_command_uses_absolute_python(self) -> None:
        command = build_hook_command(Path(r"D:\codex-desktop-weixin\cli.py"))
        self.assertIn(str(Path(sys.executable).resolve()), command)
        self.assertIn("-X utf8", command)
        self.assertIn(r"D:\codex-desktop-weixin\cli.py", command)
        self.assertTrue(command.endswith(" hook"))

    def test_enables_existing_features_without_losing_content(self) -> None:
        original = 'model_provider = "openai"\r\n\r\n[features]\r\njs_repl = false\r\n\r\n[mcp_servers.demo]\r\ncommand = "demo"\r\n'
        updated = enable_hooks_feature(original)
        self.assertIn("hooks = true", updated)
        self.assertIn('[mcp_servers.demo]', updated)
        self.assertIn('model_provider = "openai"', updated)
        self.assertEqual(enable_hooks_feature(updated), updated)
        self.assertTrue(hooks_feature_enabled(updated))

    def test_enables_commented_and_quoted_features_without_duplicate_tables_or_keys(self) -> None:
        for header in ('[features] # existing flags', '[ "features" ] # flags', "['features']"):
            for key in ('hooks', '"hooks"', "'hooks'"):
                with self.subTest(header=header, key=key):
                    original = f'model = "existing"\n{header}\n{key} = false # keep this comment\njs_repl = false\n'
                    updated = enable_hooks_feature(original)
                    expected = tomllib.loads(original)
                    expected['features']['hooks'] = True
                    self.assertEqual(tomllib.loads(updated), expected)
                    self.assertIn('# keep this comment', updated)
                    self.assertEqual(enable_hooks_feature(updated), updated)

    def test_table_text_in_multiline_values_and_case_sensitive_names_are_preserved(self) -> None:
        original = '''instructions = """
[features]
hooks = false
"""
[Features]
hooks = false
[features] # real flags
"hooks" = false
'''
        updated = enable_hooks_feature(original)
        expected = tomllib.loads(original)
        expected['features']['hooks'] = True
        self.assertEqual(tomllib.loads(updated), expected)

    def test_enabling_hooks_preserves_special_float_values(self) -> None:
        original = 'custom_value = nan\n[features]\nhooks = false\n'
        updated = enable_hooks_feature(original)
        self.assertIn('custom_value = nan', updated)
        self.assertTrue(hooks_feature_enabled(updated))

    def test_enabling_hooks_preserves_unicode_line_separators_in_strings(self) -> None:
        original = 'instructions = "hello\u2028world\u2029again"\n[features]\nhooks = false\n'
        updated = enable_hooks_feature(original)
        expected = tomllib.loads(original)
        expected['features']['hooks'] = True
        self.assertEqual(tomllib.loads(updated), expected)

    def test_real_install_accepts_commented_feature_table(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex_home = root / 'codex'
            codex_home.mkdir()
            config = codex_home / 'config.toml'
            original = 'model = "existing"\n[features] # user flags\n"hooks" = false\njs_repl = false\n'
            config.write_text(original, encoding='utf-8')
            result = install_hooks(codex_home, PACKAGE_DIR / 'cli.py', data_dir=root / 'runtime')
            expected = tomllib.loads(original)
            expected['features']['hooks'] = True
            self.assertEqual(tomllib.loads(config.read_text(encoding='utf-8')), expected)
            self.assertEqual((Path(result['backup_dir']) / 'config.toml').read_text(encoding='utf-8'), original)

    def test_merges_and_is_idempotent(self) -> None:
        existing = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other-hook"}]}]}}
        first = merge_hooks_document(existing, 'py -3 "D:\\codex-desktop-weixin\\cli.py" hook')
        second = merge_hooks_document(first, 'py -3 "D:\\codex-desktop-weixin\\cli.py" hook')
        self.assertEqual(first, second)
        self.assertEqual(len(first["hooks"]["Stop"]), 2)
        managed_handlers = [
            handler
            for groups in first["hooks"].values()
            for group in groups
            for handler in group.get("hooks", [])
            if "codex-desktop-weixin" in str(handler.get("command", ""))
        ]
        self.assertEqual(len(managed_handlers), 7)
        self.assertTrue(all("async" not in handler for handler in managed_handlers))
        cleaned = remove_managed_hooks(first)
        self.assertEqual(cleaned["hooks"]["Stop"], existing["hooks"]["Stop"])

    def test_merge_and_uninstall_preserve_other_handlers_in_a_managed_group(self) -> None:
        other = {'type': 'command', 'command': 'other-hook'}
        managed = {'type': 'command', 'command': '/app/codex_desktop_weixin/cli.py hook'}
        group = {'matcher': '*', 'description': 'user group', 'hooks': [other, managed]}
        original = {'hooks': {'Stop': [group]}}
        expected_group = dict(group, hooks=[other])
        merged = merge_hooks_document(original, '/app/codex_desktop_weixin/cli.py hook')
        self.assertEqual(merged['hooks']['Stop'][0], expected_group)
        self.assertEqual(remove_managed_hooks(original), {'hooks': {'Stop': [expected_group]}})
        self.assertEqual(remove_managed_hooks(merged), {'hooks': {'Stop': [expected_group]}})
        self.assertEqual(original['hooks']['Stop'][0]['hooks'], [other, managed])

    def test_real_install_and_uninstall_preserve_unmanaged_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex_home = root / ".codex"
            codex_home.mkdir()
            config = codex_home / "config.toml"
            config.write_text(
                'model_provider = "openai"\nnotify = ["existing"]\n\n[features]\njs_repl = false\n\n[mcp_servers.demo]\ncommand = "demo"\n',
                encoding="utf-8",
            )
            original_hook = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other-hook"}]}]}}
            hooks_path = codex_home / "hooks.json"
            hooks_path.write_text(json.dumps(original_hook), encoding="utf-8")

            result = install_hooks(codex_home, PACKAGE_DIR / "cli.py", data_dir=root / "runtime")
            updated = config.read_text(encoding="utf-8")
            self.assertTrue(hooks_feature_enabled(updated))
            self.assertIn('notify = ["existing"]', updated)
            self.assertIn('[mcp_servers.demo]', updated)
            self.assertTrue(Path(result["backup_dir"]).is_dir())
            installed = json.loads(hooks_path.read_text(encoding="utf-8"))
            self.assertEqual(len(installed["hooks"]["Stop"]), 2)

            uninstall_hooks(codex_home)
            self.assertEqual(json.loads(hooks_path.read_text(encoding="utf-8")), original_hook)


if __name__ == "__main__":
    unittest.main()
