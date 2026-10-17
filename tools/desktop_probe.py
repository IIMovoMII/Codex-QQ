"""Read-only excerpts from the installed Desktop bundle, without extraction."""

from __future__ import annotations

import argparse
import fnmatch
import json
import struct
from pathlib import Path


class AsarReader:
    def __init__(self, path: Path) -> None:
        self.path = path
        with path.open("rb") as stream:
            size, header_size, payload_size, json_size = struct.unpack("<IIII", stream.read(16))
            if size != 4 or not 0 < json_size < header_size < 20_000_000:
                raise ValueError("Unsupported ASAR header")
            self.index = json.loads(stream.read(json_size))
        self.data_offset = 8 + header_size

    def entries(self, node: dict | None = None, prefix: str = ""):
        for name, item in (node or self.index).get("files", {}).items():
            full = prefix + name
            if "files" in item:
                yield from self.entries(item, full + "/")
            else:
                yield full, item

    def read(self, item: dict) -> str:
        if item.get("unpacked"):
            raise ValueError("Unpacked entry")
        with self.path.open("rb") as stream:
            stream.seek(self.data_offset + int(item["offset"]))
            return stream.read(item["size"]).decode("utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("asar", type=Path)
    parser.add_argument("--file", default="*")
    parser.add_argument("--find", action="append", default=[])
    parser.add_argument("--radius", type=int, default=450)
    parser.add_argument("--max-matches", type=int, default=5)
    args = parser.parse_args()
    bundle = AsarReader(args.asar)
    for name, item in bundle.entries():
        if not fnmatch.fnmatch(name, args.file):
            continue
        if not args.find:
            print(json.dumps({"file": name, "bytes": item.get("size")}, ensure_ascii=False))
            continue
        if item.get("unpacked") or item.get("size", 0) > 40_000_000:
            continue
        source = bundle.read(item)
        for needle in args.find:
            cursor = 0
            for _ in range(args.max_matches):
                at = source.find(needle, cursor)
                if at < 0:
                    break
                print(json.dumps({"file": name, "needle": needle, "offset": at,
                                  "excerpt": source[max(0, at-args.radius):at+len(needle)+args.radius]},
                                 ensure_ascii=False))
                cursor = at + len(needle)


if __name__ == "__main__":
    main()
