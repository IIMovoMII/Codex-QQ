"""Stateful desktop adapter over the same write-ahead setup used by install.cmd."""
from __future__ import annotations
import json
import os
from pathlib import Path
import sys
import shutil
import tempfile
import re
from runtime_io import acquire_file_lock, atomic_write_json

class SetupService:
    def __init__(self, root: Path, data: Path, gateway: Path, *, cc_data: Path | None = None):
        self.root, self.data, self.gateway = root, data, gateway
        self.cc_data = cc_data
        self.child = None
        self.log_offset = 0
        self.lock = None
        tools = str(root / "tools")
        if tools not in sys.path:
            sys.path.insert(0, tools)

    def _configure(self):
        import configure
        return configure

    def state(self):
        c = self._configure()
        if (self.data / 'setup-reset.json').exists():
            return {'phase': 'recover', 'step': 3, 'ready': False}
        if (self.data / "setup-pending.json").exists():
            phase = c.pending_phase(self.root, self.data)
            if phase in ("preparing", "binding"):
                return {"phase": "recover", "step": 3, "ready": False}
            ready = self.connection_ready()
            return {"phase": "pairing", "step": 4 if ready else 3, "ready": ready}
        if (self.data / "settings.json").exists():
            try:
                step = json.loads((self.data / "state/setup-view.json").read_text())["step"]
            except (OSError, ValueError, KeyError):
                step = 8  # Existing users are never forced through first-use binding.
            return {"phase": "configured", "step": step if step in (5, 6, 7, 8) else 5, "ready": False}
        try:
            step = json.loads((self.data / "state/setup-view.json").read_text())["step"]
        except (OSError, ValueError, KeyError):
            step = 0
        return {"phase": "new", "step": step if step in (1, 2) else 0, "ready": False}

    def _acquire(self):
        if self.lock is not None:
            return
        self.data.mkdir(parents=True, exist_ok=True)
        self.lock = acquire_file_lock(self.data / "state/setup.lock")
        if self.lock is None:
            raise ValueError("setup_busy")

    def connection_ready(self):
        from desktop_status import qq_log_status
        return (self.child is not None and self.child.poll() is None
                and qq_log_status(self.data / 'logs/setup-gateway.log', self.log_offset) == 'ready')

    def prepare(self):
        if not self.gateway.is_file() or not (self.root / "tools/pairing_agent.py").is_file():
            raise ValueError("setup_resources_missing")
        if os.name == 'nt' and sys.getwindowsversion().build < 17763:
            raise ValueError('windows_version_unsupported')
        self.data.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(self.data).free < 100*1024*1024:
            raise ValueError('setup_disk_space_insufficient')
        with tempfile.TemporaryFile(dir=self.data) as probe:
            probe.write(b'CodexQQ setup write check')
            probe.flush()
            os.fsync(probe.fileno())
        state = self.state()
        if state["phase"] == "new":
            atomic_write_json(self.data / "state/setup-view.json", {"step": 1})
        return self.state()

    def robot_ready(self):
        if self.state()["phase"] != "new":
            raise ValueError("setup_phase_changed")
        atomic_write_json(self.data / "state/setup-view.json", {"step": 2})
        return self.state()

    def edit_credentials(self):
        self.close()
        self._acquire()
        try:
            self._configure().reset_pending(self.root, self.data)
            return self.state()
        finally:
            self.close()

    def begin(self, params):
        from setup_process import PairingGateway, wait_gateway_ready
        self._acquire()
        c = self._configure()
        if (self.data / "settings.json").exists():
            self.close()
            raise ValueError("existing_binding_preserved")
        if self.child is not None and self.child.poll() is None:
            return self.state()
        try:
            c.recover_setup(self.root, self.data)
            if not (self.data / "setup-pending.json").exists():
                app_id, secret = params.get("appId", ""), params.get("appSecret", "")
                if not isinstance(app_id, str) or not isinstance(secret, str):
                    raise ValueError("credentials_invalid")
                sandbox = params.get("sandbox", False)
                if not isinstance(sandbox, bool):
                    raise ValueError("sandbox_invalid")
                cc_data = self.cc_data or self.data.parent / ("CodexCC" if self.data.name == "CodexDesktopWeixin"
                                               else self.data.name + "-qq")
                try:
                    corrected = Path(json.loads((self.data / 'state/setup-view.json').read_text())['ccData'])
                    if corrected.is_absolute() and re.fullmatch(r'QQSetup-[0-9a-f]{12}', corrected.name):
                        cc_data = corrected
                except (OSError, ValueError, KeyError, TypeError):
                    pass
                c.prepare(self.root, self.data, cc_data, app_id, secret, sandbox)
            c.load_pending(self.root, self.data)
            log_path = self.data / "logs/setup-gateway.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("ab") as log:
                offset = log.tell()
                self.log_offset = offset
                self.child = PairingGateway([str(self.gateway), "--config", str(self.data / "cc-connect.toml")],
                                            cwd=self.root, log=log)
            wait_gateway_ready(self.child, log_path, offset=offset)
            return self.state()
        except BaseException:
            self.close()
            raise

    def complete(self, params):
        if params.get("confirmed") is not True:
            raise ValueError("binding_confirmation_required")
        if not self.connection_ready():
            raise ValueError("pairing_ready_required")
        # Hold setup.lock while the identity-only gateway is closed and the
        # existing journal publishes the bound config.
        self.child.close()
        self.child = None
        try:
            self._configure().bind(self.root, self.data, params.get("openId", ""))
            atomic_write_json(self.data / "state/setup-view.json", {"step": 5})
        finally:
            self.close()
        return self.state()

    def advance(self, step):
        if self.state()["phase"] != "configured" or step not in (6, 7, 8):
            raise ValueError("setup_phase_changed")
        atomic_write_json(self.data / "state/setup-view.json", {"step": step})
        return self.state()

    def recover(self):
        self._acquire()
        try:
            self._configure().recover_setup(self.root, self.data)
        finally:
            self.close()
        return self.state()

    def close(self):
        if self.child is not None:
            self.child.close()
            self.child = None
        if self.lock is not None:
            os.close(self.lock[0])
            self.lock = None
