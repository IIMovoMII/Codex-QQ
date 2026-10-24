"""Release CI only: reproduce the patched upstream gateway, not an install path."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
REVISION = '17c61062c2f9ce9bcdd45a2082e491f9743a2770'


def run(*args, cwd=None):
    return subprocess.run(list(args), cwd=cwd, check=True, text=True, encoding='utf-8',
                          errors='replace', stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser(description='仅供 GitHub Actions 生成配套网关')
    parser.add_argument('--version', default='1.5.0-desktop-control.5')
    args = parser.parse_args()
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        raise SystemExit('此入口只用于发布 CI。安装请运行 install.cmd，无需 Go 或源码构建。')
    go = shutil.which('go')
    if not shutil.which('git') or not go:
        raise SystemExit('发布环境缺少 Git 或 Go。')
    source = ROOT / '.build/cc-connect-release'
    vendor = ROOT / 'vendor/cc-connect/v1.5.0-desktop-control'
    target = vendor / 'bin/cc-connect-desktop-control.exe'
    staged = target.with_suffix('.staged.exe')
    if target.exists() or source.exists() or staged.exists():
        raise SystemExit('构建目标或源码目录已存在，未覆盖。请使用干净的 CI 工作目录。')
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        run('git', 'clone', '--depth', '1', '--branch', 'v1.5.0',
            'https://github.com/chenhg5/cc-connect.git', str(source))
        if run('git', 'rev-parse', 'HEAD', cwd=source) != REVISION:
            raise ValueError('上游标签内容与已核实版本不同，停止构建。')
        for name in ('metadata.patch', 'delivery.patch', 'minimal-build.patch'):
            patch = str(vendor / name)
            run('git', 'apply', '--check', patch, cwd=source)
            run('git', 'apply', patch, cwd=source)
        subprocess.run([sys.executable, '-X', 'utf8', str(ROOT / 'tools/build_cc_connect.py'),
                        '--go', go, '--source', str(source), '--output', str(staged),
                        '--version', args.version], check=True)
        staged.rename(target)
        print('发布网关已生成：' + str(target))
    except subprocess.CalledProcessError as error:
        raise SystemExit('发布构建失败。\n' + (error.stderr or '')[-2000:]) from None


if __name__ == '__main__':
    main()
