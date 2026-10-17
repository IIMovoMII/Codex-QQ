from __future__ import annotations

import sys
import json
import os
import subprocess
import tempfile
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
                capture_output=True, encoding="utf-8", timeout=5,
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
