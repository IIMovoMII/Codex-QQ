"""Selected-project QQ configuration and explicit, isolated rebinding."""
from __future__ import annotations
import copy
import json
from pathlib import Path
import re
import tomllib
import uuid
from config_transaction import ConfigTransaction, digest
from runtime_io import atomic_write_json
from setup_service import SetupService

def selected(data: Path):
    settings_path = data / 'settings.json'
    settings_bytes = settings_path.read_bytes()
    settings = json.loads(settings_bytes.decode('utf-8-sig'))
    config_path = Path(settings['config_path'])
    config_bytes = config_path.read_bytes()
    document = tomllib.loads(config_bytes.decode('utf-8-sig'))
    projects = document.get('projects', [])
    matches = [i for i, p in enumerate(projects) if p.get('name') == settings['project']]
    if len(matches) != 1:
        raise ValueError('qq_project_ambiguous')
    project_index = matches[0]
    platforms = projects[project_index].get('platforms', [])
    matches = [i for i, p in enumerate(platforms) if p.get('type') == 'qqbot']
    if len(matches) != 1:
        raise ValueError('qq_platform_ambiguous')
    platform_index = matches[0]
    options = platforms[platform_index]['options']
    return settings, document, project_index, platform_index, options, settings_bytes, config_path, config_bytes

def render_platform(raw: bytes, document, pi, qi, updates):
    expected = copy.deepcopy(document)
    options = expected['projects'][pi]['platforms'][qi]['options']
    if set(updates) - set(options):
        raise ValueError('qq_options_missing')
    options.update(updates)
    project, platform, table = -1, -1, ''
    found = set()
    result = []
    for line in raw.decode('utf-8-sig').splitlines(keepends=True):
        stripped = line.strip()
        if stripped == '[[projects]]':
            project += 1
            platform = -1
        elif stripped == '[[projects.platforms]]':
            platform += 1
        if stripped.startswith('['):
            table = stripped
        if (project, platform, table) == (pi, qi, '[projects.platforms.options]'):
            match = re.match(r'^(\s*)([\w-]+)(\s*=).*?(\r?\n)?$', line)
            if match and match[2] in updates:
                key = match[2]
                if key in found:
                    raise ValueError('qq_options_ambiguous')
                found.add(key)
                value = updates[key]
                value = str(value).lower() if isinstance(value, bool) else json.dumps(value, ensure_ascii=False)
                line = f'{match[1]}{key}{match[3]} {value}{match[4] or ""}'
        result.append(line)
    text = ''.join(result)
    if found != set(updates) or tomllib.loads(text) != expected:
        raise ValueError('qq_config_semantics_changed')
    return text.encode('utf-8')

class ConfigurationService:
    def __init__(self, root: Path, data: Path, gateway: Path):
        self.root, self.data, self.gateway = root, data, gateway
        self.pointer = data / 'state/rebinding.json'
        self.candidate = None
        self.transaction = ConfigTransaction(data)
        if self.pointer.exists():
            item = json.loads(self.pointer.read_text())
            if not re.fullmatch('[0-9a-f]{12}', item.get('id', '')):
                raise ValueError('rebind_progress_invalid')
            self.candidate = self._candidate(item['id'])

    def _candidate(self, ident):
        return SetupService(self.root, self.data / 'rebinding' / ident, self.gateway,
                            cc_data=self.data.parent / ('QQPair-' + ident))

    def begin(self, params):
        settings, doc, pi, qi, options, *_ = selected(self.data)
        if self.candidate is None:
            ident = uuid.uuid4().hex[:12]
            self.candidate = self._candidate(ident)
            atomic_write_json(self.pointer, {'id': ident, 'configHash': digest(_[-1])})
        credentials = {'appId': params.get('appId', options['app_id']),
                       'appSecret': params.get('appSecret', options['app_secret']),
                       'sandbox': params.get('sandbox', options.get('sandbox', False))}
        return self.candidate.begin(credentials)

    def complete(self, params):
        if self.candidate is None:
            raise ValueError('rebind_progress_missing')
        if params.get('confirmed') is not True:
            raise ValueError('binding_confirmation_required')
        if self.candidate.state()['phase'] != 'configured':
            self.candidate.complete(params)
        candidate = selected(self.candidate.data)
        return self.commit_options({key: candidate[4][key] for key in
                                   ('app_id', 'app_secret', 'sandbox', 'allow_from', 'group_allow_from')},
                                   session=candidate[0]['session'])

    def commit_options(self, updates, *, session=None):
        settings, doc, pi, qi, options, settings_before, path, before = selected(self.data)
        if self.pointer.exists():
            item = json.loads(self.pointer.read_text())
            if digest(before) != item['configHash']:
                raise ValueError('rebind_config_changed_preserved')
        after = render_platform(before, doc, pi, qi, updates)
        changes = [(path, before, after)]
        if session is not None:
            settings['session'] = session
            changes.append((self.data / 'settings.json', settings_before,
                            (json.dumps(settings, ensure_ascii=False, indent=2) + '\n').encode()))
        self.transaction.commit(changes)
        self.cancel()
        return {'phase': 'configured', 'step': 8, 'ready': False}

    def cancel(self):
        if self.candidate:
            self.candidate.close()
        self.candidate = None
        self.pointer.unlink(missing_ok=True)

    def close(self):
        if self.candidate:
            self.candidate.close()

    def reconcile(self):
        if self.candidate and self.candidate.state()['phase'] == 'configured':
            current, candidate = selected(self.data), selected(self.candidate.data)
            keys = ('app_id','app_secret','sandbox','allow_from','group_allow_from')
            if (all(current[4].get(k)==candidate[4].get(k) for k in keys)
                    and current[0].get('session')==candidate[0].get('session')):
                self.cancel()
                return
            # Candidate settings are published only after explicit confirmation.
            self.complete({'confirmed':True})
