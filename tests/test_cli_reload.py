from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src' / 'codex_desktop_weixin'))
import cli
from process_watch import ProcessStatus
from runtime_mode import set_runtime_mode


class ReloadBridgeTests(unittest.TestCase):
    def test_independent_mode_reload_works_without_codex_and_start_does_not_launch_it(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(temp)
            set_runtime_mode(root, 'independent')
            with patch.object(cli, 'get_codex_desktop_process_status', return_value=ProcessStatus(False, False)), \
                 patch.object(cli, 'stop_cc_connect_service', return_value={'cc_connect_stopped': True}), \
                 patch.object(cli, 'start_cc_connect_service', return_value={'cc_connect_pid': 10, 'cc_connect_api_status': 'ready'}), \
                 patch('resident_app.launch_app', return_value={'app_launch_requested': True}) as app, \
                 patch.object(cli, 'launch_codex_desktop') as codex:
                self.assertEqual(cli.command_reload_bridge(root), 0)
                self.assertEqual(cli.command_start(root / '.codex', root), 0)
                app.assert_called_once()
                codex.assert_not_called()

    def test_reload_only_bridge_never_launches_desktop_or_stops_monitor(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            with patch.object(cli, 'get_codex_desktop_process_status', return_value=ProcessStatus(desktop_alive=True, app_server_alive=True)), \
                 patch.object(cli, 'stop_cc_connect_service', return_value={'cc_connect_stopped': True}) as stop, \
                 patch.object(cli, 'start_cc_connect_service', return_value={'cc_connect_pid': 10, 'cc_connect_api_status': 'ready'}) as start, \
                 patch.object(cli, 'launch_codex_desktop') as launch, patch.object(cli, 'request_stop') as global_stop:
                self.assertEqual(cli.command_reload_bridge(Path(temp)), 0)
                stop.assert_called_once()
                start.assert_called_once()
                launch.assert_not_called()
                global_stop.assert_not_called()
                lock = cli._acquire_autostart_lock(Path(temp))
                self.assertIsNotNone(lock)
                cli.os.close(lock[0])

    def test_reload_offline_or_failed_service_never_reports_success(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            with patch.object(cli, 'get_codex_desktop_process_status', return_value=ProcessStatus(desktop_alive=False, app_server_alive=False)), \
                 patch.object(cli, 'start_cc_connect_service') as start:
                self.assertNotEqual(cli.command_reload_bridge(Path(temp)), 0)
                start.assert_not_called()
            with patch.object(cli, 'get_codex_desktop_process_status', return_value=ProcessStatus(desktop_alive=True, app_server_alive=True)), \
                 patch.object(cli, 'stop_cc_connect_service', return_value={'cc_connect_stopped': True}), \
                 patch.object(cli, 'start_cc_connect_service', return_value={'cc_connect_pid': None, 'cc_connect_api_status': 'missing'}):
                self.assertNotEqual(cli.command_reload_bridge(Path(temp)), 0)


if __name__ == '__main__':
    unittest.main()
