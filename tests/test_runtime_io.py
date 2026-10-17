from __future__ import annotations

import ctypes
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src' / 'codex_desktop_weixin'))
from runtime_io import atomic_write_json


def win_error(code=5):
    error = PermissionError('fixture')
    error.winerror = code
    return error


class AtomicWriteTests(unittest.TestCase):
    def test_short_windows_read_lock_retries_only_replace_and_preserves_unicode(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / '状态.json'
            real_replace = os.replace
            calls = []
            def transient(src, dst):
                calls.append((src, dst))
                if len(calls) <= 2:
                    raise win_error(32)
                return real_replace(src, dst)
            with patch('runtime_io.os.replace', side_effect=transient), patch('runtime_io.time.sleep') as sleep:
                atomic_write_json(path, {'value': '中文😀'})
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')), {'value': '中文😀'})
            self.assertEqual(len(calls), 3)
            self.assertEqual(sleep.call_count, 2)
            self.assertEqual(len(set(str(x[0]) for x in calls)), 1)
            self.assertEqual(list(Path(temp).glob('*.tmp')), [])

    def test_persistent_permission_denial_is_bounded_and_preserves_old_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'state.json'
            path.write_text('{"old":true}', encoding='utf-8')
            with patch('runtime_io.os.replace', side_effect=win_error()), patch('runtime_io.time.sleep') as sleep:
                with self.assertRaises(PermissionError):
                    atomic_write_json(path, {'new': True})
            self.assertEqual(json.loads(path.read_text()), {'old': True})
            self.assertLessEqual(sleep.call_count, 4)
            self.assertEqual(list(Path(temp).glob('*.tmp')), [])

    def test_nonsharing_errors_and_happy_path_have_no_sleep(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'state.json'
            with patch('runtime_io.time.sleep') as sleep:
                atomic_write_json(path, {'ok': True})
                sleep.assert_not_called()
            with patch('runtime_io.os.replace', side_effect=OSError('disk-error')), patch('runtime_io.time.sleep') as sleep:
                with self.assertRaises(OSError):
                    atomic_write_json(path, {'new': True})
                sleep.assert_not_called()

    @unittest.skipUnless(os.name == 'nt', 'Windows sharing semantics')
    def test_real_reader_without_share_delete_does_not_crash_state_write(self):
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'monitor.json'
            path.write_text('{"ready":false}', encoding='utf-8')
            handle = kernel.CreateFileW(str(path), 0x80000000, 0x1 | 0x2, None, 3, 0x80, None)
            self.assertNotEqual(handle, wintypes.HANDLE(-1).value)
            timer = threading.Timer(0.03, lambda: kernel.CloseHandle(handle))
            timer.start()
            try:
                atomic_write_json(path, {'ready': True})
            finally:
                timer.join(timeout=1)
            self.assertEqual(json.loads(path.read_text()), {'ready': True})


if __name__ == '__main__':
    unittest.main()
