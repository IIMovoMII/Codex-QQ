import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import install_app
from setup_diagnostics import InstallFailure, run_logged


class SetupDiagnosticTests(unittest.TestCase):
    def test_real_child_success_keeps_full_log(self):
        with tempfile.TemporaryDirectory() as temp:
            logs = Path(temp)
            run_logged([sys.executable, '-X', 'utf8', '-c', 'print("编译成功")'], stage='启动器编译', logs=logs)
            text = next(logs.iterdir()).read_text(encoding='utf-8')
            self.assertIn('编译成功', text)
            self.assertIn('退出码：0', text)

    def test_real_stderr_chinese_exit_code_long_output_and_redaction(self):
        with tempfile.TemporaryDirectory() as temp:
            logs = Path(temp)
            script = ('import sys; print("app_secret=FAKE_SECRET_FOR_TEST", file=sys.stderr); '
                      'print("openid=FAKE_OPENID_FOR_TEST", file=sys.stderr); '
                      'print("Authorization: Bearer FAKE_ACCESS_TOKEN", file=sys.stderr); '
                      'print("X"*50000, file=sys.stderr); '
                      'print("中文编译器错误 CS9999 plain-private", file=sys.stderr); sys.exit(23)')
            with self.assertRaises(InstallFailure) as caught:
                run_logged([sys.executable, '-X', 'utf8', '-c', script], stage='启动器编译', logs=logs,
                           sensitive=['plain-private'])
            error = caught.exception
            self.assertEqual(error.code, 23)
            self.assertEqual(error.stage, '启动器编译')
            self.assertIn('中文编译器错误 CS9999', str(error))
            self.assertIn('退出码 23', str(error))
            self.assertLess(len(str(error)), 3000)
            content = error.log_path.read_text(encoding='utf-8')
            self.assertGreater(len(content), 50000)
            for value in ('FAKE_SECRET_FOR_TEST', 'FAKE_OPENID_FOR_TEST', 'FAKE_ACCESS_TOKEN', 'plain-private'):
                self.assertNotIn(value, content)
                self.assertNotIn(value, str(error))

    def test_wizard_reports_stage_exit_and_log_instead_of_exception_class(self):
        status = dict(windows_x64=True, python_ok=True, pythonw_ok=True,
                      launcher_prerequisite_ok=True, setup_state='new', codex_config_ok=False)
        with tempfile.TemporaryDirectory() as temp:
            stderr = io.StringIO()
            error = InstallFailure('启动器编译', code=17, summary='中文错误 CS1234', log_path=Path(temp) / 'build.log')
            with patch.object(sys, 'argv', ['install_app.py', '--prepare']), \
                    patch.object(install_app, 'default_data_dir', return_value=Path(temp)), \
                    patch.object(install_app, 'check', return_value=status), \
                    patch.object(install_app, 'prepare_files', side_effect=error), contextlib.redirect_stderr(stderr):
                self.assertEqual(install_app.main(), 1)
            for value in ('启动器编译', '退出码 17', '中文错误 CS1234', 'build.log'):
                self.assertIn(value, stderr.getvalue())

    def test_prepare_stages_download_and_verification_errors(self):
        from fetch_gateway import GatewayDownloadError
        for error, stage in ((GatewayDownloadError('network'), 'QQ 网关下载'),
                             (ValueError('网关发布包校验失败'), 'QQ 网关产物验证')):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temp:
                with patch.object(install_app, 'ensure_gateway', side_effect=error), \
                        contextlib.redirect_stdout(io.StringIO()), self.assertRaises(InstallFailure) as caught:
                    install_app.prepare_files(Path(temp), Path(temp) / 'logs')
                self.assertEqual(caught.exception.stage, stage)
                self.assertTrue(caught.exception.log_path.is_file())

    def test_exit_zero_still_requires_verified_artifacts(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(install_app, 'ensure_gateway', return_value={'cached': True}), \
                    patch.object(install_app, 'launcher_ready', return_value=False), \
                    patch.object(install_app, 'run_logged'), contextlib.redirect_stdout(io.StringIO()), \
                    self.assertRaises(InstallFailure) as caught:
                install_app.prepare_files(Path(temp), Path(temp) / 'logs')
            self.assertEqual(caught.exception.stage, '启动器产物验证')

    def test_legacy_windows_compiler_output_keeps_chinese(self):
        with tempfile.TemporaryDirectory() as temp:
            script = 'import sys; sys.stderr.buffer.write("中文编译错误".encode("gb18030")); sys.exit(7)'
            with self.assertRaises(InstallFailure) as caught:
                run_logged([sys.executable, '-X', 'utf8', '-c', script], stage='编译', logs=Path(temp))
            self.assertIn('中文编译错误', str(caught.exception))

    def test_log_location_failure_reports_stage(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'not-a-directory'
            path.write_bytes(b'owned')
            with self.assertRaises(InstallFailure) as caught:
                run_logged([sys.executable, '-c', 'pass'], stage='启动器编译', logs=path)
            self.assertEqual(caught.exception.stage, '启动器编译')
            self.assertIn('无法保存诊断日志', str(caught.exception))
            self.assertEqual(path.read_bytes(), b'owned')


if __name__ == '__main__':
    unittest.main()
