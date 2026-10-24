"""Download the pinned, prebuilt CC Connect gateway and verify its release checksum."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = 'IIMovoMII/Codex-QQ'
RELEASE_TAG = 'v1.5.0-desktop-control.5'
ASSET_NAME = f'codex-qq-gateway-windows-x64-{RELEASE_TAG}.zip'
CHECKSUM_NAME = 'SHA256SUMS.txt'
UPSTREAM_REVISION = '17c61062c2f9ce9bcdd45a2082e491f9743a2770'
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024


def _download(url: str, path: Path) -> None:
    if not url.startswith('https://github.com/'):
        raise ValueError('只允许从本项目 GitHub 发布地址下载')
    request = urllib.request.Request(url, headers={'User-Agent': 'Codex-QQ-installer/1'})
    with urllib.request.urlopen(request, timeout=45) as response, path.open('wb') as output:
        total = 0
        while True:
            block = response.read(1024 * 1024)
            if not block:
                break
            total += len(block)
            if total > MAX_DOWNLOAD_BYTES:
                raise ValueError('下载文件超过安全大小限制')
            output.write(block)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _expected_checksum(text: str, asset_name: str) -> str:
    matches = []
    for line in text.splitlines():
        fields = line.strip().split()
        if len(fields) == 2 and fields[1].lstrip('*') == asset_name:
            matches.append(fields[0].lower())
    if len(matches) != 1 or len(matches[0]) != 64 or any(ch not in '0123456789abcdef' for ch in matches[0]):
        raise ValueError('发布校验文件缺少唯一的网关校验值')
    return matches[0]


def fetch(*, target: Path, tag: str = RELEASE_TAG, repository: str = REPOSITORY) -> dict:
    if target.exists():
        raise ValueError('网关目标已存在，未覆盖；如需更新请先退出助手并按更新流程操作')
    if tag != RELEASE_TAG or repository != REPOSITORY:
        raise ValueError('未核实的网关发布标签或仓库')
    target = target.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    base = f'https://github.com/{repository}/releases/download/{tag}'
    with tempfile.TemporaryDirectory(prefix='codex-qq-gateway-') as temp_name:
        temp = Path(temp_name)
        archive = temp / ASSET_NAME
        checksum_file = temp / CHECKSUM_NAME
        _download(f'{base}/{ASSET_NAME}', archive)
        _download(f'{base}/{CHECKSUM_NAME}', checksum_file)
        expected = _expected_checksum(checksum_file.read_text(encoding='utf-8'), ASSET_NAME)
        actual = _sha256(archive)
        if actual != expected:
            raise ValueError('网关发布包校验失败，未写入本机')
        with zipfile.ZipFile(archive) as bundle:
            names = set(bundle.namelist())
            required = {'cc-connect-desktop-control.exe', 'BUILD_INFO.json',
                        'THIRD_PARTY_NOTICES.md', 'CC_CONNECT_ADAPTER.md'}
            if names != required:
                raise ValueError('网关发布包内容不符合预期，未写入本机')
            info = json.loads(bundle.read('BUILD_INFO.json').decode('utf-8'))
            if (info.get('release_tag') != tag or info.get('upstream_revision') != UPSTREAM_REVISION
                    or info.get('platform') != 'windows-x64'):
                raise ValueError('网关发布包来源信息不匹配，未写入本机')
            binary = bundle.read('cc-connect-desktop-control.exe')
            if hashlib.sha256(binary).hexdigest() != info.get('binary_sha256'):
                raise ValueError('网关二进制校验失败，未写入本机')
            staged = target.with_suffix('.staged.exe')
            if staged.exists():
                raise ValueError('已有未完成的候选网关，未覆盖')
            staged.write_bytes(binary)
            try:
                staged.replace(target)
            except Exception:
                staged.unlink(missing_ok=True)
                raise
    return {'target': str(target), 'release_tag': tag, 'gateway_version': info['gateway_version'],
            'upstream_revision': info['upstream_revision'], 'sha256': _sha256(target)}


def main() -> int:
    parser = argparse.ArgumentParser(description='下载并校验 Codex-QQ 配套 CC Connect 网关')
    parser.add_argument('--target', type=Path,
                        default=ROOT / 'vendor/cc-connect/v1.5.0-desktop-control/bin/cc-connect-desktop-control.exe')
    args = parser.parse_args()
    print(json.dumps(fetch(target=args.target), ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
