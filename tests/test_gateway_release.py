from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import fetch_gateway
import package_gateway_release


class GatewayReleaseTests(unittest.TestCase):
    def test_release_workflow_rejects_tag_with_wrong_embedded_version(self):
        workflow = (Path(__file__).resolve().parents[1] /
                    '.github/workflows/release-gateway.yml').read_text(encoding='utf-8')
        self.assertIn('$expected = "v$env:GATEWAY_VERSION"', workflow)
        self.assertIn('$env:RELEASE_TAG -ne $expected', workflow)

    def test_checksum_parser_requires_one_matching_asset(self):
        digest = 'a' * 64
        self.assertEqual(fetch_gateway._expected_checksum(f'{digest}  asset.zip\n', 'asset.zip'), digest)
        with self.assertRaises(ValueError):
            fetch_gateway._expected_checksum(f'{digest}  other.zip\n', 'asset.zip')
        with self.assertRaises(ValueError):
            fetch_gateway._expected_checksum(f'{digest}  asset.zip\n{digest}  asset.zip\n', 'asset.zip')

    def test_package_contains_only_public_provenance_and_verifies_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            binary = root / 'cc-connect-desktop-control.exe'
            binary.write_bytes(b'fake-verified-binary')
            output = root / 'dist'
            with patch.object(package_gateway_release, 'os', SimpleNamespace(name='nt', environ=os.environ)):
                result = package_gateway_release.package(
                    binary, output, tag='v1.5.0-desktop-control.6',
                    gateway_version='1.5.0-desktop-control.6')
            archive = Path(result['archive'])
            self.assertEqual(result['binary_sha256'], hashlib.sha256(binary.read_bytes()).hexdigest())
            with zipfile.ZipFile(archive) as bundle:
                self.assertEqual(set(bundle.namelist()), {
                    'cc-connect-desktop-control.exe', 'BUILD_INFO.json',
                    'THIRD_PARTY_NOTICES.md', 'CC_CONNECT_ADAPTER.md'})
                info = json.loads(bundle.read('BUILD_INFO.json'))
                self.assertEqual(info['upstream_revision'], package_gateway_release.UPSTREAM_REVISION)
                self.assertEqual(info['release_tag'], 'v1.5.0-desktop-control.6')

    def test_package_rejects_overwrite_and_unverified_tag(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            binary = root / 'gateway.exe'
            binary.write_bytes(b'fixture')
            with patch.object(package_gateway_release, 'os', SimpleNamespace(name='nt', environ=os.environ)):
                with self.assertRaisesRegex(ValueError, '发布标签'):
                    package_gateway_release.package(binary, root / 'dist',
                                                    tag='v0.1.0', gateway_version='1.5.0-desktop-control.6')
                with self.assertRaisesRegex(ValueError, '内部版本'):
                    package_gateway_release.package(binary, root / 'dist',
                                                    tag='v1.5.0-desktop-control.6',
                                                    gateway_version='1.5.0-desktop-control.5')
                output = root / 'dist'
                output.mkdir()
                existing = output / 'codex-qq-gateway-windows-x64-v1.5.0-desktop-control.6.zip'
                existing.write_bytes(b'preserved')
                with self.assertRaisesRegex(ValueError, '拒绝覆盖'):
                    package_gateway_release.package(binary, output,
                                                    tag='v1.5.0-desktop-control.6',
                                                    gateway_version='1.5.0-desktop-control.6')
                self.assertEqual(existing.read_bytes(), b'preserved')

    def test_verified_gateway_is_reused_without_network(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'gateway.exe'
            target.write_bytes(b'verified-fixture')
            with patch.object(fetch_gateway, 'BINARY_SHA256', fetch_gateway._sha256(target)), \
                    patch.object(fetch_gateway, '_download') as download:
                self.assertTrue(fetch_gateway.ensure_gateway(target)['cached'])
                download.assert_not_called()
            with self.assertRaises(ValueError):
                fetch_gateway.ensure_gateway(target)
            self.assertEqual(target.read_bytes(), b'verified-fixture')

    def fetch_fixture(self, target, binary=b'verified-fixture', *, before_publish=None, metadata_hash=None):
        expected = hashlib.sha256(binary).hexdigest()

        def download(url, destination):
            if destination.suffix == '.zip':
                info = {'release_tag': fetch_gateway.RELEASE_TAG,
                        'gateway_version': fetch_gateway.RELEASE_TAG.removeprefix('v'),
                        'upstream_revision': fetch_gateway.UPSTREAM_REVISION,
                        'platform': 'windows-x64', 'binary_sha256': metadata_hash or expected}
                with zipfile.ZipFile(destination, 'w') as bundle:
                    bundle.writestr('cc-connect-desktop-control.exe', binary)
                    bundle.writestr('BUILD_INFO.json', json.dumps(info))
                    bundle.writestr('THIRD_PARTY_NOTICES.md', 'fixture')
                    bundle.writestr('CC_CONNECT_ADAPTER.md', 'fixture')
            else:
                archive = destination.parent / fetch_gateway.ASSET_NAME
                destination.write_text(fetch_gateway._sha256(archive) + '  ' + fetch_gateway.ASSET_NAME, encoding='utf-8')

        publish = fetch_gateway._publish_no_replace

        def publishing(staged, output):
            if before_publish:
                before_publish(output)
            publish(staged, output)

        with patch.object(fetch_gateway, '_download', side_effect=download), \
                patch.object(fetch_gateway, 'BINARY_SHA256', expected), \
                patch.object(fetch_gateway, '_publish_no_replace', side_effect=publishing):
            return fetch_gateway.fetch(target=target)

    def test_release_download_validates_binary_and_publishes_once(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'gateway.exe'
            result = self.fetch_fixture(target)
            self.assertFalse(result['cached'])
            self.assertEqual(target.read_bytes(), b'verified-fixture')
            self.assertFalse(target.with_suffix('.staged.exe').exists())

    def test_download_does_not_replace_target_created_by_another_process(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'gateway.exe'
            with self.assertRaises(FileExistsError):
                self.fetch_fixture(target, before_publish=lambda path: path.write_bytes(b'other-install'))
            self.assertEqual(target.read_bytes(), b'other-install')
            self.assertFalse(target.with_suffix('.staged.exe').exists())

    def test_download_does_not_delete_someone_elses_staging_file(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'gateway.exe'
            staged = target.with_suffix('.staged.exe')
            staged.write_bytes(b'other-installer')
            with self.assertRaises(FileExistsError):
                self.fetch_fixture(target)
            self.assertEqual(staged.read_bytes(), b'other-installer')
            self.assertFalse(target.exists())

    def test_valid_staging_file_is_resumed_without_network(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'gateway.exe'
            staged = target.with_suffix('.staged.exe')
            staged.write_bytes(b'verified-fixture')
            with patch.object(fetch_gateway, 'BINARY_SHA256', fetch_gateway._sha256(staged)), \
                    patch.object(fetch_gateway, '_download') as download:
                result = fetch_gateway.fetch(target=target)
            self.assertFalse(result['cached'])
            self.assertEqual(target.read_bytes(), b'verified-fixture')
            self.assertFalse(staged.exists())
            download.assert_not_called()

    def test_download_rejects_bad_binary_provenance(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'gateway.exe'
            with self.assertRaises(ValueError):
                self.fetch_fixture(target, metadata_hash='f' * 64)
            self.assertFalse(target.exists())


if __name__ == '__main__':
    unittest.main()
