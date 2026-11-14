from __future__ import annotations

import copy
import json
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "src/codex_desktop_weixin"
sys.path.insert(0, str(PACKAGE))

from desktop_migration import MigrationError, legacy_launcher_path, migrate_paths


class DesktopMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        # Windows CI may expose TEMP through an 8.3 alias; match the canonical
        # paths that the migration service intentionally publishes.
        self.root = Path(self.temp.name).resolve()
        self.old_root = self.root / "old-project"
        self.backend = self.root / "installed" / "_up_" / "backend"
        self.python = self.root / "installed" / "_up_" / "python" / "python.exe"
        self.gateway = self.root / "installed" / "_up_" / "gateway.exe"
        self.data_dir = self.root / "user-data"
        self.config_path = self.data_dir / "cc-connect" / "config.toml"
        self.settings_path = self.data_dir / "settings.json"
        old_gateway = self.old_root / "vendor/cc-connect/v1.5.0-desktop-control/bin/cc-connect-desktop-control.exe"
        self.old_gateway_path = str(old_gateway)
        self.old_script = self.old_root / "src/codex_desktop_weixin/qq_desktop_bridge.py"
        new_script = self.backend / "codex_desktop_weixin/qq_desktop_bridge.py"
        for path in (old_gateway, self.python, self.gateway, self.old_script, new_script):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"asset")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings = {
            "mode": "cc-connect",
            "executable": self.old_gateway_path,
            "project": "codex-desktop-reminders",
            "session": "qqbot:private-bound-openid",
            "data_dir": str(self.root / "qq-data"),
            "config_path": str(self.config_path),
            "timeout_seconds": 15,
        }
        self.settings_path.write_text(json.dumps(self.settings), encoding="utf-8")
        quote = lambda value: json.dumps(str(value), ensure_ascii=False)
        self.config_text = f'''language = "zh"
data_dir = {quote(self.settings['data_dir'])}

[[projects]]
name = "{self.settings['project']}"

[projects.agent]
type = "acp"

[projects.agent.options]
cmd = ["C:/Python/python.exe", "-X", "utf8", {quote(self.old_script.as_posix())}]
work_dir = {quote(self.old_root.as_posix())}
mode = "suggest"

[[projects.platforms]]
type = "qqbot"
[projects.platforms.options]
app_id = "12345678"
app_secret = "test-secret"
allow_from = "bound-openid-must-stay"
'''
        self.config_path.write_text(self.config_text, encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_migration_changes_only_runtime_paths_and_keeps_backup(self) -> None:
        before = tomllib.loads(self.config_text)
        expected = copy.deepcopy(before)
        options = expected["projects"][0]["agent"]["options"]
        options["cmd"] = [str(self.python), "-X", "utf8", str(self.backend / "codex_desktop_weixin/qq_desktop_bridge.py")]
        options["work_dir"] = str(self.backend)

        result = migrate_paths(self.data_dir, self.gateway, self.python, self.backend)

        after = tomllib.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(after, expected)
        current_settings = json.loads(self.settings_path.read_text(encoding="utf-8"))
        expected_settings = {**self.settings, "executable": str(self.gateway)}
        self.assertEqual(current_settings, expected_settings)
        self.assertEqual(result["changed"], True)
        backup = Path(result["backupDir"])
        self.assertEqual(json.loads((backup / "settings.json").read_text(encoding="utf-8")), self.settings)
        self.assertEqual((backup / "config.toml").read_text(encoding="utf-8"), self.config_text)
        self.assertFalse(list((self.data_dir / "state").glob("desktop-migration-*.json")))

    def test_legacy_launcher_is_derived_only_from_the_recognized_bridge_command(self) -> None:
        self.assertEqual(legacy_launcher_path(self.data_dir), self.old_root / "bin" / "CodexQQ助手.exe")
        migrate_paths(self.data_dir, self.gateway, self.python, self.backend)
        self.assertIsNone(legacy_launcher_path(self.data_dir))

    def test_repeated_migration_is_a_noop(self) -> None:
        migrate_paths(self.data_dir, self.gateway, self.python, self.backend)
        result = migrate_paths(self.data_dir, self.gateway, self.python, self.backend)
        self.assertEqual(result, {"changed": False, "backupDir": ""})

    def test_unrecognized_gateway_is_preserved(self) -> None:
        settings = {**self.settings, "executable": str(self.root / "custom-gateway.exe")}
        self.settings_path.write_text(json.dumps(settings), encoding="utf-8")
        before = self.config_path.read_bytes()
        with self.assertRaisesRegex(MigrationError, "gateway_executable_path_unrecognized"):
            migrate_paths(self.data_dir, self.gateway, self.python, self.backend)
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertEqual(json.loads(self.settings_path.read_text(encoding="utf-8")), settings)

    def test_unrecognized_bridge_command_is_preserved(self) -> None:
        changed = self.config_text.replace(self.old_script.as_posix(), "D:/other/custom-bridge.py")
        self.config_path.write_text(changed, encoding="utf-8")
        before = self.config_path.read_bytes()
        with self.assertRaisesRegex(MigrationError, "gateway_source_path_unrecognized"):
            migrate_paths(self.data_dir, self.gateway, self.python, self.backend)
        self.assertEqual(self.config_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
