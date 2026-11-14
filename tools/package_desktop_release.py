"""Prepare release assets from the tested installer; never rebuild or publish."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = 'IIMovoMII/Codex-QQ'

def prepare(installer: Path, verifier: Path, output: Path, notes: Path):
    version = json.loads((ROOT/'desktop-app/package.json').read_text(encoding='utf-8'))['version']
    signature = installer.with_suffix(installer.suffix+'.sig').read_text(encoding='utf-8').strip()
    name = f'CodexQQ-Setup-{version}-windows-x64.exe'
    output.mkdir(parents=True,exist_ok=False)
    target=output/name
    shutil.copyfile(installer,target)
    shutil.copyfile(installer.with_suffix(installer.suffix+'.sig'),target.with_suffix('.exe.sig'))
    metadata=dict(version=version,notes=notes.read_text(encoding='utf-8'),
                  pub_date=datetime.now(timezone.utc).isoformat(),platforms={
        'windows-x86_64':dict(signature=signature,url=f'https://github.com/{REPOSITORY}/releases/download/app-v{version}/{name}')})
    channel=output/'latest.json'
    channel.write_text(json.dumps(metadata,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    result=subprocess.run([str(verifier.resolve()),'--verify-update',str(target.resolve()),str(channel.resolve())],timeout=30)
    if result.returncode: raise ValueError('official_signature_verification_failed')
    for source in ('LICENSE','THIRD_PARTY_NOTICES.md'):
        shutil.copyfile(ROOT/source,output/source)
    for source in ('DEPENDENCIES.json','THIRD_PARTY_LICENSES.txt'):
        shutil.copyfile(ROOT/'desktop-app/src-tauri/resources/licenses'/source,output/source)
    shutil.copyfile(notes,output/'RELEASE_NOTES.md')
    hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest()for p in sorted(output.iterdir())if p.is_file()}
    (output/'SHA256SUMS.txt').write_text(''.join(f'{digest}  {name}\n'for name,digest in hashes.items()),encoding='utf-8')
    print(json.dumps(dict(version=version,installer=target.name,sha256=hashes[target.name]),ensure_ascii=False))

if __name__=='__main__':
    parser=argparse.ArgumentParser(description='从实测同一资产准备 GitHub 发布材料，不重新构建。')
    parser.add_argument('--installer',type=Path,required=True)
    parser.add_argument('--verifier',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--notes',type=Path,required=True)
    args=parser.parse_args()
    prepare(args.installer,args.verifier,args.output,args.notes)
