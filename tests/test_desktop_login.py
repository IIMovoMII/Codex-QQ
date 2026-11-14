from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/codex_desktop_weixin'))
from desktop_login import LoginEvidence

class LoginEvidenceTests(unittest.TestCase):
    def test_pending_baseline_survives_host_restart_and_rearms_for_upgraded_executable(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            exe = data / 'app.exe'
            exe.write_bytes(b'old-program')
            evidence = LoginEvidence(data, exe, context=lambda:dict(id='old', logonAt=100, kind=2), now=lambda:200)
            evidence.baseline()
            before = evidence.path.read_bytes()
            evidence.context = lambda:dict(id='new', logonAt=250, kind=2)
            evidence.now = lambda:260
            evidence.baseline()
            self.assertEqual(evidence.path.read_bytes(), before)
            evidence.observe_launch(True)
            evidence.baseline()
            self.assertEqual(evidence.read()['stage'], 'login_detected')
            self.assertTrue(evidence.verified(startup=True, runtime_ready=True, qq_ready=True))
            exe.write_bytes(b'upgraded-program')
            evidence.baseline()
            self.assertEqual(evidence.read()['stage'], 'awaiting_login')
            self.assertEqual(evidence.read()['baselineSession'], 'new')
            evidence.observe_launch(True)
            self.assertFalse(evidence.verified(startup=True, runtime_ready=True, qq_ready=True))

    def test_failed_qq_connection_can_be_verified_at_a_later_real_logon(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            exe = data / 'app.exe'
            exe.write_bytes(b'test-program')
            evidence = LoginEvidence(data, exe, context=lambda:dict(id='old', logonAt=100, kind=2), now=lambda:200)
            evidence.baseline()
            evidence.context = lambda:dict(id='first-logon', logonAt=250, kind=2)
            evidence.now = lambda:260
            evidence.observe_launch(True)
            self.assertFalse(evidence.verified(startup=True, runtime_ready=True, qq_ready=False))
            evidence.context = lambda:dict(id='second-logon', logonAt=400, kind=2)
            evidence.now = lambda:410
            evidence.baseline()
            evidence.observe_launch(False)
            self.assertFalse(evidence.verified(startup=True, runtime_ready=True, qq_ready=True))
            evidence.observe_launch(True)
            self.assertEqual(evidence.read()['loginSession'], 'second-logon')
            evidence.baseline()
            self.assertTrue(evidence.verified(startup=True, runtime_ready=True, qq_ready=True))

    def test_manual_start_and_same_logon_cannot_prove_autostart(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            exe = data / 'app.exe'
            exe.write_bytes(b'test-program')
            evidence = LoginEvidence(data, exe, context=lambda:dict(id='old', logonAt=100, kind=2), now=lambda:200)
            evidence.baseline()
            evidence.observe_launch(True)
            self.assertFalse(evidence.verified(startup=True, runtime_ready=True, qq_ready=True))
            evidence.context = lambda:dict(id='new', logonAt=250, kind=2)
            evidence.now = lambda:260
            evidence.observe_launch(False)
            self.assertFalse(evidence.verified(startup=True, runtime_ready=True, qq_ready=True))
            evidence.observe_launch(True)
            self.assertFalse(evidence.verified(startup=True, runtime_ready=True, qq_ready=False))
            self.assertTrue(evidence.verified(startup=True, runtime_ready=True, qq_ready=True))
            self.assertFalse(evidence.verified(startup=False, runtime_ready=True, qq_ready=True))
            exe.write_bytes(b'changed-program')
            self.assertFalse(evidence.verified(startup=True, runtime_ready=True, qq_ready=True))

    def test_late_or_noninteractive_launch_stays_unverified(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            exe = data / 'app.exe'
            exe.write_bytes(b'test-program')
            evidence = LoginEvidence(data, exe, context=lambda:dict(id='old', logonAt=100, kind=2), now=lambda:200)
            evidence.baseline()
            for context in (dict(id='new', logonAt=250, kind=3), dict(id='new', logonAt=250, kind=2)):
                evidence.context = lambda: context
                evidence.now = lambda:600
                evidence.observe_launch(True)
                self.assertFalse(evidence.verified(startup=True, runtime_ready=True, qq_ready=True))
