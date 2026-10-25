"""Package the verified CC Connect gateway for a GitHub release."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_REVISION = '17c61062c2f9ce9bcdd45a2082e491f9743a2770'


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def package(binary: Path, output_dir: Path, *, tag: str,
            gateway_version: str, upstream_revision: str = UPSTREAM_REVISION) -> dict:
    binary = binary.resolve()
    if os.name != 'nt' or binary.suffix.lower() != '.exe' or not binary.is_file():
        raise ValueError('需要 Windows 网关 exe 文件')
    if not re.fullmatch(r'v1\.5\.0-desktop-control\.\d+', tag):
        raise ValueError('发布标签必须与配套网关版本一致，例如 v1.5.0-desktop-control.5')
    if gateway_version != tag.removeprefix('v'):
        raise ValueError('网关内部版本必须与发布标签一致，拒绝生成自相矛盾的 BUILD_INFO')
    if upstream_revision != UPSTREAM_REVISION:
        raise ValueError('上游固定提交不匹配，拒绝打包')
    output_dir.mkdir(parents=True, exist_ok=True)
    asset_name = f'codex-qq-gateway-windows-x64-{tag}.zip'
    archive = output_dir / asset_name
    checksums = output_dir / 'SHA256SUMS.txt'
    if archive.exists() or checksums.exists():
        raise ValueError('发布输出已存在，拒绝覆盖')
    info = {
        'schema': 1,
        'product': 'Codex-QQ',
        'component': 'CC Connect desktop control gateway',
        'platform': 'windows-x64',
        'gateway_version': gateway_version,
        'release_tag': tag,
        'upstream_repository': 'https://github.com/chenhg5/cc-connect',
        'upstream_revision': upstream_revision,
        'codex_qq_commit': os.environ.get('GITHUB_SHA', 'local-build'),
        'binary_name': 'cc-connect-desktop-control.exe',
        'binary_sha256': sha256(binary),
    }
    build_info = json.dumps(info, ensure_ascii=False, indent=2) + '\n'
    notice = (ROOT / 'THIRD_PARTY_NOTICES.md').read_text(encoding='utf-8')
    adapter_readme = (ROOT / 'vendor/cc-connect/v1.5.0-desktop-control/README.md').read_text(encoding='utf-8')
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        bundle.write(binary, 'cc-connect-desktop-control.exe')
        bundle.writestr('BUILD_INFO.json', build_info)
        bundle.writestr('THIRD_PARTY_NOTICES.md', notice)
        bundle.writestr('CC_CONNECT_ADAPTER.md', adapter_readme)
    line = f'{sha256(archive)}  {asset_name}\n'
    checksums.write_text(line, encoding='utf-8', newline='\n')
    return {'archive': str(archive), 'checksums': str(checksums), **info,
            'archive_sha256': sha256(archive), 'archive_bytes': archive.stat().st_size}


def main() -> int:
    parser = argparse.ArgumentParser(description='打包已验证的 CC Connect 网关，不启动或替换运行服务')
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--release-tag', required=True)
    parser.add_argument('--gateway-version', default='1.5.0-desktop-control.5')
    args = parser.parse_args()
    result = package(args.binary, args.output_dir, tag=args.release_tag,
                     gateway_version=args.gateway_version)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
