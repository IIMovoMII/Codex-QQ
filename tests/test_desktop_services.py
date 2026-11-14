from __future__ import annotations
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src/codex_desktop_weixin"))
sys.path.insert(0, str(ROOT / "tools"))
from ui_operations import OperationJournal, OperationConflict
from setup_service import SetupService
from desktop_views import notifications
from config_transaction import ConfigTransaction
from configuration_service import selected, render_platform

class DesktopServiceTests(unittest.TestCase):
    def test_config_edit_selects_project_and_retains_every_other_field(self):
        import configure, tomllib
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            data=root/'data'
            data.mkdir()
            original=configure.config_text(root, root/'qq', '12345678', 'original-secret', 'owner_12345678')
            original += '\n[[projects]]\nname="unrelated"\n[projects.agent]\ntype="test"\n'
            config=data/'cc-connect.toml'
            config.write_text(original, encoding='utf-8')
            (data/'settings.json').write_text(json.dumps({'project':'codex-qq','config_path':str(config)}))
            settings, doc, pi, qi, opts, _, path, before = selected(data)
            after=render_platform(before,doc,pi,qi,{'app_secret':'replacement-secret','sandbox':True})
            ConfigTransaction(data).commit([(path,before,after)])
            changed=tomllib.loads(config.read_text(encoding='utf-8'))
            self.assertEqual(changed['projects'][1],doc['projects'][1])
            self.assertEqual(changed['projects'][0]['platforms'][0]['options']['allow_from'],'owner_12345678')
            self.assertIn(b'replacement-secret',config.read_bytes())
            with self.assertRaisesRegex(ValueError,'intervening_edit'):
                ConfigTransaction(data).commit([(path,before,after)])

    def test_crash_between_config_and_settings_is_recovered_and_edits_preserved(self):
        from config_transaction import digest
        from runtime_io import atomic_write_json
        with tempfile.TemporaryDirectory() as tmp:
            data=Path(tmp)
            config=data/'cc-connect.toml'
            config.write_bytes(b'before')
            settings={'config_path':str(config)}
            atomic_write_json(data/'settings.json',settings)
            ident='a'*32
            backup=data/'install-backups'/('config-'+ident)
            backup.mkdir(parents=True)
            atomic_write_json(backup/'settings-authority.json',settings)
            (backup/'0.before').write_bytes(b'before')
            (backup/'0.after').write_bytes(b'after')
            marker={'version':1,'id':ident,'files':[{'path':str(config),'before':digest(b'before'),'after':digest(b'after')}]}
            atomic_write_json(data/'state/config-change.json',marker)
            ConfigTransaction(data).recover()
            self.assertEqual(config.read_bytes(),b'after')
            atomic_write_json(data/'state/config-change.json',marker)
            config.write_bytes(b'user edit')
            with self.assertRaisesRegex(ValueError,'intervening_edit'):
                ConfigTransaction(data).recover()
            self.assertEqual(config.read_bytes(),b'user edit')

    def test_receipt_survives_restart_and_changed_payload_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "operations.db"
            first = OperationJournal(path)
            self.assertIsNone(first.begin("id", "save", {"appSecret": "private-test-secret"}))
            reply = {"status": "confirmed", "operationId": "id"}
            first.finish("id", reply)
            restarted = OperationJournal(path)
            self.assertEqual(restarted.begin("id", "save", {"appSecret": "private-test-secret"}), reply)
            with self.assertRaises(OperationConflict):
                restarted.begin("id", "save", {"appSecret": "different"})
            self.assertNotIn(b"private-test-secret", path.read_bytes())

    def test_interrupted_mutation_does_not_reexecute(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "operations.db"
            self.assertIsNone(OperationJournal(path).begin("id", "send", {}))
            result = OperationJournal(path).begin("id", "send", {})
            self.assertEqual(result["status"], "unknown")
            self.assertIn("interrupted", result["errorCode"])

    def test_packaged_setup_uses_shared_journal_and_retains_binding(self):
        import configure
        class Gateway:
            closed = 0
            def __init__(self, *_args, **kwargs):
                self.alive = True
                kwargs['log'].write(b'level=INFO msg="qqbot: gateway ready"\n')
                kwargs['log'].flush()
            def poll(self): return None if self.alive else 0
            def close(self):
                if self.alive: type(self).closed += 1
                self.alive = False
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "resources"
            (root / "backend/codex_desktop_weixin").mkdir(parents=True)
            (root / "tools").mkdir()
            (root / "tools/pairing_agent.py").touch()
            gateway = root / "gateway.exe"
            gateway.touch()
            data = Path(tmp) / "testdata"
            service = SetupService(root, data, gateway)
            self.assertEqual(service.prepare()["step"], 1)
            service.robot_ready()
            with patch("setup_process.PairingGateway", Gateway), patch("setup_process.wait_gateway_ready"):
                state = service.begin({"appId": "12345678", "appSecret": "test-only-secret", "sandbox": True})
                self.assertTrue(state["ready"])
                service.close()  # Closing the wizard retains committed credentials.
                self.assertEqual(service.state()["step"], 3)
                service.begin({})
                state = service.complete({"openId": "test_person_1234", "confirmed": True})
            self.assertEqual(state["step"], 5)
            saved = json.loads((data / "settings.json").read_text())
            self.assertEqual(saved["session"], "qqbot:test_person_1234")
            self.assertEqual(saved["executable"], str(gateway))
            self.assertIn("backend", (data / "cc-connect.toml").read_text())
            before = (data / "settings.json").read_bytes()
            with self.assertRaisesRegex(ValueError, "existing_binding"):
                service.begin({})
            self.assertEqual((data / "settings.json").read_bytes(), before)
            service.close()

    def test_correcting_unbound_credentials_keeps_backup_and_can_resume(self):
        import configure
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data, qq = root / 'setup', root / 'qq'
            configure.prepare(root, data, qq, '12345678', 'wrong-test-secret', False)
            qq.mkdir()
            (qq / 'temporary-gateway-file').write_bytes(b'preserved')
            configure.reset_pending(root, data)
            self.assertFalse((data / 'setup-pending.json').exists())
            self.assertEqual(len(list((data / 'setup-backups').glob('*/cc-connect.toml'))), 1)
            view = json.loads((data / 'state/setup-view.json').read_text())
            self.assertNotEqual(Path(view['ccData']), qq)
            configure.prepare(root, data, Path(view['ccData']), '87654321', 'correct-test-secret', True)
            _, document = configure.load_pending(root, data)
            self.assertEqual(document['projects'][0]['platforms'][0]['options']['app_id'], '87654321')
            self.assertEqual((qq / 'temporary-gateway-file').read_bytes(), b'preserved')

    def test_notification_projection_never_includes_body_or_fake_timeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sent = root / "outbox/sent"
            sent.mkdir(parents=True)
            (sent / "one.json").write_text(json.dumps({"notification": {
                "state": "completed", "created_at": "2026-11-13T01:02:03+00:00",
                "message": "private body", "session_id": "private-thread",
            }, "sent_at": "2026-11-13T01:02:05+00:00"}))
            result = notifications(root)
            self.assertEqual(result["total"], 1)
            self.assertEqual(len(result["deliveries"][0]["timeline"]), 2)
            self.assertNotIn("private", json.dumps(result))
