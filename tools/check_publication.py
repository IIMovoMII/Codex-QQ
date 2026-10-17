"""Basic tracked-file/privacy and local-link checks; not a security audit."""
from __future__ import annotations

from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = {'PROJECT_STATUS.md', 'QQ_CONTROL_IMPLEMENTATION_PLAN.md', 'QQ_CONTROL_MANUAL_TESTS.md',
           'NATIVE_INTERFACES.md', 'auth.json', 'settings.json', 'config.toml', 'app-runtime.json'}


def check() -> list[str]:
    result = subprocess.run(['git', 'ls-files', '-z'], cwd=ROOT, capture_output=True, check=True)
    names = result.stdout.decode('utf-8').split('\0')
    errors = []
    total = 0
    for name in filter(None, names):
        path = ROOT / name
        if (path.name in PRIVATE or path.suffix in ('.exe', '.dll', '.sqlite', '.db', '.jsonl', '.log')
                or any(x in ('.build', 'bin', 'logs', 'outbox', 'spool', '__pycache__') for x in Path(name).parts)):
            errors.append(name + ': private/runtime file is tracked')
            continue
        if path.is_symlink():
            errors.append(name + ': unexpected symlink')
            continue
        blob = path.read_bytes()
        total += len(blob)
        if len(blob) > 2_000_000:
            errors.append(name + ': unexpectedly large source file')
        try:
            text = blob.decode('utf-8-sig')
        except UnicodeError:
            errors.append(name + ': non-UTF-8 public file')
            continue
        if re.search(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----', text):
            errors.append(name + ': possible private key')
        if re.search(r'\b(?:ghp_|github_pat_|sk-proj-)[A-Za-z0-9_]{20,}', text):
            errors.append(name + ': possible credential')
        if re.search(r'(?m)^\s*app_secret\s*=\s*[\"\x27][A-Za-z0-9_-]{12,}[\"\x27]', text):
            errors.append(name + ': possible bot credential')
        # Alice/Bob are explicit synthetic fixtures; user home directories are
        # otherwise not suitable for a portable public project.
        for user in re.findall(r'(?i)\b[A-Z]:[/\\]+Users[/\\]+([^/\\\s\"\x27<>]+)', text):
            if user.lower() not in ('alice', 'bob', 'test', 'example', 'public'):
                errors.append(name + ': personal absolute user path')
        if path.suffix.lower() == '.md':
            for target in re.findall(r'\]\(([^)]+)\)', text):
                target = target.strip('<>').split('#', 1)[0]
                if not target or '://' in target or target.startswith('mailto:'):
                    continue
                linked = (path.parent / unquote(target)).resolve()
                if not linked.is_relative_to(ROOT) or not linked.exists():
                    errors.append(name + ': broken/nonportable local link: ' + target)
                elif linked.is_file() and linked.relative_to(ROOT).as_posix() not in names:
                    errors.append(name + ': link points to an unpublished file: ' + target)
    print(f'Checked {len(list(filter(None, names)))} public files, {total:,} bytes.')
    return errors


if __name__ == '__main__':
    found = check()
    for item in found:
        print(item)
    sys.exit(1 if found else 0)
