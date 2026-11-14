"""Use StdRegProv's system view, avoiding inherited registry virtualization."""
from __future__ import annotations
import base64
import json
import os
from pathlib import Path
import subprocess
import uuid
from runtime_io import atomic_write_json

SCRIPT = r'''
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$ProgressPreference = 'SilentlyContinue'
$ErrorActionPreference = 'Stop'
$p = [Console]::In.ReadToEnd() | ConvertFrom-Json
$r = [wmiclass]'\\.\root\default:StdRegProv'
$h = [uint32]2147483649
$run = 'Software\Microsoft\Windows\CurrentVersion\Run'
$approval = 'Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run'
$name = 'CodexQQAssistant'
function Read-Item($path, $binary) {
  $e = $r.EnumValues($h, $path)
  if ($e.ReturnValue -eq 2) { return $null }
  if ($e.ReturnValue -ne 0) { throw 'registry enum failed' }
  $i = [Array]::IndexOf([string[]]$e.sNames, $name)
  if ($i -lt 0) { return $null }
  $kind = [int]$e.Types[$i]
  if ($binary -and $kind -eq 3) {
    $v = $r.GetBinaryValue($h, $path, $name)
    if ($v.ReturnValue -ne 0) { throw 'registry read failed' }
    return @{kind=$kind; value=[Convert]::ToBase64String([byte[]]$v.uValue)}
  }
  if (-not $binary -and $kind -eq 1) {
    $v = $r.GetStringValue($h, $path, $name)
    if ($v.ReturnValue -ne 0) { throw 'registry read failed' }
    return @{kind=$kind; value=$v.sValue}
  }
  return @{kind=$kind; value=$null}
}
if ($p.mode -eq 'write') {
  foreach ($item in @(@{path=$run; data=$p.run; binary=$false}, @{path=$approval; data=$p.approval; binary=$true})) {
    if ($null -eq $item.data) {
      $result=$r.DeleteValue($h, $item.path, $name)
      if ($result.ReturnValue -notin @(0,2)) { throw 'registry delete failed' }
    } else {
      $result=$r.CreateKey($h, $item.path)
      if ($result.ReturnValue -ne 0) { throw 'registry create failed' }
      if ($item.binary -and $item.data.kind -eq 3) {
        $result=$r.SetBinaryValue($h, $item.path, $name, [Convert]::FromBase64String($item.data.value))
      } elseif (-not $item.binary -and $item.data.kind -eq 1) {
        $result=$r.SetStringValue($h, $item.path, $name, [string]$item.data.value)
      } else { throw 'registry kind invalid' }
      if ($result.ReturnValue -ne 0) { throw 'registry write failed' }
    }
  }
}
@{run=(Read-Item $run $false); approval=(Read-Item $approval $true)} | ConvertTo-Json -Compress -Depth 4
'''

class SystemStartupStore:
    def _call(self, payload):
        if os.name != 'nt':
            raise OSError('startup_windows_only')
        script = base64.b64encode(SCRIPT.encode('utf-16le')).decode('ascii')
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand', script],
                                input=json.dumps(payload).encode('ascii'), capture_output=True,
                                timeout=12, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode != 0:
            raise OSError('system_startup_registry_unavailable')
        return json.loads(result.stdout.decode('utf-8-sig'))

    def read(self):
        return self._call({'mode': 'read'})

    def write(self, value):
        return self._call({'mode': 'write', **value})

class StartupService:
    def __init__(self, data: Path, executable: Path, store=None):
        self.data, self.executable = data, executable
        self.store = store or SystemStartupStore()

    def status(self):
        from resident_app import startup_command
        try:
            item = self.store.read()
            matches = item['run'] == {'kind': 1, 'value': startup_command(self.executable, start_hidden=True)}
            approval = item['approval']
            state = 2
            if approval is not None:
                if approval.get('kind') != 3:
                    raise ValueError('startup_approval_unrecognized')
                raw = base64.b64decode(approval['value'], validate=True)
                if len(raw) < 4:
                    raise ValueError('startup_approval_unrecognized')
                state = int.from_bytes(raw[:4], 'little')
                if state not in (2, 3):
                    raise ValueError('startup_approval_unrecognized')
            return {'enabled': matches, 'verified': True, 'systemEnabled': matches and state == 2}
        except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
            return {'enabled': False, 'verified': False, 'systemEnabled': False}

    def set(self, enabled: bool, legacy: Path | None = None, *, enable_approval=True):
        from resident_app import startup_command
        before = self.store.read()
        expected = startup_command(self.executable, start_hidden=True)
        accepted = {expected, startup_command(self.executable)}
        if legacy:
            accepted.add(subprocess.list2cmdline([str(legacy.resolve())]))
        entry = before['run']
        if entry is not None and (entry.get('kind') != 1 or entry.get('value') not in accepted):
            raise ValueError('startup_entry_conflict_preserved')
        after = dict(before)
        after['run'] = {'kind': 1, 'value': expected} if enabled else None
        if enabled and enable_approval and before['approval'] is not None:
            approval = before['approval']
            if approval.get('kind') != 3:
                raise ValueError('startup_approval_unrecognized')
            raw = base64.b64decode(approval['value'], validate=True)
            if len(raw) < 4 or int.from_bytes(raw[:4], 'little') not in (2, 3):
                raise ValueError('startup_approval_unrecognized')
            after['approval'] = {'kind': 3, 'value': base64.b64encode((2).to_bytes(4,'little') + raw[4:]).decode()}
        if before == after:
            return {'changed': False, 'backupDir': ''}
        backup = self.data / 'install-backups' / ('desktop-app-startup-' + uuid.uuid4().hex)
        atomic_write_json(backup / 'registry-before.json', before)
        try:
            actual = self.store.write(after)
            if actual != after:
                raise OSError('startup_readback_failed')
        except Exception:
            self.store.write(before)
            raise
        return {'changed': True, 'backupDir': str(backup)}
