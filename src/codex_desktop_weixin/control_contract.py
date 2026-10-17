"""按实际连接的 Desktop 检查接口能力，不绑定安装版本或程序哈希。

IPC 尚无公开能力查询，因此按需读取同一进程安装包里的协议声明。
只比较本次操作使用的接口版本；工具服务另用实时目录和参数声明。
检查结果按文件身份缓存，不增加常驻进程、历史扫描或定时探测。
"""
from __future__ import annotations

import json
import re
import struct
from functools import lru_cache
from pathlib import Path

from desktop_control import DesktopControlError

BASE_METHODS = {
    'thread-owner-discovery': 1,
    'thread-stream-following-changed': 1,
    'thread-stream-state-changed': 11,
    'thread-follower-read-model-settings': 1,
}
MAX_HEADER = 20_000_000
MAX_MODULE = 16 * 1024 * 1024
MAX_INSPECTED = 32 * 1024 * 1024
TABLE_START = re.compile(r'\{\s*"(?:thread-[a-z0-9-]+|ipc-connection-reset)"\s*:\s*\d+')


def _bundle_path(process_image: str) -> Path:
    try:
        image = Path(process_image).resolve(strict=True)
        if image.name.lower() not in ('chatgpt.exe', 'codex.exe') or not image.is_file():
            raise ValueError('unexpected-desktop-image')
        # Resolve from the ACTUAL pipe owner, never a saved installation path.
        bundle = (image.parent / 'resources' / 'app.asar').resolve(strict=True)
        if not bundle.is_relative_to(image.parent) or not bundle.is_file():
            raise ValueError('unexpected-desktop-bundle')
        return bundle
    except (OSError, ValueError):
        raise DesktopControlError('desktop-capability-source-unavailable') from None


def _index(stream, size: int) -> tuple[dict, int]:
    header = stream.read(16)
    if len(header) != 16:
        raise DesktopControlError('unsupported-desktop-bundle')
    magic, header_size, _, json_size = struct.unpack('<IIII', header)
    if magic != 4 or not 0 < json_size < header_size < MAX_HEADER or 8 + header_size > size:
        raise DesktopControlError('unsupported-desktop-bundle')
    index = json.loads(stream.read(json_size))
    if not isinstance(index, dict):
        raise DesktopControlError('unsupported-desktop-bundle')
    return index, 8 + header_size


@lru_cache(maxsize=2)
def _capabilities(path: str, size: int, modified: int, changed: int) -> dict[str, int]:
    del modified, changed  # File identity forms the cache key, not an allowlist.
    try:
        with Path(path).open('rb') as stream:
            index, data_start = _index(stream, size)
            files = index['files']['.vite']['files']['build']['files']
            if not isinstance(files, dict) or len(files) > 1024:
                raise ValueError('unsupported-module-catalog')
            # No hashed filename is pinned. Prefer the shared protocol module;
            # a changed packaging layout can use another build module within
            # the same bounded read budget. Never execute bundled JavaScript.
            names = sorted((n for n in files if n.endswith(('.js', '.mjs'))),
                           key=lambda n: (not n.startswith('src'), n))
            inspected = 0
            for name in names:
                node = files[name]
                if not isinstance(node, dict) or node.get('unpacked'):
                    continue
                length = node.get('size')
                if not isinstance(length, int) or isinstance(length, bool) or not 0 < length <= MAX_MODULE:
                    continue
                offset = int(node['offset'])
                if offset < 0 or data_start + offset + length > size:
                    raise ValueError('truncated-module')
                inspected += length
                if inspected > MAX_INSPECTED:
                    break
                stream.seek(data_start + offset)
                raw = stream.read(length)
                if len(raw) != length:
                    raise ValueError('truncated-module')
                content = raw.decode('utf-8', errors='strict')
                tables = []
                for match in TABLE_START.finditer(content):
                    value, _ = json.JSONDecoder().raw_decode(content[match.start():match.start() + 16 * 1024])
                    if (not isinstance(value, dict) or 'thread-owner-discovery' not in value
                            or 'thread-stream-state-changed' not in value):
                        continue
                    if (len(value) > 256 or any(not isinstance(k, str) or not isinstance(v, int)
                            or isinstance(v, bool) or not 0 <= v <= 1000 for k, v in value.items())):
                        raise ValueError('invalid-protocol-table')
                    tables.append(value)
                if tables:
                    if any(t != tables[0] for t in tables[1:]):
                        raise ValueError('ambiguous-protocol-table')
                    return tables[0]
    except (OSError, ValueError, KeyError, TypeError, struct.error):
        pass
    raise DesktopControlError('desktop-capability-source-unavailable')


def capabilities(process_image: str) -> dict[str, int]:
    bundle = _bundle_path(process_image)
    stat = bundle.stat()
    return dict(_capabilities(str(bundle), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))


def check_method_contract(process_image: str, method: str, version: int) -> None:
    """A changed unrelated method must not disable this operation."""
    if capabilities(process_image).get(method) != version:
        raise DesktopControlError('desktop-method-contract-incompatible:' + method)


def check_contract(process_image: str) -> None:
    actual = capabilities(process_image)
    for method, version in BASE_METHODS.items():
        if actual.get(method) != version:
            raise DesktopControlError('desktop-method-contract-incompatible:' + method)


def check_app_tools_contract(process_image: str) -> None:
    # The actual server identity is matched separately before tools/list/call.
    # Native tool compatibility comes from its live inputSchema, not IPC hashes.
    _bundle_path(process_image)


def check_steer_contract(process_image: str) -> None:
    check_contract(process_image)
    check_method_contract(process_image, 'thread-follower-steer-turn', 1)


def check_queue_contract(process_image: str) -> None:
    # Queue observation only consumes the base snapshot. Each mutation checks
    # its own method immediately before dispatch (start vs single-item removal).
    check_contract(process_image)
