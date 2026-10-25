"""Own a temporary gateway tree from its first instruction, even on hard exit.

Windows 10+ assigns the child to a private kill-on-close job in CreateProcessW
itself. Assigning an already-running Popen child would leave a crash race.
No PID files, process-name kills, or handles to an existing gateway are used.
"""
from __future__ import annotations

import ctypes as c
from ctypes import wintypes as w
import os
from pathlib import Path
import subprocess
import time
from setup_diagnostics import InstallFailure


class PairingGateway:
    def __init__(self, command: list[str], *, cwd: Path, log):
        if os.name != 'nt':
            raise OSError('临时绑定网关的进程保护需要 Windows 10/11。')
        self._job = self._process = None
        self.pid = None
        self.log_path = Path(log.name)
        k = self._k = c.WinDLL('kernel32', use_last_error=True)
        signatures = {
            'CreateJobObjectW': ([c.c_void_p, w.LPCWSTR], w.HANDLE),
            'SetInformationJobObject': ([w.HANDLE, c.c_int, c.c_void_p, w.DWORD], w.BOOL),
            'QueryInformationJobObject': ([w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.c_void_p], w.BOOL),
            'InitializeProcThreadAttributeList': ([c.c_void_p, w.DWORD, w.DWORD, c.POINTER(c.c_size_t)], w.BOOL),
            'UpdateProcThreadAttribute': ([c.c_void_p, w.DWORD, c.c_size_t, c.c_void_p, c.c_size_t, c.c_void_p, c.c_void_p], w.BOOL),
            'DeleteProcThreadAttributeList': ([c.c_void_p], None),
            'CreateProcessW': ([w.LPCWSTR, w.LPWSTR, c.c_void_p, c.c_void_p, w.BOOL, w.DWORD,
                                c.c_void_p, w.LPCWSTR, c.c_void_p, c.c_void_p], w.BOOL),
            'TerminateJobObject': ([w.HANDLE, w.UINT], w.BOOL),
            'WaitForSingleObject': ([w.HANDLE, w.DWORD], w.DWORD),
            'GetExitCodeProcess': ([w.HANDLE, c.POINTER(w.DWORD)], w.BOOL),
            'CloseHandle': ([w.HANDLE], w.BOOL),
        }
        for name, (args, result) in signatures.items():
            getattr(k, name).argtypes, getattr(k, name).restype = args, result

        class Basic(c.Structure):
            _fields_ = [('process_time', c.c_int64), ('job_time', c.c_int64), ('flags', w.DWORD),
                        ('min_ws', c.c_size_t), ('max_ws', c.c_size_t), ('active', w.DWORD),
                        ('affinity', c.c_size_t), ('priority', w.DWORD), ('scheduling', w.DWORD)]

        class Extended(c.Structure):
            _fields_ = [('basic', Basic), ('io', c.c_uint64 * 6), ('process_memory', c.c_size_t),
                        ('job_memory', c.c_size_t), ('peak_process_memory', c.c_size_t), ('peak_job_memory', c.c_size_t)]

        class Startup(c.Structure):
            _fields_ = [('cb', w.DWORD), ('reserved', w.LPWSTR), ('desktop', w.LPWSTR), ('title', w.LPWSTR),
                        ('x', w.DWORD), ('y', w.DWORD), ('xsize', w.DWORD), ('ysize', w.DWORD),
                        ('xchars', w.DWORD), ('ychars', w.DWORD), ('fill', w.DWORD), ('flags', w.DWORD),
                        ('show', w.WORD), ('reserved2size', w.WORD), ('reserved2', c.c_void_p),
                        ('stdin', w.HANDLE), ('stdout', w.HANDLE), ('stderr', w.HANDLE)]

        class StartupEx(c.Structure):
            _fields_ = [('startup', Startup), ('attributes', c.c_void_p)]

        class ProcessInfo(c.Structure):
            _fields_ = [('process', w.HANDLE), ('thread', w.HANDLE), ('pid', w.DWORD), ('tid', w.DWORD)]

        initialized = False
        inherited = []
        try:
            self._job = k.CreateJobObjectW(None, None)  # private, not inheritable
            if not self._job:
                raise c.WinError(c.get_last_error())
            limits = Extended()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k.SetInformationJobObject(self._job, 9, c.byref(limits), c.sizeof(limits)):
                raise c.WinError(c.get_last_error())
            size = c.c_size_t()
            k.InitializeProcThreadAttributeList(None, 2, 0, c.byref(size))
            attributes = c.create_string_buffer(size.value)
            if not k.InitializeProcThreadAttributeList(attributes, 2, 0, c.byref(size)):
                raise c.WinError(c.get_last_error())
            initialized = True
            jobs = (w.HANDLE * 1)(self._job)
            if not k.UpdateProcThreadAttribute(attributes, 0, 0x2000D, jobs, c.sizeof(jobs), None, None):
                raise c.WinError(c.get_last_error())
            import msvcrt
            with open(os.devnull, 'rb') as null:
                for source in (msvcrt.get_osfhandle(null.fileno()), msvcrt.get_osfhandle(log.fileno())):
                    # Duplicate handles, not file descriptors; only these two
                    # may cross CreateProcess, never the job or setup lock.
                    handle = _duplicate_inheritable(source)
                    inherited.append(handle)
                handles = (w.HANDLE * 2)(*inherited)
                if not k.UpdateProcThreadAttribute(attributes, 0, 0x20002, handles, c.sizeof(handles), None, None):
                    raise c.WinError(c.get_last_error())
                si = StartupEx()
                si.startup.cb = c.sizeof(si)
                si.startup.flags = 0x100  # STARTF_USESTDHANDLES
                si.startup.stdin, si.startup.stdout, si.startup.stderr = inherited[0], inherited[1], inherited[1]
                si.attributes = c.addressof(attributes)
                pi = ProcessInfo()
                cmd = c.create_unicode_buffer(subprocess.list2cmdline(command))
                # A caller's daemon log redirection must not steal the READY
                # event from this wizard's append-only stdout/stderr log.
                env = {key: value for key, value in os.environ.items() if key.upper() != 'CC_LOG_FILE'}
                environment = c.create_unicode_buffer('\0'.join(f'{key}={value}' for key, value in
                                                                sorted(env.items(), key=lambda item: item[0].upper())) + '\0\0')
                # Job-list membership is atomic with creation; no executable
                # instruction can run outside our job, including descendants.
                if not k.CreateProcessW(str(command[0]), cmd, None, None, True,
                                       0x80000 | 0x8000000 | 0x400, environment, str(cwd), c.byref(si), c.byref(pi)):
                    raise c.WinError(c.get_last_error())
                self._process, self.pid = pi.process, pi.pid
                k.CloseHandle(pi.thread)
        except BaseException as error:
            self.close()
            if isinstance(error, OSError):
                raise InstallFailure('临时绑定网关启动', summary=f'Windows 错误码 {getattr(error, "winerror", None) or error.errno}；未启动未受保护的网关。',
                                     log_path=self.log_path) from None
            raise
        finally:
            for handle in inherited:
                k.CloseHandle(handle)
            if initialized:
                k.DeleteProcThreadAttributeList(attributes)

    def poll(self):
        if self._process is None:
            return None
        status = self._k.WaitForSingleObject(self._process, 0)
        if status == 258:
            return None
        if status != 0:
            raise c.WinError(c.get_last_error())
        code = w.DWORD()
        if not self._k.GetExitCodeProcess(self._process, c.byref(code)):
            raise c.WinError(c.get_last_error())
        return code.value

    def close(self):
        """Terminate only this job, wait for descendants, then close its handles."""
        try:
            if self._job:
                if not self._k.TerminateJobObject(self._job, 1):
                    raise c.WinError(c.get_last_error())
                # JOBOBJECT_BASIC_ACCOUNTING_INFORMATION: active count at 40.
                info = c.create_string_buffer(48)
                deadline = time.monotonic() + 5
                while True:
                    if not self._k.QueryInformationJobObject(self._job, 1, info, len(info), None):
                        raise c.WinError(c.get_last_error())
                    if c.c_uint32.from_buffer(info, 40).value == 0:
                        break
                    if time.monotonic() >= deadline:
                        raise TimeoutError('临时网关进程树清理超时；绑定进度已保留，未启用正式配置。')
                    time.sleep(0.02)
        except (OSError, TimeoutError) as error:
            raise InstallFailure('临时绑定网关清理', summary=f'{type(error).__name__}，Windows 错误码 {getattr(error, "winerror", None)}；进度已保留，未启用正式绑定。',
                                 log_path=self.log_path) from None
        finally:
            if self._job:
                self._k.CloseHandle(self._job)
                self._job = None
            if self._process:
                self._k.CloseHandle(self._process)
                self._process = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _duplicate_inheritable(handle):
    k = c.WinDLL('kernel32', use_last_error=True)
    k.GetCurrentProcess.restype = w.HANDLE
    k.DuplicateHandle.argtypes = [w.HANDLE, w.HANDLE, w.HANDLE, c.POINTER(w.HANDLE), w.DWORD, w.BOOL, w.DWORD]
    k.DuplicateHandle.restype = w.BOOL
    result = w.HANDLE()
    current = k.GetCurrentProcess()
    if not k.DuplicateHandle(current, handle, current, c.byref(result), 0, True, 2):
        raise c.WinError(c.get_last_error())
    return result.value


def wait_gateway_ready(child, log_path: Path, *, offset: int, timeout: float = 45,
                       sleep=time.sleep, clock=time.monotonic):
    """Pinned gateway logs READY only after QQ's authenticated READY dispatch.

    Read only this launch's bytes. A stale log entry, generic API socket, process
    creation, RESUMED or TCP connection is not the initial QQ READY handshake.
    """
    deadline, tail = clock() + timeout, b''
    with log_path.open('rb') as stream:
        stream.seek(offset)
        while True:
            code = child.poll()
            if code is not None:
                raise ValueError(f'QQ 网关就绪阶段提前退出（退出码 {code}）；日志：{log_path}')
            block = stream.read(65536)
            if block:
                tail += block
                lines = tail.split(b'\n')
                tail = lines.pop()[-4096:]
                for line in lines:
                    if b'level=INFO' in line and b'msg="qqbot: gateway READY"' in line:
                        if child.poll() is None:
                            return
            if clock() >= deadline:
                raise ValueError(f'QQ WebSocket READY 等待超时（{timeout:g} 秒）；日志：{log_path}')
            sleep(0.05)
