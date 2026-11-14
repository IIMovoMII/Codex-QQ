"""Fail closed when any configuration, delivery or Desktop task is unresolved."""
from pathlib import Path
import time

def can_update(data: Path, codex_home: Path, journal) -> dict:
    if journal.has_unresolved():
        return {'allowed': False, 'reason': 'operation_pending'}
    if any((data/p).exists() for p in ('setup-pending.json','state/rebinding.json',
                                     'state/config-change.json','state/install-takeover.json')):
        return {'allowed': False, 'reason': 'configuration_pending'}
    if any((data/'outbox/pending').glob('*.json')):
        return {'allowed': False, 'reason': 'delivery_pending'}
    from process_watch import get_codex_desktop_process_status
    if not get_codex_desktop_process_status().desktop_alive:
        return {'allowed': True, 'reason': 'desktop_closed'}
    from control_catalog import LocalCatalog
    from control_backend import NativeBackend
    from desktop_control import WindowsDesktopPipe
    try:
        catalog = LocalCatalog(codex_home)
        rows = catalog._query("SELECT id FROM threads WHERE archived=0 AND agent_role IS NULL ORDER BY updated_at_ms DESC LIMIT 201")
        if len(rows) > 200:
            return {'allowed': False, 'reason': 'task_state_unknown'}
        backend = NativeBackend(pipe_factory=lambda: WindowsDesktopPipe(timeout_seconds=2))
        connection, _ = backend.probe()
        deadline = time.monotonic()+12
        for row in rows:
            if time.monotonic()>deadline:
                return {'allowed': False, 'reason': 'task_state_unknown'}
            state = backend.snapshot(row['id'], connection)
            # Only a positively idle/paused task permits an install. Unknown
            # ownership, queued work and unavailable conversations all wait.
            if (backend._message_route(state) != 'started' or state.get('async_questions')
                    or state.get('async_questions_unverified') or not state.get('requests_known')):
                return {'allowed': False, 'reason': 'task_active_or_unknown'}
            if catalog.saved_queue(row['id']):
                return {'allowed': False, 'reason': 'task_queued'}
    except (OSError, ValueError, RuntimeError, KeyError):
        return {'allowed': False, 'reason': 'task_state_unknown'}
    return {'allowed': True, 'reason': 'idle'}
