from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import fetch_gateway
import package_gateway_release


class GatewayReleaseTests(unittest.TestCase):
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
            result = package_gateway_release.package(
                binary, output, tag='v1.5.0-desktop-control.5',
                gateway_version='1.5.0-desktop-control.5')
            archive = Path(result['archive'])
            self.assertEqual(result['binary_sha256'], hashlib.sha256(binary.read_bytes()).hexdigest())
            with zipfile.ZipFile(archive) as bundle:
                self.assertEqual(set(bundle.namelist()), {
                    'cc-connect-desktop-control.exe', 'BUILD_INFO.json',
                    'THIRD_PARTY_NOTICES.md', 'CC_CONNECT_ADAPTER.md'})
                info = json.loads(bundle.read('BUILD_INFO.json'))
                self.assertEqual(info['upstream_revision'], package_gateway_release.UPSTREAM_REVISION)
                self.assertEqual(info['release_tag'], 'v1.5.0-desktop-control.5')

    def test_package_rejects_overwrite_and_unverified_tag(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            binary = root / 'gateway.exe'
            binary.write_bytes(b'fixture')
            with self.assertRaises(ValueError):
                package_gateway_release.package(binary, root / 'dist',
                                                tag='v0.1.0', gateway_version='1.5.0-desktop-control.5')


if __name__ == '__main__':
    unittest.main()
