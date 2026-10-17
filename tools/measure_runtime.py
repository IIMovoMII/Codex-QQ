"""Read-only short process-resource sample; no Desktop/model operations."""
from __future__ import annotations

import argparse
import ctypes
import json
import sys
import time
from ctypes import wintypes as w
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/codex_desktop_weixin'))
from launcher import read_monitor_pid, read_cc_connect_pid
from runtime_io import default_data_dir


class Memory(ctypes.Structure):
    _fields_ = [('cb', w.DWORD), ('PageFaultCount', w.DWORD)] + [(name, ctypes.c_size_t) for name in (
        'PeakWorkingSetSize', 'WorkingSetSize', 'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage',
        'QuotaPeakNonPagedPoolUsage', 'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage', 'PrivateUsage')]


def sample(pid: int) -> dict:
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    kernel.OpenProcess.argtypes, kernel.OpenProcess.restype = [w.DWORD, w.BOOL, w.DWORD], w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.GetProcessTimes.argtypes = [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4
    kernel.GetProcessTimes.restype = w.BOOL
    psapi.GetProcessMemoryInfo.argtypes = [w.HANDLE, ctypes.POINTER(Memory), w.DWORD]
    psapi.GetProcessMemoryInfo.restype = w.BOOL
    handle = kernel.OpenProcess(0x1000 | 0x10, False, pid)
    if not handle: raise ctypes.WinError(ctypes.get_last_error())
    try:
        create, end, system, user = (w.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle, ctypes.byref(create), ctypes.byref(end), ctypes.byref(system), ctypes.byref(user)):
            raise ctypes.WinError(ctypes.get_last_error())
        tick = lambda field: (field.dwHighDateTime << 32) | field.dwLowDateTime
        memory = Memory()
        memory.cb = ctypes.sizeof(memory)
        if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(memory), memory.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return {'created': tick(create), 'cpu': (tick(system) + tick(user)) / 10000000,
                'working_mib': memory.WorkingSetSize / 1048576, 'private_mib': memory.PrivateUsage / 1048576}
    finally:
        kernel.CloseHandle(handle)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', type=Path, default=default_data_dir())
    parser.add_argument('--seconds', type=float, default=10)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 30: parser.error('sample range: 1..30 seconds')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetActiveProcessorCount.argtypes, kernel.GetActiveProcessorCount.restype = [w.WORD], w.DWORD
    cores = kernel.GetActiveProcessorCount(0xffff)
    pids = {'monitor_or_app': read_monitor_pid(args.data_dir, require_ready=True),
            'cc_connect': read_cc_connect_pid(args.data_dir)}
    before = {name: sample(pid) if pid else None for name, pid in pids.items()}
    start = time.monotonic()
    time.sleep(args.seconds)
    duration = time.monotonic() - start
    result = {}
    for name, pid in pids.items():
        if not pid:
            result[name] = {'running': False}
            continue
        after = sample(pid)
        old = before[name]
        if old['created'] != after['created']: raise RuntimeError('process-identity-changed')
        delta = max(0, after['cpu'] - old['cpu']) / duration * 100
        result[name] = {'pid': pid, 'working_mib': round(after['working_mib'], 2),
                        'private_mib': round(after['private_mib'], 2), 'working_delta_mib': round(after['working_mib'] - old['working_mib'], 2),
                        'cpu_single_core_percent': round(delta, 3), 'cpu_machine_percent': round(delta / cores, 4)}
    print(json.dumps({'sample_seconds': round(duration, 2), 'logical_processors': cores, 'processes': result}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
