"""Standalone NSIS write-ahead recovery; only manifest-owned program files.

This file also runs from the installer's private, verified Python bootstrap.
It never opens QQ settings, Codex credentials, transcripts or runtime data.
"""
from __future__ import annotations
import argparse
import base64
import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from functools import wraps

MAIN = 'codex-qq-desktop.exe'
EXTRAS = (MAIN, 'WebView2Loader.dll', 'uninstall.exe', 'build-manifest.json', 'LICENSE', 'THIRD_PARTY_NOTICES.md')
REGISTRY = (r'Software\Microsoft\Windows\CurrentVersion\Uninstall\Codex QQ 助手',
            r'Software\CodexQQ\Codex QQ 助手')

def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)

def child(root, relative):
    rel = PurePosixPath(relative)
    if (not relative or '\\' in relative or ':' in relative or rel.is_absolute()
            or any(p in ('..', '.') for p in rel.parts)):
        raise ValueError('install_path_invalid')
    path = root.joinpath(*rel.parts)
    # Resolve every existing parent; junctions and symlinks cannot redirect a
    # manifest operation outside the explicitly selected installation.
    if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink():
        raise ValueError('install_path_redirected')
    return path

def owned(manifest):
    if manifest.stat().st_size > 512*1024:
        raise ValueError('install_manifest_too_large')
    value = json.loads(manifest.read_text(encoding='utf-8'))
    files = value['files']
    if not isinstance(files, dict) or len(files) > 2048:
        raise ValueError('install_manifest_invalid')
    for relative, digest in files.items():
        child(manifest.parent, relative)
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('install_manifest_invalid')
    return set(files) | set(EXTRAS)

def registry_snapshot():
    if os.name != 'nt': return {}
    import winreg
    result = {}
    for name in REGISTRY:
        values = []
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, name) as key:
                for i in range(winreg.QueryInfoKey(key)[1]):
                    field, value, kind = winreg.EnumValue(key, i)
                    if kind == winreg.REG_BINARY: value = base64.b64encode(value).decode('ascii')
                    values.append([field, value, kind])
        except FileNotFoundError:
            pass
        result[name] = values
    return result

def registry_restore(values):
    if os.name != 'nt': return
    import winreg
    if set(values) != set(REGISTRY): raise ValueError('install_registry_invalid')
    for name, fields in values.items():
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, name, access=winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
            current = [winreg.EnumValue(key, i)[0] for i in range(winreg.QueryInfoKey(key)[1])]
            for field in current: winreg.DeleteValue(key, field)
            for field, value, kind in fields:
                if kind == winreg.REG_BINARY: value = base64.b64decode(value, validate=True)
                winreg.SetValueEx(key, field, 0, kind, value)

def installer_identity(pid):
    """Return creation time and liveness from one handle; no PID reuse guess."""
    if os.name != 'nt': return None
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)]*4
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x00101000, False, pid)
    if not handle:
        if ctypes.get_last_error() == 87: return None
        raise OSError('installer_identity_unavailable')
    try:
        created, ended, system, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(ended), ctypes.byref(system), ctypes.byref(user)):
            raise OSError('installer_identity_unavailable')
        return (created.dwHighDateTime << 32 | created.dwLowDateTime, kernel.WaitForSingleObject(handle, 0) == 258)
    finally:
        kernel.CloseHandle(handle)

def serialized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self.lock():
            return method(self, *args, **kwargs)
    return call

class InstallTransaction:
    def __init__(self, target: Path, state_root: Path | None = None):
        if target.is_symlink() or getattr(target, 'is_junction', lambda: False)():
            raise ValueError('install_root_invalid')
        self.target = target.resolve()
        if self.target == Path(self.target.anchor) or self.target.is_symlink():
            raise ValueError('install_root_invalid')
        ident = hashlib.sha256(os.path.normcase(str(self.target)).encode('utf-8')).hexdigest()[:24]
        base = state_root or Path(os.environ['LOCALAPPDATA']) / 'CodexQQInstaller'
        self.base = base.resolve() / ident
        self.marker = self.base / 'transaction.json'

    @contextmanager
    def lock(self):
        if os.name != 'nt':
            yield
            return
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel.CreateMutexW.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.ReleaseMutex.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.CreateMutexW(None, False, 'Local\\CodexQQRecovery-' + self.base.name)
        if not handle: raise OSError('install_lock_unavailable')
        acquired = False
        try:
            acquired = kernel.WaitForSingleObject(handle, 10000) in (0, 128)
            if not acquired: raise OSError('install_recovery_busy')
            yield
        finally:
            if acquired: kernel.ReleaseMutex(handle)
            kernel.CloseHandle(handle)

    def read(self):
        value = json.loads(self.marker.read_text(encoding='utf-8'))
        if value.get('target') != str(self.target) or value.get('version') != 1:
            raise ValueError('install_journal_invalid')
        ident = value.get('id', '')
        if len(ident) != 32 or any(c not in '0123456789abcdef' for c in ident):
            raise ValueError('install_journal_invalid')
        return value, self.base / ('backup-' + ident)

    @serialized
    def prepare(self, incoming: Path, bootstrap: Path, installer_pid: int, *, watch=True):
        if self.marker.exists():
            previous, _ = self.read()
            if previous['stage'] == 'prepared':
                identity = installer_identity(previous['installerPid'])
                if identity and identity[1] and previous.get('installerCreated') in (None, identity[0]):
                    raise ValueError('install_recovery_pending')
                self.rollback()
        names = owned(incoming)
        incoming_names = sorted(names)
        previous_manifest = self.target / 'build-manifest.json'
        if previous_manifest.is_file(): names |= owned(previous_manifest)
        names = sorted(names)
        present = {name: sha(child(self.target, name)) for name in names if child(self.target, name).is_file()}
        total = sum(child(self.target, name).stat().st_size for name in present)
        if total > 512*1024*1024: raise ValueError('install_backup_too_large')
        self.base.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(self.base).free < total + 150*1024*1024:
            raise ValueError('install_backup_space_insufficient')
        ident = uuid.uuid4().hex
        backup = self.base / ('backup-' + ident)
        for name in present:
            destination = child(backup / 'files', name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(child(self.target, name), destination)
            if sha(destination) != present[name]: raise ValueError('install_backup_changed')
        # The recovery process lives outside INSTDIR, including its interpreter.
        # Neither overwriting Python nor terminating NSIS invalidates recovery.
        worker = backup / 'worker'
        shutil.copytree(bootstrap, worker / 'python')
        shutil.copy2(Path(__file__), worker / 'maintenance.py')
        value = dict(version=1, id=ident, target=str(self.target), stage='prepared',
                     incomingVersion=json.loads(incoming.read_text(encoding='utf-8'))['version'],
                     incomingFiles=incoming_names,
                     files=names, before=present, registry=registry_snapshot(),
                     installerPid=installer_pid, preparedAt=time.time())
        identity = installer_identity(installer_pid) if watch else None
        if watch and (not identity or not identity[1]):
            raise ValueError('installer_not_running')
        value['installerCreated'] = identity[0] if identity else None
        atomic(backup / 'record.json', value)
        atomic(self.marker, value)
        self.prune_backups(ident)
        if watch:
            self.watcher = subprocess.Popen([str(worker / 'python/python.exe'), '-I', '-B', str(worker / 'maintenance.py'),
                              'watch', str(self.target), '--transaction-id', ident], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=0x08000000 if os.name == 'nt' else 0)
        return value

    def prune_backups(self, current):
        candidates = []
        for directory in self.base.glob('backup-*'):
            if directory.is_symlink() or getattr(directory, 'is_junction', lambda: False)(): continue
            try:
                record = json.loads((directory / 'record.json').read_text(encoding='utf-8'))
                if (record.get('target') == str(self.target) and record.get('version') == 1
                        and directory.name == 'backup-' + record.get('id', '')
                        and len(record.get('id', '')) == 32 and record['id'] != current):
                    candidates.append(directory)
            except (OSError, ValueError, TypeError):
                continue
        for directory in sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)[2:]:
            try: shutil.rmtree(directory)
            except OSError: pass  # A closing recovery worker can still hold its interpreter.

    @serialized
    def commit(self):
        value, _ = self.read()
        if value['stage'] == 'rolled_back': raise ValueError('install_already_rolled_back')
        for name in set(value['files']) - set(value.get('incomingFiles', value['files'])):
            path = child(self.target, name)
            if name in value['before'] and path.is_file() and sha(path) == value['before'][name]:
                path.unlink()
        value['stage'] = 'committed'
        value['committedAt'] = time.time()
        atomic(self.marker, value)

    @serialized
    def rollback(self, expected_id=None):
        value, backup = self.read()
        if expected_id is not None and value['id'] != expected_id: return
        if value['stage'] != 'prepared': return
        before = value['before']
        for name, digest in before.items():
            if name not in value['files'] or sha(child(backup / 'files', name)) != digest:
                raise ValueError('install_backup_integrity_failed')
        for name in value['files']:
            target = child(self.target, name)
            if target.is_file():
                if name in before and sha(target) == before[name]: continue
                quarantine = child(backup / 'failed-program', name)
                quarantine.parent.mkdir(parents=True, exist_ok=True)
                # The user may install on D: while recovery state lives on C:.
                # Copy/verify before deleting this exact manifest-owned file.
                digest=sha(target)
                shutil.copy2(target, quarantine)
                if sha(quarantine)!=digest or sha(target)!=digest:
                    raise ValueError('install_failed_file_changed')
                target.unlink()
            if name in before:
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_suffix(target.suffix + '.restore')
                shutil.copy2(child(backup / 'files', name), temporary)
                os.replace(temporary, target)
        registry_restore(value['registry'])
        value['stage'] = 'rolled_back'
        value['rolledBackAt'] = time.time()
        atomic(self.marker, value)

    def watch(self, transaction_id=None):
        value, _ = self.read()
        if transaction_id != value['id']: return
        if os.name != 'nt': raise ValueError('install_watch_windows_only')
        deadline = time.monotonic() + 30*60
        while time.monotonic() < deadline:
            current = self.read()[0]
            if current['id'] != transaction_id or current['stage'] != 'prepared': return
            identity = installer_identity(value['installerPid'])
            if not identity or not identity[1] or identity[0] != value.get('installerCreated'): break
            time.sleep(1)  # Separate recovery process; no UI or bridge thread waits.
        else:
            return  # Never roll back an installer that is still alive.
        for attempt in range(10):
            try:
                if self.read()[0]['id'] != transaction_id: return
                self.rollback(transaction_id)
                return
            except OSError:
                time.sleep(1)
        with self.lock():
            current,_=self.read()
            if current['id']==transaction_id and current['stage']=='prepared':
                atomic(self.marker,{**current,'recoveryError':'program_restore_failed','recoveryCheckedAt':time.time()})

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('prepare', 'commit', 'rollback', 'watch'))
    parser.add_argument('target', type=Path)
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--bootstrap', type=Path)
    parser.add_argument('--installer-pid', type=int)
    parser.add_argument('--transaction-id')
    args = parser.parse_args()
    try:
        tx = InstallTransaction(args.target)
        if args.command == 'prepare':
            if not args.manifest or not args.bootstrap or not args.installer_pid:
                raise ValueError('install_arguments_invalid')
            tx.prepare(args.manifest, args.bootstrap, args.installer_pid)
        elif args.command == 'watch': tx.watch(args.transaction_id)
        else: getattr(tx, args.command)()
        return 0
    except (OSError, ValueError, KeyError, TypeError):
        print('install_maintenance_failed', file=sys.stderr)
        return 4

if __name__ == '__main__':
    raise SystemExit(main())
