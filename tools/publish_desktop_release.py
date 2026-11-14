"""Publish exact accepted assets and verify the public round trip."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

REPOSITORY='IIMovoMII/Codex-QQ'

def run(*args, **kwargs):
    return subprocess.run(list(args),check=True,**kwargs)

def publish(assets: Path,evidence: Path,verifier: Path):
    metadata=json.loads((assets/'latest.json').read_text(encoding='utf-8'))
    version=metadata['version'];tag='app-v'+version
    report=json.loads(evidence.read_text(encoding='utf-8'))
    installer=assets/f'CodexQQ-Setup-{version}-windows-x64.exe'
    digest=hashlib.sha256(installer.read_bytes()).hexdigest()
    if (report.get('version')!=version or report.get('sha256')!=digest or
        any(report.get('gates',{}).get(g)!='passed'for g in ('G1','G2','G3','G4','G5','G6'))):
        raise ValueError('mandatory_acceptance_not_complete')
    sums={line.split('  ',1)[1]:line.split('  ',1)[0]for line in (assets/'SHA256SUMS.txt').read_text().splitlines()}
    for name,expected in sums.items():
        file=assets/name
        if file.parent!=assets or hashlib.sha256(file.read_bytes()).hexdigest()!=expected:
            raise ValueError('release_asset_changed')
    run(str(verifier.resolve()),'--verify-update',str(installer.resolve()),str((assets/'latest.json').resolve()),timeout=30)
    if run('git','status','--porcelain',capture_output=True).stdout:
        raise ValueError('release_source_not_committed')
    revision=run('git','rev-parse','HEAD',capture_output=True,text=True).stdout.strip()
    if report.get('commit')!=revision:raise ValueError('acceptance_commit_mismatch')
    found=subprocess.run(['gh','release','view',tag,'--repo',REPOSITORY],capture_output=True)
    if found.returncode==0:raise ValueError('release_already_exists_preserved')
    existing=run('git','ls-remote','--tags','origin','refs/tags/'+tag,capture_output=True).stdout
    if existing:raise ValueError('tag_already_exists_preserved')
    run('git','push','origin','HEAD:main')
    run('gh','release','create',tag,*[str(p.resolve())for p in sorted(assets.iterdir())if p.is_file()],
        '--repo',REPOSITORY,'--target',revision,'--title',f'Codex QQ 助手 {version}',
        '--notes-file',str((assets/'RELEASE_NOTES.md').resolve()),'--latest')
    with tempfile.TemporaryDirectory(prefix='codex-qq-release-check-')as temporary:
        run('gh','release','download',tag,'--repo',REPOSITORY,'--dir',temporary)
        for name,expected in sums.items():
            if hashlib.sha256((Path(temporary)/name).read_bytes()).hexdigest()!=expected:
                raise ValueError('public_asset_hash_mismatch')
        run(str(verifier.resolve()),'--verify-update',str(Path(temporary)/installer.name),
            str(Path(temporary)/'latest.json'),timeout=30)
    print(f'https://github.com/{REPOSITORY}/releases/tag/{tag}')

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--assets',type=Path,required=True)
    parser.add_argument('--evidence',type=Path,required=True)
    parser.add_argument('--verifier',type=Path,required=True)
    args=parser.parse_args()
    publish(args.assets.resolve(),args.evidence.resolve(),args.verifier)
