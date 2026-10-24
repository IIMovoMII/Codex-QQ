"""Download/reuse the verified release; end users never need a Go toolchain."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from fetch_gateway import ensure_gateway

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description='下载或复用已校验的 QQ 网关，不覆盖其他版本、不启动服务')
    parser.parse_args()
    target = ROOT / 'vendor/cc-connect/v1.5.0-desktop-control/bin/cc-connect-desktop-control.exe'
    print(json.dumps(ensure_gateway(target), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
