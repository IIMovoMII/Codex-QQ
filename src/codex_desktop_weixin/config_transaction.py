"""Recoverable local configuration replacement; never overwrite an intervening edit."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import uuid
from runtime_io import acquire_file_lock, atomic_write_json

def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()

def _write(path: Path, value: bytes):
    part = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with part.open('xb') as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(part, path)
    finally:
        part.unlink(missing_ok=True)

class ConfigTransaction:
    def __init__(self, data: Path):
        self.data = data.resolve()
        self.marker = self.data / 'state/config-change.json'

    def recover(self, *, rollback=False):
        if not self.marker.exists():
            return
        item = json.loads(self.marker.read_text(encoding='utf-8'))
        ident = item.get('id', '')
        if item.get('version') != 1 or not re.fullmatch('[0-9a-f]{32}', ident):
            raise ValueError('config_transaction_invalid')
        backup = self.data / 'install-backups' / ('config-' + ident)
        changes = item.get('files')
        if not isinstance(changes, list) or not 1 <= len(changes) <= 2:
            raise ValueError('config_transaction_invalid')
        payloads = []
        for i, entry in enumerate(changes):
            target = Path(entry['path'])
            # The original settings file authorizes its exact external TOML path.
            # The immutable backup contains that authorization for crash recovery.
            authorized = json.loads((backup / 'settings-authority.json').read_text())
            allowed = {self.data / 'settings.json', Path(authorized.get('config_path', '')).resolve()}
            if not target.is_absolute() or target.resolve() not in allowed or target.is_symlink():
                raise ValueError('config_transaction_path_changed')
            before, after = (backup / f'{i}.before').read_bytes(), (backup / f'{i}.after').read_bytes()
            if digest(before) != entry['before'] or digest(after) != entry['after']:
                raise ValueError('config_transaction_backup_changed')
            if digest(target.read_bytes()) not in (entry['before'], entry['after']):
                raise ValueError('config_transaction_intervening_edit_preserved')
            payloads.append((target, before if rollback else after))
        for target, payload in payloads:
            if target.read_bytes() != payload:
                _write(target, payload)
        self.marker.unlink()

    def commit(self, changes: list[tuple[Path, bytes, bytes]]):
        self.data.joinpath('state').mkdir(parents=True, exist_ok=True)
        lock = acquire_file_lock(self.data / 'state/install-migration.lock')
        if lock is None:
            raise ValueError('config_transaction_busy')
        try:
            self.recover()
            ident = uuid.uuid4().hex
            backup = self.data / 'install-backups' / ('config-' + ident)
            backup.mkdir(parents=True)
            authority = json.loads((self.data / 'settings.json').read_text(encoding='utf-8-sig'))
            atomic_write_json(backup / 'settings-authority.json', authority)
            files = []
            for i, (target, before, after) in enumerate(changes):
                if target.read_bytes() != before:
                    raise ValueError('config_transaction_intervening_edit_preserved')
                for label, payload in (('before', before), ('after', after)):
                    with (backup / f'{i}.{label}').open('xb') as stream:
                        stream.write(payload)
                        stream.flush()
                        os.fsync(stream.fileno())
                files.append({'path': str(target.resolve()), 'before': digest(before), 'after': digest(after)})
            atomic_write_json(self.marker, {'version': 1, 'id': ident, 'files': files})
            try:
                self.recover()
            except Exception:
                self.recover(rollback=True)
                raise
        finally:
            os.close(lock[0])
