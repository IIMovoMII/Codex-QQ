from __future__ import annotations

import ctypes
import json
import os
import signal
import shutil
import socket
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

from process_watch import get_codex_desktop_process_status, request_codex_desktop_close
from notifier import DeliverySettings
from runtime_io import atomic_write_json, ensure_runtime_dirs
from rollout import is_desktop_rollout


CODEX_APP_TARGET = r"shell:AppsFolder\OpenAI.Codex_2p2nqsd0c76g0!App"
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001
PROCESS_IDENTITY_VERSION = 1
PROCESS_START_TIME_TOLERANCE_SECONDS = 5.0
WINDOWS_EPOCH_OFFSET_SECONDS = 11644473600


def _query_process_identity(pid: int) -> dict[str, Any]:
    """Return enough identity data to detect Windows PID reuse.

    A PID is only a temporary handle.  The state files therefore carry the
    process image and creation time as well.  The creation time is the
    decisive field: Windows may reuse a PID for an unrelated process after a
    managed process exits.
    """
    if pid <= 0:
        return {"process_image": "", "process_start_time": None}
    if os.name != "nt":
        try:
            image = os.path.realpath(f"/proc/{pid}/exe")
        except OSError:
            image = ""
        return {"process_image": image, "process_start_time": None}

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    open_process.restype = ctypes.c_void_p
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    query_path = kernel32.QueryFullProcessImageNameW
    query_path.argtypes = [ctypes.c_void_p, ctypes.c_ulong, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    query_path.restype = ctypes.c_int
    get_times = kernel32.GetProcessTimes
    get_times.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    get_times.restype = ctypes.c_int

    handle = open_process(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return {"process_image": "", "process_start_time": None}
    try:
        image = ""
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if query_path(handle, 0, buffer, ctypes.byref(size)):
            image = buffer.value

        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        start_time: float | None = None
        if get_times(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            ticks = (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
            start_time = ticks / 10_000_000 - WINDOWS_EPOCH_OFFSET_SECONDS
        return {"process_image": image, "process_start_time": start_time}
    finally:
        close_handle(handle)


def _capture_process_identity(pid: int) -> dict[str, Any]:
    """Capture a managed process identity, retrying its first few milliseconds."""
    identity = _query_process_identity(pid)
    for _ in range(5):
        if identity.get("process_image") and (os.name != "nt" or identity.get("process_start_time") is not None):
            break
        time.sleep(0.05)
        identity = _query_process_identity(pid)
    return {"identity_version": PROCESS_IDENTITY_VERSION, **identity}


def _process_identity_matches(state: dict[str, Any], pid: int) -> bool:
    """Check that a live PID is still the exact process recorded in state."""
    try:
        if int(state.get("identity_version", 0)) != PROCESS_IDENTITY_VERSION:
            return False
    except (TypeError, ValueError):
        return False

    actual = _query_process_identity(pid)
    expected_image = str(state.get("process_image") or "")
    actual_image = str(actual.get("process_image") or "")
    if not expected_image or not actual_image:
        return False
    if os.path.normcase(os.path.normpath(expected_image)) != os.path.normcase(os.path.normpath(actual_image)):
        return False

    expected_start = state.get("process_start_time")
    actual_start = actual.get("process_start_time")
    if os.name == "nt":
        try:
            if expected_start is None or actual_start is None:
                return False
            if abs(float(expected_start) - float(actual_start)) > PROCESS_START_TIME_TOLERANCE_SECONDS:
                return False
        except (TypeError, ValueError):
            return False
    return True


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    open_process.restype = ctypes.c_void_p
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    handle = open_process(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    close_handle(handle)
    return True


def read_monitor_pid(data_dir: Path, *, require_ready: bool = False) -> int | None:
    try:
        value = json.loads((data_dir / "state" / "monitor.json").read_text(encoding="utf-8"))
        pid = int(value.get("pid", 0))
        if require_ready and value.get("ready") is False:
            return None
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
        return None
    return pid if _pid_alive(pid) and _process_identity_matches(value, pid) else None


def write_monitor_pid(data_dir: Path, *, ready: bool = True) -> None:
    paths = ensure_runtime_dirs(data_dir)
    pid = os.getpid()
    atomic_write_json(
        paths["state"] / "monitor.json",
        {"pid": pid, "started_at": time.time(), "ready": ready, **_capture_process_identity(pid)},
    )


def clear_monitor_pid(data_dir: Path) -> None:
    path = data_dir / "state" / "monitor.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if int(value.get("pid", 0)) != os.getpid():
            return
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
        return
    try:
        path.unlink()
    except OSError:
        pass


def _service_state_path(data_dir: Path) -> Path:
    return data_dir / "state" / "cc-connect.json"


def _read_service_state(data_dir: Path) -> dict[str, Any]:
    try:
        value = json.loads(_service_state_path(data_dir).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


_WINDOWS_CTRL_BREAK_HELPER = r"""
import ctypes
import sys
import time

pid = int(sys.argv[1])
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
handler_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_ulong)
ignore_handler = handler_type(lambda _event: True)
kernel32.AttachConsole.argtypes = [ctypes.c_ulong]
kernel32.AttachConsole.restype = ctypes.c_int
kernel32.SetConsoleCtrlHandler.argtypes = [handler_type, ctypes.c_int]
kernel32.SetConsoleCtrlHandler.restype = ctypes.c_int
kernel32.GenerateConsoleCtrlEvent.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
kernel32.GenerateConsoleCtrlEvent.restype = ctypes.c_int
kernel32.FreeConsole.argtypes = []
kernel32.FreeConsole.restype = ctypes.c_int

kernel32.FreeConsole()
if not kernel32.AttachConsole(pid):
    raise ctypes.WinError(ctypes.get_last_error())
if not kernel32.SetConsoleCtrlHandler(ignore_handler, True):
    raise ctypes.WinError(ctypes.get_last_error())
if not kernel32.GenerateConsoleCtrlEvent(1, 0):
    raise ctypes.WinError(ctypes.get_last_error())
time.sleep(0.2)
kernel32.FreeConsole()
"""


def _send_windows_ctrl_break(pid: int) -> None:
    """Deliver Ctrl+Break from an isolated helper process.

    The service owns a separate hidden console.  The sender can receive the same
    Ctrl+Break event, so it must not be the monitor process that owns cleanup.
    """
    helper = subprocess.Popen(
        [sys.executable, "-c", _WINDOWS_CTRL_BREAK_HELPER, str(pid)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        close_fds=True,
    )
    try:
        helper.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        helper.kill()


def _force_terminate_process(pid: int) -> None:
    if os.name != "nt":
        os.kill(pid, signal.SIGKILL)
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    open_process.restype = ctypes.c_void_p
    terminate_process = kernel32.TerminateProcess
    terminate_process.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    terminate_process.restype = ctypes.c_int
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    handle = open_process(PROCESS_TERMINATE, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not terminate_process(handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        close_handle(handle)


def read_cc_connect_pid(data_dir: Path) -> int | None:
    try:
        value = _read_service_state(data_dir)
        pid = int(value.get("pid", 0))
    except (TypeError, ValueError):
        return None
    return pid if _pid_alive(pid) and _process_identity_matches(value, pid) else None


def cc_connect_api_socket_path(data_dir: Path) -> Path:
    settings = DeliverySettings.load(data_dir / "settings.json")
    configured = Path(settings.data_dir) if settings.data_dir else data_dir / "cc-connect-data"
    if not configured.is_absolute():
        configured = data_dir / configured
    return configured / "run" / "api.sock"


def _socket_is_reachable(path: Path) -> bool | None:
    """Return True/False when this Python build can probe AF_UNIX, else None."""
    family = getattr(socket, "AF_UNIX", None)
    if family is None:
        return None
    stream = socket.socket(family, socket.SOCK_STREAM)
    stream.settimeout(0.3)
    try:
        stream.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        stream.close()


def _directory_contains_name(path: Path) -> bool:
    """Detect special Windows entries whose stat/lstat calls can fail."""
    try:
        wanted = os.path.normcase(path.name)
        with os.scandir(path.parent) as entries:
            return any(os.path.normcase(entry.name) == wanted for entry in entries)
    except OSError:
        return os.path.lexists(path)


def _unlink_socket_path(path: Path) -> None:
    path.unlink()


def _rotate_cc_connect_run_directory(socket_path: Path) -> dict[str, Any]:
    """Replace a poisoned Windows run directory while preserving regular state files."""
    run_dir = socket_path.parent
    if run_dir.name.lower() != "run":
        return {
            "socket_cleanup": "failed",
            "socket_path": str(socket_path),
            "socket_error": "refused to rotate a directory not named run",
        }
    suffix = time.strftime("%Y%m%d-%H%M%S")
    stale_dir = run_dir.with_name(f"run-stale-{suffix}-{os.getpid()}")
    counter = 0
    while stale_dir.exists():
        counter += 1
        stale_dir = run_dir.with_name(f"run-stale-{suffix}-{os.getpid()}-{counter}")
    try:
        run_dir.rename(stale_dir)
        run_dir.mkdir(parents=True, exist_ok=False)
    except OSError as error:
        if stale_dir.exists() and not run_dir.exists():
            try:
                stale_dir.rename(run_dir)
            except OSError:
                pass
        return {
            "socket_cleanup": "failed",
            "socket_path": str(socket_path),
            "socket_error": f"run directory rotation failed: {error}",
        }

    preserved: list[str] = []
    preserve_errors: list[str] = []
    try:
        children = list(stale_dir.iterdir())
    except OSError as error:
        children = []
        preserve_errors.append(str(error))
    for child in children:
        if os.path.normcase(child.name) == os.path.normcase(socket_path.name):
            continue
        try:
            if child.is_file():
                shutil.copy2(child, run_dir / child.name)
                preserved.append(child.name)
        except OSError as error:
            preserve_errors.append(f"{child.name}: {error}")
    result: dict[str, Any] = {
        "socket_cleanup": "rotated_run_directory",
        "socket_path": str(socket_path),
        "stale_run_directory": str(stale_dir),
        "preserved_run_files": preserved,
    }
    if preserve_errors:
        result["preserve_warnings"] = preserve_errors
    return result


def cleanup_stale_cc_connect_socket(data_dir: Path) -> dict[str, Any]:
    """Remove the project-owned API socket only while no managed service is alive."""
    socket_path = cc_connect_api_socket_path(data_dir)
    if read_cc_connect_pid(data_dir) is not None:
        return {"socket_cleanup": "skipped_service_alive", "socket_path": str(socket_path)}
    if not _directory_contains_name(socket_path):
        return {"socket_cleanup": "absent", "socket_path": str(socket_path)}
    reachable = _socket_is_reachable(socket_path)
    if reachable is True:
        return {"socket_cleanup": "skipped_reachable", "socket_path": str(socket_path)}
    try:
        _unlink_socket_path(socket_path)
    except OSError as error:
        if os.name == "nt":
            rotated = _rotate_cc_connect_run_directory(socket_path)
            if rotated.get("socket_cleanup") != "failed":
                rotated["socket_error"] = str(error)
                return rotated
        return {
            "socket_cleanup": "failed",
            "socket_path": str(socket_path),
            "socket_error": str(error),
        }
    return {"socket_cleanup": "removed", "socket_path": str(socket_path)}


def _api_status_from_log(log_path: Path, *, start_offset: int = 0) -> str:
    try:
        with log_path.open("rb") as stream:
            stream.seek(max(0, start_offset))
            raw = stream.read(512 * 1024)
    except (FileNotFoundError, OSError):
        return "unknown"
    status = "unknown"
    for line in raw.decode("utf-8", errors="replace").splitlines():
        lowered = line.lower()
        if "api server started" in lowered:
            status = "ready"
        elif "api server unavailable" in lowered:
            status = "unavailable"
    return status


def cc_connect_api_status(data_dir: Path) -> str:
    # A historical log line is not proof that the current bridge is alive.
    # First validate the managed process identity; otherwise a reused PID and
    # an old "api server started" line can make doctor/autostart lie forever.
    if read_cc_connect_pid(data_dir) is None:
        socket_path = cc_connect_api_socket_path(data_dir)
        if _socket_is_reachable(socket_path) is True:
            return "reachable_untracked"
        return "missing" if not _directory_contains_name(socket_path) else "unavailable"
    state = _read_service_state(data_dir)
    try:
        start_offset = int(state.get("log_offset", 0))
    except (TypeError, ValueError):
        start_offset = 0
    status = _api_status_from_log(data_dir / "logs" / "cc-connect.log", start_offset=start_offset)
    if status == "unknown" and not _directory_contains_name(cc_connect_api_socket_path(data_dir)):
        return "missing"
    return status


def _wait_for_cc_connect_api(
    process: subprocess.Popen[bytes],
    log_path: Path,
    *,
    start_offset: int,
    timeout_seconds: float = 5.0,
) -> str:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return "process_exited"
        status = _api_status_from_log(log_path, start_offset=start_offset)
        if status in ("ready", "unavailable"):
            return status
        time.sleep(0.1)
    return _api_status_from_log(log_path, start_offset=start_offset)


def start_cc_connect_service(data_dir: Path) -> dict[str, Any]:
    paths = ensure_runtime_dirs(data_dir)
    # Desktop gives its native service a new pipe name each start. Preserve only
    # that small hint for ACP processes launched without the original environment.
    # Consumers still verify the real server PID/creation time and bundle contract.
    from endpoint_hint import persist_native_endpoint
    persist_native_endpoint(data_dir)
    existing = read_cc_connect_pid(data_dir)
    if existing:
        api_status = cc_connect_api_status(data_dir)
        if api_status not in ("unavailable", "missing"):
            return {
                "cc_connect_started": False,
                "cc_connect_pid": existing,
                "reason": "already_running",
                "cc_connect_api_status": api_status,
            }
        stopped = stop_cc_connect_service(data_dir)
        if not stopped.get("cc_connect_stopped"):
            return {
                "cc_connect_started": False,
                "cc_connect_pid": existing,
                "reason": "api_unavailable_restart_failed",
                "cc_connect_api_status": api_status,
                "stop_result": stopped,
            }
    settings = DeliverySettings.load(paths["root"] / "settings.json")
    executable = shutil.which(settings.executable) or settings.executable
    if not executable or not Path(executable).exists():
        return {"cc_connect_started": False, "cc_connect_pid": None, "reason": "not_configured"}
    if not settings.config_path or not Path(settings.config_path).is_file():
        return {"cc_connect_started": False, "cc_connect_pid": None, "reason": "config_missing"}
    socket_cleanup = cleanup_stale_cc_connect_socket(data_dir)
    if socket_cleanup.get("socket_cleanup") == "failed":
        return {
            "cc_connect_started": False,
            "cc_connect_pid": None,
            "reason": "stale_socket_cleanup_failed",
            **socket_cleanup,
        }
    log_path = paths["logs"] / "cc-connect.log"
    try:
        log_offset = log_path.stat().st_size
    except OSError:
        log_offset = 0
    flags = 0
    startupinfo = None
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
    with log_path.open("ab") as log:
        process = subprocess.Popen(
            [executable, "--config", settings.config_path],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            creationflags=flags,
            startupinfo=startupinfo,
            close_fds=True,
        )
    atomic_write_json(
        _service_state_path(data_dir),
        {
            "pid": process.pid,
            "started_at": time.time(),
            "executable": str(executable),
            "config_path": settings.config_path,
            "log_offset": log_offset,
            "socket_path": str(cc_connect_api_socket_path(data_dir)),
            **_capture_process_identity(process.pid),
        },
    )
    api_status = _wait_for_cc_connect_api(process, log_path, start_offset=log_offset)
    if api_status == "process_exited":
        try:
            _service_state_path(data_dir).unlink()
        except OSError:
            pass
        cleanup_stale_cc_connect_socket(data_dir)
        return {
            "cc_connect_started": False,
            "cc_connect_pid": None,
            "reason": f"exited_{process.returncode}",
            "cc_connect_api_status": api_status,
            "log_path": str(log_path),
        }
    if api_status != "ready":
        stop_result = stop_cc_connect_service(data_dir)
        return {
            "cc_connect_started": False,
            "cc_connect_pid": None,
            "reason": "api_unavailable",
            "cc_connect_api_status": api_status,
            "log_path": str(log_path),
            "stop_result": stop_result,
        }
    return {
        "cc_connect_started": True,
        "cc_connect_pid": process.pid,
        "cc_connect_api_status": "ready",
        "log_path": str(log_path),
        **socket_cleanup,
    }


def stop_cc_connect_service(data_dir: Path, *, timeout_seconds: float = 8.0) -> dict[str, Any]:
    pid = read_cc_connect_pid(data_dir)
    if not pid:
        try:
            _service_state_path(data_dir).unlink()
        except OSError:
            pass
        return {
            "cc_connect_stopped": True,
            "cc_connect_pid": None,
            "reason": "not_running",
            **cleanup_stale_cc_connect_socket(data_dir),
        }
    try:
        if os.name == "nt":
            _send_windows_ctrl_break(pid)
        else:
            os.kill(pid, signal.SIGTERM)
    except OSError as error:
        return {"cc_connect_stopped": False, "cc_connect_pid": pid, "reason": str(error)}
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            try:
                _service_state_path(data_dir).unlink()
            except OSError:
                pass
            return {
                "cc_connect_stopped": True,
                "cc_connect_pid": pid,
                "reason": "graceful",
                **cleanup_stale_cc_connect_socket(data_dir),
            }
        time.sleep(0.1)
    try:
        _force_terminate_process(pid)
    except OSError as error:
        if not _pid_alive(pid):
            try:
                _service_state_path(data_dir).unlink()
            except OSError:
                pass
            return {
                "cc_connect_stopped": True,
                "cc_connect_pid": pid,
                "reason": "graceful_during_force_race",
                **cleanup_stale_cc_connect_socket(data_dir),
            }
        return {"cc_connect_stopped": False, "cc_connect_pid": pid, "reason": f"graceful_timeout; force_failed: {error}"}
    forced_deadline = time.monotonic() + 2.0
    while time.monotonic() < forced_deadline:
        if not _pid_alive(pid):
            try:
                _service_state_path(data_dir).unlink()
            except OSError:
                pass
            return {
                "cc_connect_stopped": True,
                "cc_connect_pid": pid,
                "reason": "forced_after_graceful_timeout",
                **cleanup_stale_cc_connect_socket(data_dir),
            }
        time.sleep(0.1)
    return {"cc_connect_stopped": False, "cc_connect_pid": pid, "reason": "force_timeout"}


def recover_cc_connect_service(data_dir: Path) -> dict[str, Any]:
    """Recover the dedicated bridge after an actual local-API delivery failure."""
    stopped = stop_cc_connect_service(data_dir)
    if not stopped.get("cc_connect_stopped"):
        return {"recovered": False, "reason": "stop_failed", "stop_result": stopped}
    started = start_cc_connect_service(data_dir)
    recovered = bool(started.get("cc_connect_api_status") == "ready" and started.get("cc_connect_pid"))
    return {
        "recovered": recovered,
        "reason": "restarted" if recovered else str(started.get("reason") or "start_failed"),
        "stop_result": stopped,
        "start_result": started,
    }


def wait_for_codex_desktop(
    *,
    timeout_seconds: float = 30.0,
    poll_seconds: float = 0.5,
    transcript_path: str | Path | None = None,
) -> dict[str, Any]:
    """Wait for a Desktop session or process tree to become observable.

    Codex can emit its first hook while the packaged Desktop process is still
    being created or while the first JSONL record is still incomplete. This
    bounded retry lives outside the synchronous hook and returns evidence for
    the autostart log instead of silently giving up.
    """
    timeout = max(0.0, float(timeout_seconds))
    interval = max(0.05, float(poll_seconds))
    started = time.monotonic()
    deadline = started + timeout
    attempts = 0
    transcript = Path(transcript_path) if transcript_path else None
    while True:
        attempts += 1
        classification = None
        if transcript is not None:
            try:
                classification = is_desktop_rollout(transcript)
            except (FileNotFoundError, PermissionError, OSError):
                classification = None
            if classification is False:
                return {
                    "desktop_detected": False,
                    "app_server_detected": False,
                    "reason": "non_desktop_session",
                    "attempts": attempts,
                    "wait_seconds": time.monotonic() - started,
                }
        status = get_codex_desktop_process_status()
        if status.desktop_alive and (transcript is None or classification is True):
            return {
                "desktop_detected": True,
                "app_server_detected": status.app_server_alive,
                "reason": "desktop_detected",
                "attempts": attempts,
                "wait_seconds": time.monotonic() - started,
            }
        if time.monotonic() >= deadline:
            reason = "rollout_not_ready" if transcript is not None and classification is None else "desktop_not_detected"
            return {
                "desktop_detected": False,
                "app_server_detected": status.app_server_alive,
                "reason": reason,
                "attempts": attempts,
                "wait_seconds": timeout,
            }
        time.sleep(min(interval, max(0.0, deadline - time.monotonic())))


def start_background_monitor(cli_path: Path, codex_home: Path, data_dir: Path) -> dict[str, Any]:
    paths = ensure_runtime_dirs(data_dir)
    existing = read_monitor_pid(data_dir)
    if existing:
        for _ in range(30):
            if read_monitor_pid(data_dir, require_ready=True) == existing:
                return {"monitor_started": False, "monitor_pid": existing, "reason": "already_running"}
            if read_monitor_pid(data_dir) != existing:
                break
            time.sleep(0.1)
        else:
            return {"monitor_started": False, "monitor_pid": None, "reason": "monitor_starting"}
    try:
        (paths["root"] / "stop.request").unlink()
    except FileNotFoundError:
        pass
    command = [
        sys.executable,
        "-X",
        "utf8",
        str(cli_path.resolve()),
        "monitor",
        "--codex-home",
        str(codex_home.resolve()),
        "--data-dir",
        str(data_dir.resolve()),
        "--exit-with-desktop",
        "--manage-cc-connect",
        "--write-pid",
    ]
    flags = 0
    if os.name == "nt":
        flags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    log_path = paths["logs"] / "monitor-console.log"
    with log_path.open("ab") as log:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            creationflags=flags,
            close_fds=True,
        )
    for _ in range(30):
        detected = read_monitor_pid(data_dir, require_ready=True)
        if detected:
            return {"monitor_started": True, "monitor_pid": detected, "log_path": str(log_path)}
        if process.poll() is not None:
            break
        time.sleep(0.1)
    return {"monitor_started": False, "monitor_pid": None, "reason": "startup_failed", "log_path": str(log_path)}


def launch_codex_desktop() -> bool:
    if get_codex_desktop_process_status().desktop_alive:
        return False
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    subprocess.Popen(
        ["explorer.exe", CODEX_APP_TARGET],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
        close_fds=True,
    )
    return True


def request_stop(data_dir: Path, *, close_desktop: bool) -> dict[str, Any]:
    paths = ensure_runtime_dirs(data_dir)
    atomic_write_json(paths["root"] / "stop.request", {"requested_at": time.time()})
    result = {
        "monitor_pid": read_monitor_pid(data_dir),
        "desktop_close_requested": request_codex_desktop_close() if close_desktop else False,
    }
    if result["monitor_pid"] is None:
        result.update(stop_cc_connect_service(data_dir))
    return result
