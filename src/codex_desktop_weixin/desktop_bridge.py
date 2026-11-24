"""Bounded JSON-lines bridge for the Tauri desktop host."""
from __future__ import annotations

import json
import os
import subprocess
import sqlite3
import sys
import time
import tomllib
import uuid
from collections import OrderedDict
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

PACKAGE_DIR = Path(__file__).resolve().parent
if str(PACKAGE_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGE_DIR))

from installer import (hook_install_status, install_hooks,
                       managed_hooks_need_path_update, update_managed_hook_paths)
from launcher import (cc_connect_api_status, read_cc_connect_pid, read_monitor_pid,
                      request_stop)
from notifier import DeliverySettings
from process_watch import get_codex_desktop_process_status
from resident_app import ResidentApplication, RUN_KEY, RUN_VALUE, set_startup, startup_command
from runtime_io import acquire_file_lock, atomic_write_json, ensure_runtime_dirs
from desktop_migration import (MigrationError, legacy_launcher_path,
                               migration_required, migrate_paths, recover_paths)
from ui_operations import OperationJournal, OperationConflict
from setup_service import SetupService
from desktop_status import codex_connection_status, qq_connection_status
from native_app_tools import NativeAppTools
from desktop_views import notifications, logs
from configuration_service import ConfigurationService, selected, digest
from desktop_startup import StartupService
from desktop_login import LoginEvidence
from desktop_update import UpdateReceipt

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 1024 * 1024
MAX_CACHED_REPLIES = 256
SUPPORTED_COMMANDS = frozenset({
    "get_snapshot", "check_connections", "start_runtime", "stop_runtime",
    "restart_runtime", "set_startup", "repair_startup", "patch_preferences",
    "install_hooks", "commit_config_change", "complete_binding",
    "send_test_message", "record_user_confirmation", "prepare_files",
    "begin_pairing", "cancel_pairing", "recover_setup", "run_checks",
    "check_update", "download_update", "apply_update", "begin_update", "fail_update", "shutdown", "get_operation",
    "get_setup_state", "robot_ready", "advance_setup", "edit_pairing", "list_notifications", "list_logs",
    "export_diagnostics", "open_codex", "open_known_folder", "open_allowed_url", "begin_rebind", "host_ready", "get_update_guard",
})

DEFAULT_PREFERENCES: dict[str, Any] = {
    "version": 1, "theme": "light", "transparency": True, "motion": True,
    "quality": "auto", "startupView": "overview", "checkUpdates": True,
    "automaticUpdates": False,
}
PREFERENCE_ENUMS = {
    "theme": {"light", "dark", "system"},
    "quality": {"auto", "fine", "light"},
    "startupView": {"overview", "tray"},
}
PREFERENCE_BOOLEANS = {"transparency", "motion", "checkUpdates", "automaticUpdates"}
STARTUP_APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"


class BridgeError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def validate_request(value: Any) -> tuple[str, str, dict[str, Any]]:
    if not isinstance(value, dict) or value.get("version") != PROTOCOL_VERSION:
        raise BridgeError("protocol_version_invalid")
    request_id = value.get("requestId")
    command = value.get("command")
    params = value.get("params", {})
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
        raise BridgeError("request_id_invalid")
    if not isinstance(command, str) or command not in SUPPORTED_COMMANDS:
        raise BridgeError("command_not_allowed")
    if not isinstance(params, dict):
        raise BridgeError("params_invalid")
    return request_id, command, params


class DesktopBridge:
    def __init__(self, codex_home: Path, data_dir: Path, startup_executable: Path,
                 *, app_factory: Callable[[Path, Path], ResidentApplication] = ResidentApplication,
                 now: Callable[[], float] = time.time, login_start: bool = False) -> None:
        self.codex_home = codex_home.expanduser().resolve()
        self.data_dir = data_dir.expanduser().resolve()
        self.startup_executable = startup_executable.resolve()
        self.app_factory = app_factory
        self.now = now
        self.app: ResidentApplication | None = None
        self._replies: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.operations = OperationJournal(self.data_dir / "state/ui-operations.sqlite3")
        self.preference_path = self.data_dir / "ui-preferences.json"
        self._app_tools = NativeAppTools(hint_path=self.data_dir / 'state/native-app-tools.json')
        self._codex_observed = float('-inf')
        self._codex_status = "unknown"
        self._auto_start = False
        self.packaged, self.gateway_path, self.python_path, self.backend_path = self._package_paths()
        self.setup = SetupService(self.backend_path.parent, self.data_dir, self.gateway_path)
        self.startup_service = StartupService(self.data_dir, self.startup_executable)
        self.login_evidence = LoginEvidence(self.data_dir, self.startup_executable)
        self.login_start = login_start
        self.update_receipt = UpdateReceipt(self.data_dir, self.startup_executable, self.operations)
        self.configuration = ConfigurationService(self.backend_path.parent, self.data_dir, self.gateway_path)

    def handle(self, request: Any) -> dict[str, Any]:
        try:
            request_id, command, params = validate_request(request)
        except BridgeError as error:
            return {"status": "unknown", "preview": False, "errorCode": error.code}

        mutation = command not in {"get_snapshot", "check_connections", "run_checks", "get_operation", "get_setup_state", "list_notifications", "list_logs", "shutdown", "get_update_guard"}
        try:
            cached = self.operations.begin(request_id, command, params) if mutation else self._replies.get(request_id)
        except (OperationConflict, OSError, ValueError, sqlite3.Error) as error:
            return {"requestId": request_id, "operationId": request_id, "status": "unknown",
                    "preview": False, "errorCode": str(error) if isinstance(error, OperationConflict) else "operation_journal_unavailable"}
        if cached is not None:
            return cached

        response: dict[str, Any] = {
            "requestId": request_id,
            "operationId": request_id,
            "status": "confirmed",
            "preview": False,
        }
        try:
            data = self._dispatch(command, params, request_id)
            if data is not None:
                response["data"] = data
        except BridgeError as error:
            response.update(status="unknown", errorCode=error.code)
        except (OSError, ValueError, RuntimeError, TypeError, KeyError, sqlite3.Error):
            response.update(status="unknown", errorCode="operation_not_confirmed")

        if mutation and not (command == 'begin_update' and response['status'] == 'confirmed'):
            try:
                self.operations.finish(request_id, response)
            except (OSError, ValueError, sqlite3.Error):
                response.update(status="unknown", errorCode="operation_receipt_not_saved")
        self._replies[request_id] = response
        while len(self._replies) > MAX_CACHED_REPLIES:
            self._replies.popitem(last=False)
        return response

    def _dispatch(self, command: str, params: dict[str, Any], operation_id: str = '') -> dict[str, Any] | None:
        if command == 'begin_update':
            if not self.packaged:
                raise BridgeError('installed_application_required')
            return self.update_receipt.prepare(operation_id, params)
        if command == 'fail_update':
            self.update_receipt.fail(params.get('operationId'))
            return {'update': {'status': 'failed'}}
        if command == "get_update_guard":
            from update_guard import can_update
            return {"guard": can_update(self.data_dir, self.codex_home, self.operations)}
        if command == "host_ready":
            if self.packaged:
                self.update_receipt.reconcile()
            recover_paths(self.data_dir)
            self.configuration.transaction.recover()
            candidate = self.configuration.candidate
            if candidate and any((candidate.data / name).exists() for name in ('setup-pending.json', 'setup-reset.json')):
                candidate.recover()
            self.configuration.reconcile()
            if any((self.data_dir / name).exists() for name in ('setup-pending.json', 'setup-reset.json')):
                self.setup.recover()
            self.login_evidence.observe_launch(self.login_start)
            startup = self._startup_status()
            if startup['verified'] and startup['systemEnabled']:
                self.login_evidence.baseline()
            self._auto_start = True
            return {"ready": True}
        if command == "get_operation":
            operation = params.get("operationId")
            if not isinstance(operation, str) or not 1 <= len(operation) <= 128:
                raise BridgeError("request_id_invalid")
            return {"operation": self.operations.get(operation)}
        if command == "get_setup_state":
            candidate = self.configuration.candidate
            return {"setup": ({**candidate.state(), "rebind": True} if candidate else self.setup.state())}
        if command == "begin_rebind":
            self._stop_runtime()
            try:
                state = self.configuration.begin(params)
            except Exception:
                self.configuration.cancel()
                self._start_runtime()
                raise BridgeError("binding_candidate_failed_original_preserved") from None
            return {"setup": {**state, "rebind": True}, "snapshot": self.snapshot()}
        if command == "commit_config_change":
            options = selected(self.data_dir)[4]
            if params.get("appId") != options["app_id"]:
                raise BridgeError("different_robot_requires_explicit_rebinding")
            sandbox = params.get("sandbox")
            if not isinstance(sandbox, bool):
                raise BridgeError("configuration_value_invalid")
            secret = params.get("appSecret") if params.get("replaceSecret") is True else options["app_secret"]
            if not isinstance(secret, str) or len(secret.strip()) < 8 or "\n" in secret or "\r" in secret:
                raise BridgeError("configuration_value_invalid")
            self._stop_runtime()
            try:
                self.configuration.begin({"appId": options["app_id"], "appSecret": secret, "sandbox": sandbox})
                self.configuration.close()
                self.configuration.commit_options({"app_secret": secret, "sandbox": sandbox})
            except Exception:
                self.configuration.cancel()
                raise BridgeError("configuration_validation_failed_original_preserved") from None
            finally:
                self._start_runtime()
            return {"snapshot": self.snapshot()}
        if command == "advance_setup":
            if not read_monitor_pid(self.data_dir, require_ready=True):
                raise BridgeError("runtime_not_ready")
            if params.get('step') == 6:
                deadline = time.monotonic() + 25
                while qq_connection_status(self.data_dir) != 'ready':
                    if time.monotonic() >= deadline:
                        raise BridgeError('formal_qq_connection_pending')
                    time.sleep(0.25)
            return {"setup": self.setup.advance(params.get("step"))}
        if command == 'edit_pairing':
            candidate = self.configuration.candidate
            state = (candidate or self.setup).edit_credentials()
            return {'setup': {**state, 'rebind': bool(candidate)}}
        if command in {"prepare_files", "robot_ready", "begin_pairing", "complete_binding", "cancel_pairing"}:
            try:
                if command == "prepare_files":
                    state = self.setup.prepare()
                elif command == "robot_ready":
                    state = self.setup.robot_ready()
                elif command == "begin_pairing":
                    if self.configuration.candidate:
                        self._stop_runtime()
                        try:
                            state = self.configuration.begin(params)
                        except Exception:
                            self.configuration.close()
                            self._start_runtime()
                            raise
                    else:
                        state = self.setup.begin(params)
                elif command == "complete_binding":
                    if self.configuration.candidate:
                        try:
                            state = self.configuration.complete(params)
                        finally:
                            self._start_runtime()
                    else:
                        state = self.setup.complete(params)
                else:
                    self.setup.close()
                    if self.configuration.candidate:
                        self.configuration.cancel()
                        self._start_runtime()
                    state = self.setup.state()
            except (ValueError, OSError, RuntimeError):
                raise BridgeError("setup_step_failed_check_diagnostics") from None
            return {"setup": state, "snapshot": self.snapshot()}
        if command in ("get_snapshot", "check_connections"):
            if self._auto_start:
                self._auto_start = False
                if (self.data_dir / "settings.json").is_file() and not self._migration_is_required():
                    try:
                        self._start_runtime()
                    except BridgeError:
                        pass
            return {"snapshot": self.snapshot(refresh_codex=command == "check_connections"),
                    "preferences": self.preferences()}
        if command == "list_notifications":
            return notifications(self.data_dir, offset=params.get("offset", 0), limit=params.get("limit", 200))
        if command == "list_logs":
            return {"logs": logs(self.data_dir)}
        if command == "export_diagnostics":
            value = {"version": 1, "checks": self._checks()["checks"], "logs": logs(self.data_dir)}
            target = self.data_dir / "diagnostics" / ("diagnostics-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".json")
            atomic_write_json(target, value)
            return {"exportPath": str(target)}
        if command == "open_codex":
            from launcher import CODEX_APP_TARGET
            subprocess.Popen(["explorer.exe", CODEX_APP_TARGET], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), close_fds=True)
            return {"opened": True}
        if command == "open_known_folder":
            known = {"app": self.data_dir, "qq": Path(self._settings().data_dir), "install": self.startup_executable.parent,
                     "logs": self.data_dir / "logs", "diagnostics": self.data_dir / "diagnostics"}
            path = known.get(params.get("folder"))
            if path is None or not path.is_absolute() or not path.is_dir():
                raise BridgeError("folder_unavailable")
            os.startfile(path)
            return {"opened": True}
        if command == "open_allowed_url":
            import webbrowser
            url = params.get("url")
            if url not in {"https://q.qq.com/", "https://bot.q.qq.com/wiki/", "https://github.com/IIMovoMII/Codex-QQ/releases"}:
                raise BridgeError("url_not_allowed")
            webbrowser.open(url)
            return {"opened": True}
        if command == "send_test_message":
            from notifier import CCConnectSender
            settings = self._settings()
            if settings.mode != "cc-connect" or not settings.session.startswith("qqbot:"):
                raise BridgeError("test_destination_unavailable")
            ok, _ = CCConnectSender(settings).send("Codex-QQ 连接测试")
            result = {"accepted": ok, "confirmedByUser": False, "at": self.now(), "scope": self._receipt_scope()}
            atomic_write_json(self.data_dir / "state/qq-test.json", result)
            if not ok:
                raise BridgeError("test_send_result_unconfirmed")
            return {"testMessage": result}
        if command == "record_user_confirmation":
            if params.get("received") is not True:
                raise BridgeError("receipt_confirmation_required")
            path = self.data_dir / "state/qq-test.json"
            try:
                value = json.loads(path.read_text())
            except (OSError, ValueError):
                raise BridgeError("test_record_missing") from None
            if not value.get("accepted") or value.get("scope") != self._receipt_scope():
                raise BridgeError("test_send_result_unconfirmed")
            value.update(confirmedByUser=True, confirmedAt=self.now())
            atomic_write_json(path, value)
            return {"snapshot": self.snapshot()}
        if command == "start_runtime":
            self._start_runtime()
            return {"snapshot": self.snapshot()}
        if command == "stop_runtime":
            self._stop_runtime()
            return {"snapshot": self.snapshot()}
        if command == "restart_runtime":
            self._stop_runtime()
            self._start_runtime()
            return {"snapshot": self.snapshot()}
        if command == "set_startup":
            enabled = params.get("enabled")
            if not isinstance(enabled, bool):
                raise BridgeError("startup_value_invalid")
            if enabled:
                self._ensure_startup(legacy_launcher_path(self.data_dir))
            else:
                self.startup_service.set(False)
            state = self._startup_status()
            if not state["verified"] or state["enabled"] != enabled or (enabled and not state["systemEnabled"]):
                raise BridgeError("startup_readback_failed")
            return {"snapshot": self.snapshot()}
        if command == "repair_startup":
            self._ensure_startup(legacy_launcher_path(self.data_dir))
            state = self._startup_status()
            if not state["verified"] or not state["enabled"] or not state["systemEnabled"]:
                raise BridgeError("startup_readback_failed")
            return {"snapshot": self.snapshot()}
        if command == "patch_preferences":
            return {"preferences": self._patch_preferences(params)}
        if command == "install_hooks":
            install_hooks(self.codex_home, self._cli_path(), data_dir=self.data_dir)
            return {"snapshot": self.snapshot()}
        if command == "recover_setup":
            if any((self.data_dir / name).exists() for name in ('setup-pending.json', 'setup-reset.json')):
                return {"setup": self.setup.recover(), "snapshot": self.snapshot()}
            return self._migrate_install()
        if command == "run_checks":
            return self._checks()
        if command == "shutdown":
            self.configuration.close()
            self.setup.close()
            self._stop_owned_runtime()
            return {"stopped": True}
        raise BridgeError("capability_not_implemented")

    def _package_paths(self) -> tuple[bool, Path, Path, Path]:
        if self.package_dir.parent.name == "backend":
            resource_root = self.package_dir.parents[1]
            gateway = resource_root / "gateway.exe"
            python = resource_root / "python" / "python.exe"
            backend = resource_root / "backend"
            return gateway.is_file() and python.is_file(), gateway, python, backend
        project_root = self.package_dir.parents[1]
        gateway = project_root / "vendor/cc-connect/v1.5.0-desktop-control/bin/cc-connect-desktop-control.exe"
        return False, gateway, Path(sys.executable), project_root / "src"

    @property
    def package_dir(self) -> Path:
        return PACKAGE_DIR

    def _migration_is_required(self) -> bool:
        if not self.packaged:
            return False
        # Fresh profiles handle shared Codex hooks at the explicit hooks step.
        # Migration cannot run before this profile has an existing binding.
        if not (self.data_dir / 'settings.json').is_file():
            return (self.data_dir / 'state/install-takeover.json').exists()
        try:
            return ((self.data_dir / 'state/install-takeover.json').exists()
                    or migration_required(self.data_dir, self.gateway_path, self.python_path, self.backend_path)
                    or managed_hooks_need_path_update(self.codex_home, self._cli_path()))
        except (MigrationError, OSError, ValueError, TypeError):
            return True

    def _migrate_install(self) -> dict[str, Any]:
        if not self.packaged:
            raise BridgeError("migration_requires_installed_app")
        marker = self.data_dir / 'state/install-takeover.json'
        if marker.exists():
            pending = json.loads(marker.read_text(encoding='utf-8'))
            if pending.get('version') != 1:
                raise BridgeError('migration_marker_invalid')
            legacy_launcher = Path(pending['legacyLauncher']) if pending.get('legacyLauncher') else None
        else:
            legacy_launcher = legacy_launcher_path(self.data_dir)
            previous = self.startup_service.store.read()
            pending = {'version': 1, 'legacyLauncher': str(legacy_launcher) if legacy_launcher else '',
                       'startupExists': previous['run'] is not None}
            atomic_write_json(marker, pending)
        self._stop_runtime()
        try:
            result = migrate_paths(self.data_dir, self.gateway_path, self.python_path, self.backend_path)
            hooks = update_managed_hook_paths(self.codex_home, self._cli_path(), data_dir=self.data_dir)
            startup = (self.startup_service.set(True, legacy_launcher, enable_approval=False)
                       if pending['startupExists'] else {'changed': False})
        except (OSError, ValueError, TypeError, RuntimeError) as error:
            # A torn file pair is restored first; a failed takeover must not
            # leave an otherwise usable, previously running assistant stopped.
            recover_paths(self.data_dir)
            try:
                self._start_runtime()
            except BridgeError:
                pass
            raise BridgeError("migration_not_confirmed") from error
        self._start_runtime()
        marker.unlink()
        return {"migration": result, "hooks": hooks, "startup": startup, "snapshot": self.snapshot()}

    def _start_runtime(self) -> None:
        if self.app is not None and self.app.worker is not None and self.app.worker.is_alive():
            return
        if read_monitor_pid(self.data_dir, require_ready=True):
            return
        if (self.data_dir / "stop.request").exists():
            (self.data_dir / "stop.request").unlink(missing_ok=True)
        app = self.app_factory(self.codex_home, self.data_dir)
        try:
            started = app.start()
        except Exception as error:
            app.stop()
            raise BridgeError("runtime_start_failed") from error
        if not started:
            if read_monitor_pid(self.data_dir, require_ready=True):
                return
            raise BridgeError("runtime_instance_busy")
        self.app = app

    def _stop_runtime(self) -> None:
        if self.app is not None:
            self._stop_owned_runtime()
            return
        request_stop(self.data_dir, close_desktop=False)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if not read_monitor_pid(self.data_dir):
                # The monitor clears its PID before the resident owner finishes
                # stopping the gateway and releases its lifetime lock.
                released = acquire_file_lock(self.data_dir / 'state/app.lock')
                if released is not None:
                    os.close(released[0])
                    return
            time.sleep(0.1)
        raise BridgeError("runtime_stop_not_confirmed")

    def _stop_owned_runtime(self) -> None:
        app, self.app = self.app, None
        if app is not None:
            app.stop()

    def _startup_status(self) -> dict[str, bool]:
        return self.startup_service.status()

    def _ensure_startup(self, legacy_executable: Path | None) -> dict[str, Any]:
        try:
            result = self.startup_service.set(True, legacy_executable)
            self.login_evidence.baseline()
            return result
        except ValueError as error:
            raise BridgeError(str(error)) from None

    def _settings(self) -> DeliverySettings:
        return DeliverySettings.load(self.data_dir / "settings.json")

    def _public_config(self) -> tuple[str, bool, str]:
        settings = self._settings()
        config_path = Path(settings.config_path) if settings.config_path else None
        if config_path is None or not config_path.is_file():
            return "", False, ""
        try:
            document = tomllib.loads(config_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, tomllib.TOMLDecodeError):
            return "", False, ""

        options = selected(self.data_dir)[4]
        owner = options.get("allow_from", "")
        identity = "•••• " + owner[-4:] if isinstance(owner, str) and owner not in ("", "*") else ""
        return str(options.get("app_id", "")), options.get("sandbox") is True, identity

    def _cli_path(self) -> Path:
        return Path(__file__).resolve().with_name("cli.py")

    def _receipt_scope(self) -> str:
        settings, _, _, _, options, *_ = selected(self.data_dir)
        return digest(json.dumps([options.get("app_id"), options.get("sandbox"), settings.get("session")]).encode())

    def preferences(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.preference_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and payload.get("version") == 1:
                return {**DEFAULT_PREFERENCES,
                        **{key: payload[key] for key in DEFAULT_PREFERENCES if key in payload},
                        "revision": payload.get("revision", 0)}
        except (OSError, ValueError, UnicodeDecodeError):
            pass
        return dict(DEFAULT_PREFERENCES)

    def _patch_preferences(self, params: dict[str, Any]) -> dict[str, Any]:
        allowed = PREFERENCE_ENUMS.keys() | PREFERENCE_BOOLEANS
        updates = {key: value for key, value in params.items() if key != "expectedRevision"}
        if not updates or set(updates) - allowed:
            raise BridgeError("preferences_patch_invalid")
        for key, value in updates.items():
            if key in PREFERENCE_ENUMS and value not in PREFERENCE_ENUMS[key]:
                raise BridgeError("preferences_value_invalid")
            if key in PREFERENCE_BOOLEANS and not isinstance(value, bool):
                raise BridgeError("preferences_value_invalid")
        current = self.preferences()
        expected = params.get("expectedRevision")
        revision = int(current.get("revision", 0))
        if expected is not None and (not isinstance(expected, int) or expected != revision):
            raise BridgeError("preferences_revision_conflict")
        updated = {**current, **updates, "version": 1, "revision": revision + 1}
        atomic_write_json(self.preference_path, updated)
        return {key: updated[key] for key in DEFAULT_PREFERENCES} | {"revision": updated["revision"]}

    def _checks(self) -> dict[str, Any]:
        hooks = hook_install_status(self.codex_home)
        snapshot = self.snapshot(refresh_codex=True)
        return {
            "snapshot": snapshot,
            "checks": {
                "codexHome": self.codex_home.is_dir(),
                "hooksEnabled": bool(hooks.get("features_hooks_enabled")),
                "hooksInstalled": bool(hooks.get("managed_hooks_installed")),
                "runtimeReady": snapshot["runtime"] == "ready",
                "qqApiReady": snapshot["qqApi"] == "ready",
            },
        }

    def snapshot(self, *, refresh_codex: bool = False) -> dict[str, Any]:
        try:
            if refresh_codex or time.monotonic() - self._codex_observed > 10:
                self._codex_status = codex_connection_status(self._app_tools)
                self._codex_observed = time.monotonic()
            codex_state = self._codex_status
        except (OSError, RuntimeError):
            codex_state = "unknown"
        observed = datetime.fromtimestamp(self.now(), timezone.utc).astimezone().isoformat(timespec="seconds")

        monitor_pid = read_monitor_pid(self.data_dir, require_ready=True)
        app_state: dict[str, Any] = {}
        try:
            loaded = json.loads((self.data_dir / "state" / "app.json").read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                app_state = loaded
        except (OSError, ValueError):
            pass
        if monitor_pid:
            runtime_state = "ready"
        elif app_state and app_state.get("ready") is False:
            runtime_state = "stopped"
        else:
            runtime_state = "unknown"

        try:
            delivery = self._settings()
            configured = (delivery.mode == "cc-connect" and bool(delivery.config_path)
                          and Path(delivery.config_path).is_file())
        except (OSError, ValueError):
            delivery = DeliverySettings()
            configured = False
        api_status = cc_connect_api_status(self.data_dir) if delivery.mode == "cc-connect" else "not_used"
        qq_state = qq_connection_status(self.data_dir) if configured else "unknown"
        try:
            app_id, sandbox, identity = self._public_config()
        except (OSError, ValueError):
            app_id, sandbox, identity = "", False, ""
        try:
            group_rule = selected(self.data_dir)[4].get('group_allow_from', '')
            groups_enabled = bool(group_rule.strip()) if isinstance(group_rule, str) else bool(group_rule)
        except (OSError, ValueError, KeyError, TypeError):
            groups_enabled = None
        try:
            hook_status = hook_install_status(self.codex_home)
            hooks = ("ready" if hook_status.get("features_hooks_enabled") and hook_status.get("managed_hooks_installed")
                     else "missing")
        except (OSError, ValueError, TypeError):
            hook_status = {}
            hooks = "unknown"
        startup = self._startup_status()
        try:
            receipt = json.loads((self.data_dir / "state/qq-test.json").read_text())
            test_submitted = receipt.get("accepted") is True and receipt.get("scope") == self._receipt_scope()
            received = test_submitted and receipt.get("confirmedByUser") is True
        except (OSError, ValueError, TypeError):
            received, test_submitted = False, False
        return {
            "revision": time.time_ns(),
            "observedAt": observed,
            "runtime": runtime_state,
            "codex": codex_state,
            "qq": qq_state,
            "qqApi": api_status,
            "configured": configured,
            "appId": app_id,
            "sandbox": sandbox,
            "identity": identity,
            "groupsEnabled": groups_enabled,
            "hooks": hooks,
            "hooksInstalled": hook_status.get("managed_hooks_installed"),
            "hooksEnabled": hook_status.get("features_hooks_enabled"),
            # Installation is observable; Codex's trust decision has no supported
            # read API. Unknown must not be presented as an outstanding approval.
            "hooksTrust": "unknown",
            "startup": startup["enabled"],
            "startupEnabled": startup["systemEnabled"],
            "startupVerified": self.login_evidence.verified(startup=startup['verified'] and startup['systemEnabled'],
                                                          runtime_ready=runtime_state == 'ready', qq_ready=qq_state == 'ready'),
            "startupConfiguredVerified": startup["verified"],
            "receivedByUser": received,
            "testSubmitted": test_submitted,
            "appDataPath": str(self.data_dir),
            "qqDataPath": str(delivery.data_dir) if configured else "",
            "installPath": str(self.startup_executable.parent),
            "migrationRequired": self._migration_is_required(),
        }

    def close(self) -> None:
        self.configuration.close()
        self.setup.close()
        self._stop_owned_runtime()


def serve(bridge: DesktopBridge, *, stdin=None, stdout=None) -> int:
    source = stdin or sys.stdin.buffer
    sink = stdout or sys.stdout.buffer
    while True:
        frame = source.readline(MAX_FRAME_BYTES + 1)
        if not frame:
            bridge.close()
            return 0
        if len(frame) > MAX_FRAME_BYTES or not frame.endswith(b"\n"):
            bridge.close()
            return 2
        request = None
        try:
            request = json.loads(frame.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            response = {"status": "unknown", "preview": False, "errorCode": "request_json_invalid"}
        else:
            response = bridge.handle(request)
        encoded = (json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(encoded) > MAX_FRAME_BYTES:
            encoded = b'{"status":"unknown","preview":false,"errorCode":"response_too_large"}\n'
        sink.write(encoded)
        sink.flush()
        if isinstance(request, dict) and request.get("command") == "shutdown":
            bridge.close()
            return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--codex-home", type=Path, default=Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex"))
    parser.add_argument("--data-dir", type=Path, default=Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "CodexDesktopWeixin")
    parser.add_argument("--startup-executable", type=Path, required=True)
    parser.add_argument("--host-handshake", action="store_true")
    parser.add_argument('--login-start', action='store_true')
    args = parser.parse_args(argv)
    # The resident monitor prints console event summaries from its worker.
    # stdout belongs exclusively to the host protocol, including between RPCs.
    # Preserve its binary sink before redirecting all incidental Python output.
    protocol_stdout = sys.stdout.buffer
    with redirect_stdout(sys.stderr):
        bridge = DesktopBridge(args.codex_home, args.data_dir, args.startup_executable, login_start=args.login_start)
        if not args.host_handshake and (bridge.data_dir / "settings.json").is_file() and not bridge._migration_is_required():
            try:
                bridge._start_runtime()
            except BridgeError:
                pass
        return serve(bridge, stdout=protocol_stdout)


if __name__ == "__main__":
    raise SystemExit(main())
