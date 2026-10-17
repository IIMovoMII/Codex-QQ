"""Real Windows job cleanup regression, using only temporary shadow apps."""
import ctypes as c
from ctypes import wintypes as w
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src/codex_desktop_weixin'))
from launcher import read_monitor_pid, request_stop
from process_watch import _snapshot


class Basic(c.Structure):
    _fields_ = [('process_time', c.c_int64), ('job_time', c.c_int64),
                ('flags', w.DWORD), ('min_ws', c.c_size_t), ('max_ws', c.c_size_t),
                ('active', w.DWORD), ('affinity', c.c_size_t),
                ('priority', w.DWORD), ('scheduling', w.DWORD)]


class Extended(c.Structure):
    _fields_ = [('basic', Basic), ('io', c.c_uint64 * 6),
                ('process_memory', c.c_size_t), ('job_memory', c.c_size_t),
                ('peak_process_memory', c.c_size_t), ('peak_job_memory', c.c_size_t)]


@unittest.skipUnless(os.name == 'nt', 'Windows job integration')
class ResidentJobTests(unittest.TestCase):
    def test_broker_survives_job_cleanup_that_kills_previous_direct_launch(self):
        if not any(name.lower() == 'explorer.exe' for _, name in _snapshot().values()):
            self.skipTest('requires an interactive Windows Explorer desktop; not available on headless CI')
        if not (ROOT / 'bin/CodexQQ助手.exe').is_file():
            self.skipTest('build the local launcher before this integration test')
        k = c.WinDLL('kernel32', use_last_error=True)
        k.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
        k.CreateJobObjectW.restype = w.HANDLE
        k.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
        k.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        k.OpenProcess.restype = w.HANDLE
        k.IsProcessInJob.argtypes = [w.HANDLE, w.HANDLE, c.POINTER(w.BOOL)]
        k.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        k.CloseHandle.argtypes = [w.HANDLE]
        for mode in ('direct', 'broker'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp:
                base = Path(temp) / '独立 启动 & 测试'
                home, data = base / '.codex', base / 'data'
                job = k.CreateJobObjectW(None, None)
                self.assertTrue(job)
                process = None
                handle = None
                try:
                    limits = Extended()
                    limits.basic.flags = 0x2000  # Only this isolated job, kill on close.
                    self.assertTrue(k.SetInformationJobObject(job, 9, c.byref(limits), c.sizeof(limits)))
                    process = subprocess.Popen([sys.executable, '-X', 'utf8',
                        str(ROOT / 'tests/helpers/resident_job_child.py'), mode, str(home), str(data)],
                        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW)
                    self.assertTrue(k.AssignProcessToJobObject(job, int(process._handle)))
                    process.stdin.write(b'launch\n')
                    process.stdin.flush()
                    deadline = time.monotonic() + 8
                    app_pid = None
                    while time.monotonic() < deadline:
                        app_pid = read_monitor_pid(data, require_ready=True)
                        if app_pid: break
                        time.sleep(0.05)
                    self.assertIsNotNone(app_pid, 'isolated app never became ready')
                    handle = k.OpenProcess(0x101000, False, app_pid)
                    in_job = w.BOOL()
                    self.assertTrue(k.IsProcessInJob(handle, job, c.byref(in_job)))
                    self.assertEqual(bool(in_job.value), mode == 'direct')
                    if mode == 'broker':
                        parent_pid, parent_name = _snapshot()[app_pid]
                        self.assertEqual(_snapshot()[parent_pid][1].lower(), 'explorer.exe')
                    k.CloseHandle(job)
                    job = None
                    process.wait(timeout=4)
                    time.sleep(0.15)
                    if mode == 'direct':
                        self.assertIsNone(read_monitor_pid(data))
                    else:
                        self.assertEqual(read_monitor_pid(data, require_ready=True), app_pid)
                finally:
                    if job: k.CloseHandle(job)
                    if process:
                        process.wait(timeout=4)
                        process.stdin.close()
                    request_stop(data, close_desktop=False)
                    deadline = time.monotonic() + 8
                    while read_monitor_pid(data) and time.monotonic() < deadline:
                        time.sleep(0.05)
                    self.assertIsNone(read_monitor_pid(data))
                    if handle:
                        try:
                            self.assertEqual(k.WaitForSingleObject(handle, 8000), 0)
                        finally:
                            k.CloseHandle(handle)


if __name__ == '__main__':
    unittest.main()
