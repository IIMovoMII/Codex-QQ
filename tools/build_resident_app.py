"""Build the tiny local WinExe with the existing Windows/.NET compiler."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    if os.name != 'nt':
        raise SystemExit('该本机构建入口仅支持 Windows。')
    compiler = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    pythonw = Path(sys.executable).with_name('pythonw.exe')
    if not compiler.is_file() or not pythonw.is_file():
        raise SystemExit('缺少现有 .NET 编译器或 Python 窗口运行时；未安装其他依赖。')
    output = ROOT / 'bin'
    output.mkdir(exist_ok=True)
    target = output / 'CodexQQ助手.exe'
    subprocess.run([str(compiler), '/nologo', '/target:winexe', '/platform:anycpu',
                    '/reference:System.Web.Extensions.dll', '/reference:Microsoft.CSharp.dll', '/out:' + str(target),
                    str(ROOT / 'tools/ResidentLauncher.cs')], check=True)
    # Generated build metadata, not a second source checkout or credentials.
    (output / 'app-runtime.json').write_text(json.dumps({'version': 1, 'pythonw': str(pythonw)},
                                                      ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'executable': str(target), 'launcher_bytes': target.stat().st_size,
                      'self_contained': False, 'uses_existing_python': True}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
