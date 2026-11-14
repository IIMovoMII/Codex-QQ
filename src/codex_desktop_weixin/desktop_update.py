"""Durable installer intent; success requires the matching committed program."""
from __future__ import annotations
import json
import re
import time
from pathlib import Path

from package_install import InstallTransaction
from runtime_io import atomic_write_json

class UpdateReceipt:
    def __init__(self, data: Path, executable: Path, operations):
        self.path = data / 'state/desktop-update.json'
        self.root = executable.parent
        self.operations = operations

    def prepare(self, operation: str, params: dict) -> dict:
        version, source, digest = (params.get(k) for k in ('version', 'sourceVersion', 'sha256'))
        if any(not isinstance(v, str) or not re.fullmatch(r'\d+\.\d+\.\d+', v) for v in (version, source)):
            raise ValueError('update_version_invalid')
        if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise ValueError('update_digest_invalid')
        installed = json.loads((self.root / 'build-manifest.json').read_text(encoding='utf-8'))
        if installed.get('version') != source or tuple(map(int, version.split('.'))) <= tuple(map(int, source.split('.'))):
            raise ValueError('update_version_not_newer')
        if self.path.exists() and self.read().get('status') == 'pending':
            raise ValueError('update_already_pending')
        value = dict(operationId=operation, sourceVersion=source, targetVersion=version,
                     sha256=digest, status='pending', preparedAt=time.time())
        atomic_write_json(self.path, value)
        return {'update': {'status': 'prepared', 'version': version}}

    def read(self):
        if self.path.stat().st_size > 8192:
            raise ValueError('update_receipt_invalid')
        value = json.loads(self.path.read_text(encoding='utf-8'))
        if not isinstance(value.get('operationId'), str) or not 1 <= len(value['operationId']) <= 128:
            raise ValueError('update_receipt_invalid')
        return value

    def finish(self, value, status, error=None):
        operation = value['operationId']
        response = dict(requestId=operation, operationId=operation, status='unknown' if error else 'confirmed', preview=False)
        if error:
            response['errorCode'] = error
        else:
            response['data'] = {'update': {'status': status, 'version': value['targetVersion']}}
        self.operations.finish(operation, response)
        atomic_write_json(self.path, {**value, 'status': status, 'finishedAt': time.time()})

    def fail(self, operation):
        value = self.read()
        if value['operationId'] != operation or value['status'] != 'pending':
            raise ValueError('update_operation_mismatch')
        self.finish(value, 'failed', 'update_install_not_confirmed')

    def reconcile(self):
        if not self.path.exists():
            return
        value = self.read()
        if value['status'] != 'pending':
            return
        manifest = json.loads((self.root / 'build-manifest.json').read_text(encoding='utf-8'))
        try:
            transaction, _ = InstallTransaction(self.root).read()
        except FileNotFoundError:
            return  # The installer may not have started; intent remains unknown.
        if transaction.get('preparedAt', 0) < value['preparedAt']:
            return
        if transaction.get('incomingVersion') != value['targetVersion']:
            return
        if manifest.get('version') == value['targetVersion'] and transaction['stage'] == 'committed':
            self.finish(value, 'installed')
        elif manifest.get('version') == value['sourceVersion'] and transaction['stage'] == 'rolled_back':
            self.finish(value, 'rolled_back', 'update_rolled_back')
