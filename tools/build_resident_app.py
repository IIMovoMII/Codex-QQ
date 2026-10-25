"""Build the tiny local WinExe with the existing Windows/.NET compiler."""
from __future__ import annotations

import json
import argparse
import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src/codex_desktop_weixin'))
from runtime_io import atomic_write_json


def main() -> int:
    parser = argparse.ArgumentParser(description='生成小型启动器；--output-dir 用于隔离验证')
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'bin')
    args = parser.parse_args()
    if os.name != 'nt':
        raise SystemExit('该本机构建入口仅支持 Windows。')
    compiler = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    pythonw = Path(sys.executable).with_name('pythonw.exe')
    if not compiler.is_file() or not pythonw.is_file():
        raise SystemExit('缺少现有 .NET 编译器或 Python 窗口运行时；未安装其他依赖。')
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    target = output / 'CodexQQ助手.exe'
    source = ROOT / 'tools/ResidentLauncher.cs'
    fd, name = tempfile.mkstemp(prefix='.launcher-', suffix='.exe', dir=output)
    os.close(fd)
    candidate = Path(name)
    try:
        subprocess.run([str(compiler), '/nologo', '/utf8output', '/target:winexe', '/platform:anycpu',
                        '/reference:System.Web.Extensions.dll', '/reference:Microsoft.CSharp.dll',
                        '/out:' + str(candidate), str(source)], check=True)
        os.replace(candidate, target)
    finally:
        candidate.unlink(missing_ok=True)
    # Generated build metadata, not a second source checkout or credentials.
    atomic_write_json(output / 'app-runtime.json', {'version': 1, 'pythonw': str(pythonw),
        'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
        'launcher_sha256': hashlib.sha256(target.read_bytes()).hexdigest()})
    print(json.dumps({'executable': str(target), 'launcher_bytes': target.stat().st_size,
                      'self_contained': False, 'uses_existing_python': True}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as error:
        print(f'启动器编译失败（编译器退出码 {error.returncode}），详细错误见上方。', file=sys.stderr)
        raise SystemExit(error.returncode) from None
