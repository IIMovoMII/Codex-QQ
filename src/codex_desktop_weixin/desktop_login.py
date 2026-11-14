"""Login-start evidence: registry readback and manual launch are insufficient."""
from __future__ import annotations
import ctypes as c
from ctypes import wintypes as w
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import time
from runtime_io import atomic_write_json

def session_context():
    if os.name != 'nt': raise OSError('login_windows_only')
    class Luid(c.Structure):
        _fields_ = [('low', w.DWORD), ('high', w.LONG)]
    class Statistics(c.Structure):
        _fields_ = [('token', Luid), ('authentication', Luid), ('expiration', c.c_int64),
                    ('type', w.DWORD), ('impersonation', w.DWORD), ('charged', w.DWORD),
                    ('available', w.DWORD), ('groups', w.DWORD), ('privileges', w.DWORD), ('modified', Luid)]
    class String(c.Structure):
        _fields_ = [('length', w.USHORT), ('maximum', w.USHORT), ('buffer', c.c_void_p)]
    class Session(c.Structure):
        _fields_ = [('size', w.DWORD), ('id', Luid), ('user', String), ('domain', String), ('package', String),
                    ('kind', w.DWORD), ('session', w.DWORD), ('sid', c.c_void_p), ('logon_at', c.c_int64)]
    kernel, advapi, security = c.WinDLL('kernel32', use_last_error=True), c.WinDLL('advapi32', use_last_error=True), c.WinDLL('secur32')
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    advapi.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, c.POINTER(w.HANDLE)]
    advapi.GetTokenInformation.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.POINTER(w.DWORD)]
    security.LsaGetLogonSessionData.argtypes = [c.POINTER(Luid), c.POINTER(c.POINTER(Session))]
    security.LsaFreeReturnBuffer.argtypes = [c.c_void_p]
    token = w.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, c.byref(token)): raise c.WinError(c.get_last_error())
    try:
        statistics, length = Statistics(), w.DWORD()
        if not advapi.GetTokenInformation(token, 10, c.byref(statistics), c.sizeof(statistics), c.byref(length)):
            raise c.WinError(c.get_last_error())
    finally: kernel.CloseHandle(token)
    pointer = c.POINTER(Session)()
    if security.LsaGetLogonSessionData(c.byref(statistics.authentication), c.byref(pointer)) != 0 or not pointer:
        raise OSError('login_session_unavailable')
    try:
        session = pointer.contents
        return dict(id=f'{session.id.high & 0xffffffff:08x}{session.id.low:08x}',
                    logonAt=session.logon_at/10_000_000-11644473600, kind=session.kind)
    finally: security.LsaFreeReturnBuffer(pointer)

@lru_cache(maxsize=8)
def _digest(path, size, modified):
    with Path(path).open('rb') as stream: return hashlib.file_digest(stream, 'sha256').hexdigest()

def executable_identity(executable):
    path = executable.resolve()
    stat = path.stat()
    return {'path': str(path), 'sha256': _digest(str(path), stat.st_size, stat.st_mtime_ns)}

class LoginEvidence:
    def __init__(self, data, executable, *, context=session_context, now=time.time):
        self.path = data / 'state/startup-verification.json'
        self.executable, self.context, self.now = executable, context, now

    def baseline(self):
        try:
            identity, context = executable_identity(self.executable), self.context()
            current = self.read()
            if current.get('executable') == identity:
                if current.get('stage') == 'verified': return
                configured = current.get('configuredAt')
                if (current.get('stage') in ('awaiting_login', 'login_detected')
                        and isinstance(current.get('baselineSession'), str) and current['baselineSession']
                        and type(configured) in (int, float) and math.isfinite(configured)):
                    return  # Host startup must not erase a previous logon's baseline.
            atomic_write_json(self.path, dict(version=1, stage='awaiting_login', executable=identity,
                              baselineSession=context['id'], configuredAt=self.now()))
        except (OSError, ValueError, KeyError, TypeError):
            pass  # Missing evidence never becomes a successful verification.

    def read(self):
        try:
            if self.path.stat().st_size > 8192: return {}
            value = json.loads(self.path.read_text(encoding='utf-8'))
            return value if value.get('version') == 1 else {}
        except (OSError, ValueError, AttributeError): return {}

    def observe_launch(self, startup):
        if not startup: return
        try:
            value, context, now = self.read(), self.context(), self.now()
            if (value.get('stage') not in ('awaiting_login', 'login_detected') or context['id'] == value['baselineSession']
                    or context['kind'] not in (2, 10, 11)
                    or not value['configuredAt'] < context['logonAt'] <= now
                    or now-context['logonAt'] > 300
                    or value['executable'] != executable_identity(self.executable)):
                return
            value.update(stage='login_detected', loginSession=context['id'], logonAt=context['logonAt'],
                         launchedAt=now, hostPid=os.getppid())
            atomic_write_json(self.path, value)
        except (OSError, ValueError, KeyError, TypeError): pass

    def verified(self, *, startup, runtime_ready, qq_ready):
        if not startup: return False
        try:
            value = self.read()
            if value.get('executable') != executable_identity(self.executable): return False
            if value.get('stage') == 'login_detected' and runtime_ready and qq_ready:
                # A launch candidate is tied to this exact Windows logon.
                if value['loginSession'] != self.context()['id']: return False
                value.update(stage='verified', qqReadyAt=self.now())
                atomic_write_json(self.path, value)
            return value.get('stage') == 'verified'
        except (OSError, ValueError, KeyError, TypeError): return False
