"""Collect exact locked dependency notices, failing closed on missing licenses."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / 'desktop-app'

def license_files(root):
    return sorted(p for p in root.iterdir() if p.is_file() and
                  p.name.upper().startswith(('LICENSE', 'LICENCE', 'COPYING', 'NOTICE', 'UNLICENSE')))

def prepare(output: Path):
    records = []
    blocks = ['Third-party notices for Codex QQ. Unmodified MPL sources are available at the exact crate URLs below.\n']
    upstream = APP / 'licenses'
    for source in json.loads((upstream/'sources.json').read_text(encoding='utf-8')):
        path = ROOT / source['file']
        if hashlib.sha256(path.read_bytes()).hexdigest() != source['sha256']:
            raise ValueError('upstream_license_hash_changed')
    metadata = subprocess.run(['cargo','metadata','--locked','--format-version','1','--filter-platform',
                               'x86_64-pc-windows-gnu'], cwd=APP/'src-tauri', capture_output=True, check=True)
    for package in sorted(json.loads(metadata.stdout)['packages'], key=lambda p:(p['name'],p['version'])):
        if not package['source']:
            continue
        root = Path(package['manifest_path']).parent
        paths = license_files(root)
        if package.get('license_file'):
            paths.append(root/package['license_file'])
        name = package['name']
        if not paths:
            vcs = json.loads((root/'.cargo_vcs_info.json').read_text())['git']['sha1']
            if name == 'alloc-stdlib': folder='dropbox-rust-alloc-no-stdlib-'
            elif name == 'tauri-plugin': folder='tauri-apps-tauri-'
            elif name.startswith('webview2-com'): folder='wravery-webview2-rs-'
            elif 'MPL-2.0' in package.get('license',''): folder=None
            else: raise ValueError('missing_crate_license:'+name)
            paths = [upstream/'MPL-2.0.txt'] if folder is None else license_files(upstream/'upstream'/(folder+vcs[:12]))
        if not paths: raise ValueError('missing_crate_license:'+name)
        record = dict(kind='Rust',name=name,version=package['version'],license=package.get('license'),
                      repository=package.get('repository'),
                      source=f"https://crates.io/api/v1/crates/{name}/{package['version']}/download")
        records.append(record)
        blocks.append('\n'+json.dumps(record,ensure_ascii=False)+'\n')
        for path in dict.fromkeys(paths):
            blocks.append(path.name+'\n'+path.read_text(encoding='utf-8-sig')+'\n')
    lock=json.loads((APP/'package-lock.json').read_text(encoding='utf-8'))
    for name,value in sorted(lock['packages'].items()):
        if not name or value.get('dev'): continue
        root=APP/name
        paths=license_files(root)
        if not paths and name in ('node_modules/is-reference', 'node_modules/locate-character'):
            # These exact upstream packages publish the MIT declaration in README.
            readme=root/'README.md'
            if '## License\n\nMIT' in readme.read_text(encoding='utf-8'):
                paths=[readme,root/'package.json']
        if not paths: raise ValueError('missing_npm_license:'+name)
        record=dict(kind='npm',name=name.removeprefix('node_modules/'),version=value['version'],
                    license=value.get('license'),source=value.get('resolved'),integrity=value.get('integrity'))
        records.append(record)
        blocks.append('\n'+json.dumps(record,ensure_ascii=False)+'\n')
        for path in paths: blocks.append(path.name+'\n'+path.read_text(encoding='utf-8-sig')+'\n')
    for path in (upstream/'MPL-2.0.txt',upstream/'WebView2-SDK-1.0.3800.47.txt',upstream/'Go-1.25.14-LICENSE.txt'):
        blocks.append('\n'+path.name+'\n'+path.read_text(encoding='utf-8-sig'))
    for record in json.loads((upstream/'gateway-dependencies.json').read_text(encoding='utf-8')):
        path=ROOT/record['file']
        if hashlib.sha256(path.read_bytes()).hexdigest()!=record['sha256']:
            raise ValueError('gateway_license_hash_changed')
        records.append({'kind':'Go',**record})
        blocks.append('\n'+path.read_text(encoding='utf-8-sig'))
    output.mkdir(parents=True,exist_ok=True)
    (output/'DEPENDENCIES.json').write_text(json.dumps(records,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (output/'THIRD_PARTY_LICENSES.txt').write_text(''.join(blocks),encoding='utf-8')
    print(f'Collected notices for {len(records)} locked dependencies.')

if __name__=='__main__':
    prepare(APP/'src-tauri/resources/licenses')
