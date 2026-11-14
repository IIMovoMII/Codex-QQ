"""Prepare verified, redistributable Windows runtime resources for Tauri."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile
import urllib.request
import zipfile

from fetch_gateway import BINARY_SHA256, ensure_gateway

ROOT = Path(__file__).resolve().parents[1]
RESOURCES = ROOT / "desktop-app/src-tauri/resources"
PYTHON_VERSION = "3.13.13"
PYTHON_EMBED_SHA256 = "8766a8775746235e23cf5aee5027ab1060bb981d93110577adcf3508aa0cbd55"
MAX_PYTHON_ARCHIVE_BYTES = 20 * 1024 * 1024

def source_fingerprint() -> str:
    paths = [ROOT/p for p in ('LICENSE','THIRD_PARTY_NOTICES.md','desktop-app/package.json',
             'desktop-app/package-lock.json','desktop-app/src-tauri/Cargo.toml','desktop-app/src-tauri/Cargo.lock',
             'desktop-app/src-tauri/build.rs','desktop-app/src-tauri/app.manifest','desktop-app/src-tauri/tauri.conf.json')]
    for directory in ('src/codex_desktop_weixin','desktop-app/src','desktop-app/src-tauri/src',
                      'desktop-app/src-tauri/installer','desktop-app/licenses'):
        paths.extend(p for p in (ROOT/directory).rglob('*') if p.is_file() and '__pycache__' not in p.parts)
    paths.extend(ROOT/'tools'/name for name in ('configure.py','pairing_agent.py','setup_process.py',
                 'setup_diagnostics.py','prepare_desktop_package.py','prepare_desktop_licenses.py'))
    lines=''.join(_hash_file(p)+'  '+p.relative_to(ROOT).as_posix()+'\n' for p in sorted(set(paths)))
    return hashlib.sha256(lines.encode('utf-8')).hexdigest()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _download_python(target: Path) -> None:
    url = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip"
    request = urllib.request.Request(url, headers={"User-Agent": "Codex-QQ-desktop-builder/1"})
    total = 0
    with urllib.request.urlopen(request, timeout=60) as response, target.open("xb") as output:
        while True:
            block = response.read(1024 * 1024)
            if not block:
                break
            total += len(block)
            if total > MAX_PYTHON_ARCHIVE_BYTES:
                raise ValueError("Python runtime archive exceeded its size limit")
            output.write(block)
    if _hash_file(target) != PYTHON_EMBED_SHA256:
        raise ValueError("Python runtime SHA-256 did not match the pinned release artifact")


def _prepare_python() -> Path:
    target = RESOURCES / "python"
    manifest = target / "python-runtime.json"
    if target.exists():
        try:
            record = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise FileExistsError("Unknown Python resource directory exists; preserved without overwrite") from None
        if (record.get("version") != PYTHON_VERSION or record.get("sha256") != PYTHON_EMBED_SHA256
                or not (target / "python.exe").is_file()):
            raise FileExistsError("Python resource directory does not match the pinned release; preserved")
        if record.get("files"):
            actual = {p.relative_to(target).as_posix(): _hash_file(p) for p in target.rglob('*')
                      if p.is_file() and p != manifest}
            if actual != record["files"]:
                raise ValueError("Python runtime contents changed; refused to package")
            return target
        # Upgrade an earlier builder-owned cache by comparing every file with
        # the pinned archive below. Never bless the existing bytes as a baseline.

    RESOURCES.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="python-runtime-", dir=RESOURCES) as temp_name:
        temporary = Path(temp_name)
        archive = temporary / "python-embed.zip"
        extracted = temporary / "python"
        extracted.mkdir()
        _download_python(archive)
        with zipfile.ZipFile(archive) as bundle:
            members = bundle.infolist()
            for member in members:
                path = PurePosixPath(member.filename)
                if path.is_absolute() or ".." in path.parts or "\\" in member.filename:
                    raise ValueError("Python runtime archive contains an unsafe path")
            bundle.extractall(extracted)
        pth = extracted / "python313._pth"
        if not pth.is_file() or not (extracted / "python.exe").is_file():
            raise ValueError("Python embeddable package is missing its runtime entry files")
        entries = [line.strip() for line in pth.read_text(encoding="utf-8").splitlines() if line.strip()]
        if "../backend" not in entries:
            entries.append("../backend")
        pth.write_text("\n".join(entries) + "\n", encoding="utf-8")
        files = {p.relative_to(extracted).as_posix(): _hash_file(p) for p in extracted.rglob('*') if p.is_file()}
        (extracted / "python-runtime.json").write_text(
            json.dumps({"version": PYTHON_VERSION, "sha256": PYTHON_EMBED_SHA256, "files": files}, indent=2) + "\n",
            encoding="utf-8",
        )
        if target.exists():
            actual = {p.relative_to(target).as_posix(): _hash_file(p) for p in target.rglob('*')
                      if p.is_file() and p != manifest}
            if actual != files:
                raise ValueError("Earlier Python cache differs from the pinned runtime; preserved for inspection")
            shutil.copyfile(extracted / 'python-runtime.json', manifest)
        else:
            extracted.rename(target)
    return target


def main() -> int:
    python = _prepare_python()
    gateway = RESOURCES / "gateway.exe"
    gateway_result = ensure_gateway(gateway)
    if _hash_file(gateway) != BINARY_SHA256:
        raise ValueError("Verified gateway file did not match the pinned build")
    backend = RESOURCES / 'backend/codex_desktop_weixin'
    backend.mkdir(parents=True, exist_ok=True)
    source = ROOT / 'src/codex_desktop_weixin'
    expected = {p.name for p in source.glob('*.py')}
    for path in backend.iterdir():
        if path.is_file() and path.suffix == '.py' and path.name not in expected:
            path.unlink()  # Only this builder's exact, generated source directory.
        elif path.is_dir() or path.suffix != '.py':
            raise ValueError('Unexpected file in generated backend resources')
    for path in source.glob('*.py'):
        shutil.copyfile(path, backend / path.name)
    tools = RESOURCES / 'tools'
    tools.mkdir(exist_ok=True)
    for name in ('configure.py', 'pairing_agent.py', 'setup_process.py', 'setup_diagnostics.py'):
        shutil.copyfile(ROOT / 'tools' / name, tools / name)
    package = json.loads((ROOT / 'desktop-app/package.json').read_text(encoding='utf-8'))
    from prepare_desktop_licenses import prepare
    prepare(RESOURCES / 'licenses')
    files = {p.relative_to(RESOURCES).as_posix(): _hash_file(p) for p in RESOURCES.rglob('*')
             if p.is_file() and p.name != 'build-manifest.json'}
    if any('__pycache__' in name or name.endswith(('.pyc', '.log', '.jsonl')) for name in files):
        raise ValueError('Generated runtime contains forbidden cache or local data')
    (RESOURCES / 'build-manifest.json').write_text(json.dumps({
        'version': package['version'], 'sourceSha256': source_fingerprint(), 'python': PYTHON_VERSION,
        'gateway': gateway_result['release_tag'], 'files': files,
    }, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({
        "python_version": PYTHON_VERSION,
        "python_executable": str(python / "python.exe"),
        "gateway_release": gateway_result["release_tag"],
        "gateway_sha256": gateway_result["sha256"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
