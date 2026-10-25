from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import unittest
from dataclasses import replace
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parents[1]
if str(PACKAGE_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGE_DIR))

from history_analysis import analyze_history
from hook_bridge import hook_main
from installer import hook_install_status, install_hooks, uninstall_hooks
from launcher import (
    cc_connect_api_socket_path,
    cc_connect_api_status,
    clear_monitor_pid,
    launch_codex_desktop,
    read_monitor_pid,
    request_stop,
    read_cc_connect_pid,
    start_cc_connect_service,
    stop_cc_connect_service,
    start_background_monitor,
    wait_for_codex_desktop,
    write_monitor_pid,
)
from monitor import DesktopMonitor, MonitorConfig
from models import TurnState
from notifier import DeliverySettings, save_delivery_settings
from process_watch import get_codex_desktop_process_status
from rollout import iter_rollout_signals
from state_machine import StateMachine
from runtime_io import append_json_line, default_data_dir, ensure_runtime_dirs, acquire_file_lock
from runtime_mode import runtime_mode


def _default_codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def command_test() -> int:
    suite = unittest.defaultTestLoader.discover(str(PROJECT_DIR / "tests"), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


def command_analyze_history(codex_home: Path, *, include_examples: bool) -> int:
    result = analyze_history(codex_home / "sessions", include_examples=include_examples)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def command_replay(path: Path) -> int:
    machine = StateMachine()
    notifications = []
    for _, signal in iter_rollout_signals(path):
        notifications.extend(machine.apply(signal))
    summary: dict[str, int] = {}
    for notification in notifications:
        summary[notification.state.value] = summary.get(notification.state.value, 0) + 1
    print(json.dumps({"path": str(path), "notifications": summary, "total": len(notifications)}, ensure_ascii=False, indent=2))
    return 0


def command_monitor(
    codex_home: Path,
    data_dir: Path,
    duration: float | None,
    *,
    exit_with_desktop: bool,
    write_pid: bool,
    manage_cc_connect: bool,
) -> int:
    if write_pid:
        existing = read_monitor_pid(data_dir)
        if existing and existing != os.getpid():
            print(json.dumps({"error": "monitor_already_running", "pid": existing}, ensure_ascii=False))
            return 3
        write_monitor_pid(data_dir, ready=False)
    try:
        monitor = DesktopMonitor(
            MonitorConfig(
                codex_home=codex_home,
                data_dir=data_dir,
                exit_with_desktop=exit_with_desktop,
                manage_cc_connect=manage_cc_connect,
            )
        )
        monitor.run(
            duration_seconds=duration,
            on_started=(lambda: write_monitor_pid(data_dir, ready=True)) if write_pid else None,
        )
        return 0
    finally:
        if write_pid:
            clear_monitor_pid(data_dir)


def command_install_hooks(codex_home: Path, data_dir: Path) -> int:
    result = install_hooks(codex_home, Path(__file__), data_dir=data_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def command_doctor(codex_home: Path, data_dir: Path) -> int:
    status = hook_install_status(codex_home)
    try:
        delivery = DeliverySettings.load(data_dir / "settings.json")
    except ValueError as error:
        status.update({"data_dir": str(data_dir), "delivery_mode": "invalid",
                       "delivery_error": type(error).__name__, "healthy": False})
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 1
    processes = get_codex_desktop_process_status()
    api_status = cc_connect_api_status(data_dir) if delivery.mode == "cc-connect" else "not_used"
    status.update(
        {
            "codex_home_exists": codex_home.exists(),
            "session_root_exists": (codex_home / "sessions").exists(),
            "data_dir": str(data_dir),
            "monitor_pid": read_monitor_pid(data_dir, require_ready=True),
            "cc_connect_pid": read_cc_connect_pid(data_dir),
            "desktop_alive": processes.desktop_alive,
            "desktop_app_server_alive": processes.app_server_alive,
            "delivery_mode": delivery.mode,
            "runtime_mode": runtime_mode(data_dir),
            "cc_connect_executable": shutil.which(delivery.executable) or None,
            "cc_connect_api_status": api_status,
            "cc_connect_api_socket": str(cc_connect_api_socket_path(data_dir)),
            "outbox_pending": len(list((data_dir / "outbox" / "pending").glob("*.json"))),
        }
    )
    print(json.dumps(status, ensure_ascii=False, indent=2))
    healthy = bool(status["codex_home_exists"] and status["session_root_exists"])
    if processes.desktop_alive:
        healthy = healthy and bool(status["monitor_pid"] and status["features_hooks_enabled"] and status["managed_hooks_installed"])
    if delivery.mode == "cc-connect" and api_status != "ready":
        healthy = False
    return 0 if healthy else 1


def command_start(codex_home: Path, data_dir: Path) -> int:
    mode = runtime_mode(data_dir)
    if mode == 'unknown':
        print('应用模式无法核实，未启动 Codex 或后台。请双击独立应用入口重新开启。')
        return 3
    if mode == 'independent':
        from resident_app import launch_app
        print(json.dumps(launch_app(codex_home, data_dir), ensure_ascii=False))
        return 0
    desktop_launched = launch_codex_desktop()
    result_code = command_ensure_runtime(codex_home, data_dir, trigger_event="manual_start")
    result = {
        "cc_connect_pid": read_cc_connect_pid(data_dir),
        "monitor_pid": read_monitor_pid(data_dir),
        "desktop_launched": desktop_launched,
        "desktop_was_running": not desktop_launched,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result_code


def command_reload_bridge(data_dir: Path) -> int:
    """Reload ACP code only; never launches/closes Desktop or runs a task."""
    if runtime_mode(data_dir) == 'unknown':
        print('应用模式无法核实，未启动桥接。请先重新开启独立应用。')
        return 3
    if runtime_mode(data_dir) != 'independent' and not get_codex_desktop_process_status().desktop_alive:
        print('Codex 未运行，未启动桥接。不会替你打开 Codex。')
        return 3
    lock = _acquire_autostart_lock(data_dir)
    if lock is None:
        print('已有启动/维护流程，请稍后再试。')
        return 3
    try:
        stopped = stop_cc_connect_service(data_dir)
        if not stopped.get('cc_connect_stopped'):
            print('旧 QQ 桥接未能停止，未启动重复实例。')
            return 1
        result = start_cc_connect_service(data_dir)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get('cc_connect_pid') and result.get('cc_connect_api_status') == 'ready' else 1
    finally:
        os.close(lock[0])


def _acquire_autostart_lock(data_dir: Path) -> tuple[int, Path] | None:
    paths = ensure_runtime_dirs(data_dir)
    return acquire_file_lock(paths['state'] / 'autostart.lock')


def _write_autostart_log(data_dir: Path, record: dict[str, object]) -> None:
    try:
        append_json_line(ensure_runtime_dirs(data_dir)["logs"] / "autostart.jsonl", {"at": time.time(), **record})
    except OSError:
        # Autostart must never turn a notification hook into a Codex failure.
        pass


def command_ensure_runtime(
    codex_home: Path,
    data_dir: Path,
    *,
    wait_seconds: float = 30.0,
    trigger_event: str = "unknown",
    transcript_path: str = "",
) -> int:
    try:
        lock = _acquire_autostart_lock(data_dir)
    except OSError as error:
        _write_autostart_log(
            data_dir,
            {"status": "failed", "reason": "lock_error", "trigger_event": trigger_event, "error_type": type(error).__name__},
        )
        return 1
    if lock is None:
        _write_autostart_log(
            data_dir,
            {"status": "lock_busy", "trigger_event": trigger_event},
        )
        return 0
    descriptor, _ = lock
    try:
        # Check UNDER the migration lock, including old helpers already queued.
        if runtime_mode(data_dir) != 'desktop':
            _write_autostart_log(data_dir, {'status': 'skipped', 'reason': 'independent_app_or_unknown_mode'})
            return 0
        detected = wait_for_codex_desktop(
            timeout_seconds=wait_seconds,
            transcript_path=transcript_path or None,
        )
        if not detected.get("desktop_detected"):
            _write_autostart_log(
                data_dir,
                {
                    "status": "skipped",
                    "reason": detected.get("reason", "desktop_not_detected"),
                    "trigger_event": trigger_event,
                    **detected,
                },
            )
            return 0
        # Retry transient startup failures, not permanent configuration errors.
        cc_connect: dict[str, object] = {}
        monitor: dict[str, object] = {}
        settings = DeliverySettings.load(data_dir / "settings.json")
        for startup_attempt in range(1, 4):
            cc_connect = start_cc_connect_service(data_dir) if settings.mode == "cc-connect" else {}
            monitor = start_background_monitor(Path(__file__), codex_home, data_dir)
            delivery_ready = settings.mode != "cc-connect" or bool(
                cc_connect.get("cc_connect_pid") and cc_connect.get("cc_connect_api_status") == "ready"
            )
            ready = bool(monitor.get("monitor_pid")) and delivery_ready
            _write_autostart_log(
                data_dir,
                {
                    "status": "ready" if ready else "failed",
                    "trigger_event": trigger_event,
                    "desktop_detected": detected.get("desktop_detected"),
                    "app_server_detected": detected.get("app_server_detected"),
                    "attempts": detected.get("attempts"),
                    "wait_seconds": detected.get("wait_seconds"),
                    "startup_attempt": startup_attempt,
                    "cc_connect_pid": cc_connect.get("cc_connect_pid"),
                    "cc_connect_reason": cc_connect.get("reason", "started"),
                    "monitor_pid": monitor.get("monitor_pid"),
                    "monitor_reason": monitor.get("reason", "started"),
                },
            )
            if ready:
                return 0
            if cc_connect.get("reason") in ("not_configured", "config_missing"):
                break
            if startup_attempt < 3:
                time.sleep(1.0)
                if not get_codex_desktop_process_status().desktop_alive:
                    break
        return 1
    except Exception as error:
        _write_autostart_log(
            data_dir,
            {
                "status": "failed",
                "reason": "startup_exception",
                "trigger_event": trigger_event,
                "error_type": type(error).__name__,
            },
        )
        return 1
    finally:
        os.close(descriptor)


def command_configure_delivery(args: argparse.Namespace) -> int:
    settings_path = args.data_dir / "settings.json"
    current = DeliverySettings.load(settings_path)
    updated = replace(
        current,
        mode=args.mode if args.mode is not None else current.mode,
        executable=args.executable if args.executable is not None else current.executable,
        project=args.project if args.project is not None else current.project,
        session=args.session if args.session is not None else current.session,
        data_dir=args.cc_data_dir if args.cc_data_dir is not None else current.data_dir,
        config_path=args.cc_config if args.cc_config is not None else current.config_path,
    )
    save_delivery_settings(settings_path, updated)
    print(
        json.dumps(
            {"settings_path": str(settings_path), "settings": updated.to_dict(), "restart_monitor_required": True},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Codex Desktop 本地状态监控")
    sub = parser.add_subparsers(dest="command", required=True)
    app = sub.add_parser('app', help='运行独立托盘应用，不启动或关闭 Codex')
    app.add_argument('--codex-home', type=Path, default=_default_codex_home())
    app.add_argument('--data-dir', type=Path, default=default_data_dir())
    app.add_argument('--headless', action='store_true', help=argparse.SUPPRESS)
    app.add_argument('--duration', type=float, default=None, help=argparse.SUPPRESS)
    app.add_argument('--brokered', action='store_true', help=argparse.SUPPRESS)
    sub.add_parser("test", help="运行本地测试")
    analyze = sub.add_parser("analyze-history", help="只读统计历史错误形态")
    analyze.add_argument("--codex-home", type=Path, default=_default_codex_home())
    analyze.add_argument("--include-examples", action="store_true", help="输出少量脱敏后的代表错误")
    replay = sub.add_parser("replay", help="只读回放一个 Codex JSONL 会话")
    replay.add_argument("path", type=Path)
    monitor = sub.add_parser("monitor", help="运行本地影子监控")
    monitor.add_argument("--codex-home", type=Path, default=_default_codex_home())
    monitor.add_argument("--data-dir", type=Path, default=default_data_dir())
    monitor.add_argument("--duration", type=float, default=None, help="测试运行秒数；省略则持续运行")
    monitor.add_argument("--exit-with-desktop", action="store_true", help="Codex 退出后监控也退出")
    monitor.add_argument("--manage-cc-connect", action="store_true", help=argparse.SUPPRESS)
    monitor.add_argument("--write-pid", action="store_true", help=argparse.SUPPRESS)
    start = sub.add_parser("start", help="后台启动监控，并在需要时打开 Codex Desktop")
    start.add_argument("--codex-home", type=Path, default=_default_codex_home())
    start.add_argument("--data-dir", type=Path, default=default_data_dir())
    reload_bridge = sub.add_parser('reload-bridge', help='只重载 QQ 桥接，不启动/关闭 Desktop，不运行模型任务')
    reload_bridge.add_argument('--data-dir', type=Path, default=default_data_dir())
    ensure = sub.add_parser("ensure-runtime", help=argparse.SUPPRESS)
    ensure.add_argument("--codex-home", type=Path, default=_default_codex_home())
    ensure.add_argument("--data-dir", type=Path, default=default_data_dir())
    ensure.add_argument("--wait-seconds", type=float, default=30.0, help=argparse.SUPPRESS)
    ensure.add_argument("--trigger-event", default="unknown", help=argparse.SUPPRESS)
    ensure.add_argument("--transcript-path", default="", help=argparse.SUPPRESS)
    stop = sub.add_parser("stop", help="停止监控，并正常请求关闭 Codex Desktop")
    stop.add_argument("--data-dir", type=Path, default=default_data_dir())
    stop.add_argument("--monitor-only", action="store_true", help="只停监控，不关闭 Codex Desktop")
    sub.add_parser("hook", help=argparse.SUPPRESS)
    install = sub.add_parser("install-hooks", help="安全合并用户级钩子")
    install.add_argument("--codex-home", type=Path, default=_default_codex_home())
    install.add_argument("--data-dir", type=Path, default=default_data_dir())
    uninstall = sub.add_parser("uninstall-hooks", help="仅移除本程序的钩子")
    uninstall.add_argument("--codex-home", type=Path, default=_default_codex_home())
    doctor = sub.add_parser("doctor", help="检查本地监控环境")
    doctor.add_argument("--codex-home", type=Path, default=_default_codex_home())
    doctor.add_argument("--data-dir", type=Path, default=default_data_dir())
    delivery = sub.add_parser("configure-delivery", help="配置影子模式或 CC Connect 发送模式")
    delivery.add_argument("--data-dir", type=Path, default=default_data_dir())
    delivery.add_argument("--mode", choices=("shadow", "cc-connect"), default=None)
    delivery.add_argument("--executable", default=None, help="cc-connect.exe 的路径或命令名")
    delivery.add_argument("--project", default=None, help="CC Connect 项目名；空字符串表示自动选择")
    delivery.add_argument("--session", default=None, help="CC Connect 会话键；空字符串表示自动选择")
    delivery.add_argument("--cc-data-dir", default=None, help="CC Connect 自己的数据目录")
    delivery.add_argument("--cc-config", default=None, help="CC Connect 配置文件路径")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == 'app':
        from resident_app import launch_app, run_app
        if args.brokered:
            return run_app(args.codex_home, args.data_dir, headless=args.headless, duration=args.duration)
        print(json.dumps(launch_app(args.codex_home, args.data_dir,
                                    headless=args.headless, duration=args.duration), ensure_ascii=False))
        return 0
    if args.command == "test":
        return command_test()
    if args.command == "analyze-history":
        return command_analyze_history(args.codex_home, include_examples=args.include_examples)
    if args.command == "replay":
        return command_replay(args.path)
    if args.command == "monitor":
        return command_monitor(
            args.codex_home,
            args.data_dir,
            args.duration,
            exit_with_desktop=args.exit_with_desktop,
            write_pid=args.write_pid,
            manage_cc_connect=args.manage_cc_connect,
        )
    if args.command == "start":
        return command_start(args.codex_home, args.data_dir)
    if args.command == 'reload-bridge':
        return command_reload_bridge(args.data_dir)
    if args.command == "ensure-runtime":
        return command_ensure_runtime(
            args.codex_home,
            args.data_dir,
            wait_seconds=args.wait_seconds,
            trigger_event=args.trigger_event,
            transcript_path=args.transcript_path,
        )
    if args.command == "stop":
        result = request_stop(args.data_dir, close_desktop=not args.monitor_only)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "hook":
        return hook_main()
    if args.command == "install-hooks":
        return command_install_hooks(args.codex_home, args.data_dir)
    if args.command == "uninstall-hooks":
        result = uninstall_hooks(args.codex_home)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "doctor":
        return command_doctor(args.codex_home, args.data_dir)
    if args.command == "configure-delivery":
        return command_configure_delivery(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
