"""Reproducible ACP/QQ-only build. Never installs or stops a live service."""
from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
from pathlib import Path

REQUIRED = {'no_acp', 'no_qqbot'}
MODULE = 'github.com/chenhg5/cc-connect/'


def build_tags(source: Path) -> list[str]:
    found = set()
    for path in (source / 'cmd' / 'cc-connect').glob('plugin_*.go'):
        match = re.match(r'//go:build !(no_[a-z0-9_]+)', path.read_text(encoding='utf-8'))
        if not match:
            raise ValueError('未知的插件编译标记：' + path.name)
        found.add(match.group(1))
    if not REQUIRED <= found:
        raise ValueError('源码缺少必需的 ACP 或 QQ 插件，拒绝编译')
    return sorted(found - REQUIRED)


def verify_dependencies(output: str) -> None:
    plugins = {x for x in output.splitlines() if x.startswith(MODULE + 'agent/') or x.startswith(MODULE + 'platform/')}
    if plugins != {MODULE + 'agent/acp', MODULE + 'platform/qqbot'}:
        raise ValueError('实际编译的代理/平台不是仅 ACP 和 QQ，拒绝发布：' + ', '.join(sorted(plugins)))


def verify_binary_metadata(output: str) -> None:
    match = re.search(r'-tags=(\S+)', output)
    if not match:
        raise ValueError('二进制缺少可核对的编译标记')
    tags = set(match.group(1).split(','))
    if tags & REQUIRED or 'no_codex' not in tags:
        raise ValueError('错误编译：ACP/QQ 被排除或另一个 Codex Agent 被包含')


def run(go: str, args: list[str], source: Path | None = None) -> str:
    result = subprocess.run([go, *args], cwd=source, capture_output=True, encoding='utf-8',
                            errors='strict', timeout=240, check=True)
    return result.stdout


def main() -> None:
    parser = argparse.ArgumentParser(description='仅构建，不安装，不发送 QQ 消息')
    parser.add_argument('--go', required=True)
    parser.add_argument('--check-binary', type=Path)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--version', default='1.5.0-desktop-control.4')
    args = parser.parse_args()
    if args.check_binary:
        verify_binary_metadata(run(args.go, ['version', '-m', str(args.check_binary)]))
        print('ACP/QQ 编译标记检查通过；这不等于服务启动或手机实收验收。')
        return
    if args.source is None or args.output is None:
        parser.error('构建需要 --source 和 --output')
    target = args.output.resolve()
    if not target.name.endswith('.staged.exe') or target.exists():
        parser.error('只允许写入尚不存在的 *.staged.exe，不覆盖运行版本')
    tags = build_tags(args.source)
    joined = ','.join(tags)
    verify_dependencies(run(args.go, ['list', '-deps', '-tags', joined, './cmd/cc-connect'], args.source))
    target.parent.mkdir(parents=True, exist_ok=True)
    run(args.go, ['build', '-trimpath', '-tags', joined, '-ldflags', f'-s -w -X main.version={args.version}',
                  '-o', str(target), './cmd/cc-connect'], args.source)
    verify_binary_metadata(run(args.go, ['version', '-m', str(target)]))
    print(run(str(target), ['--version']).strip())
    print('SHA-256：' + hashlib.sha256(target.read_bytes()).hexdigest())
    print('候选文件已生成，未替换运行程序。还须通过包测试、就绪检查及人工验收。')


if __name__ == '__main__':
    main()
