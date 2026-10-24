"""Obtain the pinned, patched gateway without replacing a running executable."""
from __future__ import annotations

import argparse
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
    parser = argparse.ArgumentParser(description='获取 QQ 网关，不覆盖已有文件、不启动服务')
    parser.add_argument('--go', default=shutil.which('go'))
    parser.add_argument('--source-build', action='store_true',
                        help='开发者模式：从固定 CC Connect 源码应用补丁并本机构建')
    parser.add_argument('--version', default='1.5.0-desktop-control.5')
    args = parser.parse_args()
    source = ROOT / '.build/cc-connect-release'
    vendor = ROOT / 'vendor/cc-connect/v1.5.0-desktop-control'
    target = vendor / 'bin/cc-connect-desktop-control.exe'
    staged = target.with_suffix('.staged.exe')
    if not args.source_build:
        # Ordinary installation uses the CI-built, checksum-verified release.
        # Go is deliberately an explicit developer fallback, not a hidden
        # prerequisite for users who only want to run the application.
        subprocess.run([sys.executable, '-X', 'utf8', str(ROOT / 'tools/fetch_gateway.py'),
                        '--target', str(target)], check=True)
        return
    if not shutil.which('git') or not args.go:
        raise SystemExit('源码构建需要 Git 和 Go 1.25 或更新版本；普通安装无需 Go。')
    if target.exists() or source.exists() or staged.exists():
        raise SystemExit('构建目标或源码目录已存在，未覆盖。更新/重新构建见 docs/development.md。')
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
                        '--go', args.go, '--source', str(source), '--output', str(staged),
                        '--version', args.version], check=True)
        # Windows rename refuses to overwrite an existing target.
        staged.rename(target)
        print('网关已生成：' + str(target))
        print('未启动服务；请继续完成自己的机器人配置。')
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or '')[-2000:]
        raise SystemExit('构建失败，原运行版本未改动。\n' + detail) from None


if __name__ == '__main__':
    main()
