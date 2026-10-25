from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from runtime_io import atomic_write_json, default_data_dir, ensure_runtime_dirs


MANAGED_MARKER = "codex-desktop-weixin"


def _quote_command_argument(value: str) -> str:
    if os.name == "nt":
        # Desktop uses the session's PowerShell, not Python's shell=True/cmd.
        # Literal strings also protect paths containing $, &, or apostrophes.
        return "'" + value.replace("'", "''") + "'"
    return shlex.quote(value)


def build_hook_command(cli_path: Path) -> str:
    # The hook runs from Codex Desktop's environment, which may not inherit the
    # user's interactive PATH.  Pin the interpreter that installed the hook so
    # a desktop-launched hook does not depend on the Python launcher being found.
    python_executable = Path(sys.executable).resolve()
    invocation = "& " if os.name == "nt" else ""
    return f"{invocation}{_quote_command_argument(str(python_executable))} -X utf8 {_quote_command_argument(str(cli_path.resolve()))} hook"


def _handler(command: str) -> dict[str, Any]:
    return {
        "type": "command",
        "command": command,
        "commandWindows": command,
        "timeout": 3,
    }


def managed_hook_groups(command: str) -> dict[str, list[dict[str, Any]]]:
    return {
        "SessionStart": [{"matcher": "startup|resume|clear|compact", "hooks": [_handler(command)]}],
        "SessionEnd": [{"matcher": "*", "hooks": [_handler(command)]}],
        "UserPromptSubmit": [{"hooks": [_handler(command)]}],
        "PermissionRequest": [{"matcher": "*", "hooks": [_handler(command)]}],
        "PreToolUse": [{"matcher": "^request_user_input$", "hooks": [_handler(command)]}],
        "PostToolUse": [{"matcher": "^request_user_input$", "hooks": [_handler(command)]}],
        "Stop": [{"hooks": [_handler(command)]}],
    }


def _is_managed_handler(handler: Any) -> bool:
    if not isinstance(handler, dict):
        return False
    command = " ".join(str(handler.get(key) or "") for key in ("command", "commandWindows", "command_windows"))
    return MANAGED_MARKER in command.replace("_", "-").lower()


def _is_managed_group(group: Any) -> bool:
    if not isinstance(group, dict):
        return False
    handlers = group.get("hooks")
    if not isinstance(handlers, list):
        return False
    return any(_is_managed_handler(handler) for handler in handlers)


def _unmanaged_groups(groups: list[Any]) -> list[Any]:
    preserved = []
    for group in groups:
        if not _is_managed_group(group):
            preserved.append(group)
            continue
        # Users can place multiple handlers in one matcher group. Remove only
        # our handlers, keeping their other hooks and group metadata intact.
        handlers = [handler for handler in group["hooks"] if not _is_managed_handler(handler)]
        if handlers:
            preserved.append(dict(group, hooks=handlers))
    return preserved


def merge_hooks_document(existing: dict[str, Any] | None, command: str) -> dict[str, Any]:
    document: dict[str, Any] = dict(existing or {})
    hooks = document.get("hooks")
    if hooks is None:
        hooks = {}
    if not isinstance(hooks, dict):
        raise ValueError("hooks.json 的 hooks 字段不是对象，无法安全合并。")
    merged_hooks: dict[str, Any] = dict(hooks)
    for event_name, additions in managed_hook_groups(command).items():
        current = merged_hooks.get(event_name) or []
        if not isinstance(current, list):
            raise ValueError(f"hooks.json 的 {event_name} 字段不是数组，无法安全合并。")
        preserved = _unmanaged_groups(current)
        merged_hooks[event_name] = preserved + additions
    document["hooks"] = merged_hooks
    return document


def remove_managed_hooks(existing: dict[str, Any]) -> dict[str, Any]:
    document = dict(existing)
    hooks = document.get("hooks")
    if not isinstance(hooks, dict):
        return document
    cleaned: dict[str, Any] = {}
    for event_name, groups in hooks.items():
        if not isinstance(groups, list):
            cleaned[event_name] = groups
            continue
        remaining = _unmanaged_groups(groups)
        if remaining:
            cleaned[event_name] = remaining
    document["hooks"] = cleaned
    return document


def enable_hooks_feature(config_text: str) -> str:
    # Keep special floats comparable for the preservation check (nan != nan).
    expected = tomllib.loads(config_text, parse_float=lambda value: (float, value))
    features = expected.setdefault("features", {})
    if not isinstance(features, dict):
        raise ValueError("config.toml 的 features 字段不是表，无法安全启用 hooks。")
    if features.get("hooks") is True:
        return config_text
    features["hooks"] = True
    newline = "\r\n" if "\r\n" in config_text else "\n"
    # TOML permits Unicode line separators inside strings; only LF/CRLF end
    # physical lines. str.splitlines() would split and rewrite those values.
    lines = config_text.replace("\r\n", "\n").split("\n") if config_text else []
    if config_text.endswith("\n"):
        lines.pop()

    def complete_statement(index: int) -> bool:
        # A header-looking line can be literal text inside a multiline value.
        # A prefix ending at a real header/boolean assignment parses completely.
        try:
            tomllib.loads("\n".join(lines[:index + 1]))
        except tomllib.TOMLDecodeError:
            return False
        return True

    def checked_result(result: list[str]) -> str:
        updated = newline.join(result) + (newline if config_text.endswith(("\n", "\r")) else "")
        if tomllib.loads(updated, parse_float=lambda value: (float, value)) != expected:
            raise ValueError("无法只修改 features.hooks，原配置已保留；请检查 config.toml。")
        return updated

    section_start: int | None = None
    section_end = len(lines)
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("["):
            continue
        try:
            header = tomllib.loads(stripped)
        except tomllib.TOMLDecodeError:
            continue
        if not complete_statement(index):
            continue
        if header == {"features": {}}:
            if section_start is not None:
                raise ValueError("config.toml 中存在重复的 [features]。")
            section_start = index
            continue
        if section_start is not None:
            section_end = index
            break

    if section_start is None:
        result = list(lines)
        if result and result[-1].strip():
            result.append("")
        result.extend(["[features]", "hooks = true"])
        return checked_result(result)

    matches: list[int] = []
    for index in range(section_start + 1, section_end):
        if "=" not in lines[index] or lines[index].lstrip().startswith("#"):
            continue
        left = lines[index].split("=", 1)[0].strip()
        try:
            key = tomllib.loads(left + " = true")
        except tomllib.TOMLDecodeError:
            continue
        if key == {"hooks": True} and complete_statement(index):
            matches.append(index)
    if len(matches) > 1:
        raise ValueError("config.toml 的 [features] 中存在重复 hooks 设置。")
    if matches:
        index = matches[0]
        key, value = lines[index].split("=", 1)
        body, marker, comment = value.partition("#")
        leading = body[: len(body) - len(body.lstrip())]
        trailing = body[len(body.rstrip()):]
        lines[index] = key + "=" + leading + "true" + trailing + marker + comment
    else:
        lines.insert(section_end, "hooks = true")
    return checked_result(lines)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hooks_feature_enabled(config_text: str) -> bool:
    try:
        parsed = tomllib.loads(config_text)
    except tomllib.TOMLDecodeError:
        return False
    features = parsed.get("features")
    return isinstance(features, dict) and features.get("hooks") is True


def _backup(path: Path, backup_dir: Path) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "existed": False, "sha256": None, "backup": None}
    data = path.read_bytes()
    destination = backup_dir / path.name
    destination.write_bytes(data)
    return {"path": str(path), "existed": True, "sha256": _sha256(data), "backup": str(destination)}


def install_hooks(codex_home: Path, cli_path: Path, *, data_dir: Path | None = None) -> dict[str, Any]:
    codex_home = codex_home.resolve()
    config_path = codex_home / "config.toml"
    hooks_path = codex_home / "hooks.json"
    if not config_path.exists():
        raise FileNotFoundError(config_path)
    command = build_hook_command(cli_path)
    existing_hooks: dict[str, Any] | None = None
    if hooks_path.exists():
        existing_hooks = json.loads(hooks_path.read_text(encoding="utf-8-sig"))
        if not isinstance(existing_hooks, dict):
            raise ValueError("现有 hooks.json 不是 JSON 对象。")
    merged = merge_hooks_document(existing_hooks, command)
    original_config = config_path.read_text(encoding="utf-8-sig")
    updated_config = enable_hooks_feature(original_config)

    paths = ensure_runtime_dirs(data_dir or default_data_dir())
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup_dir = paths["root"] / "install-backups" / stamp
    backup_dir.mkdir(parents=True, exist_ok=False)
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": [_backup(config_path, backup_dir), _backup(hooks_path, backup_dir)],
    }
    atomic_write_json(backup_dir / "manifest.json", manifest)

    try:
        atomic_write_json(hooks_path, merged)
        config_bytes = updated_config.encode("utf-8")
        temporary = config_path.with_name(f".{config_path.name}.{os.getpid()}.tmp")
        try:
            with temporary.open("wb") as stream:
                stream.write(config_bytes)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, config_path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        json.loads(hooks_path.read_text(encoding="utf-8"))
        if not hooks_feature_enabled(config_path.read_text(encoding="utf-8-sig")):
            raise RuntimeError("写入后未能验证 features.hooks。")
    except Exception:
        for item in manifest["files"]:
            target = Path(item["path"])
            if item["existed"]:
                shutil.copyfile(item["backup"], target)
            else:
                try:
                    target.unlink()
                except FileNotFoundError:
                    pass
        raise

    return {"hooks_path": str(hooks_path), "config_path": str(config_path), "backup_dir": str(backup_dir), "command": command}


def uninstall_hooks(codex_home: Path) -> dict[str, Any]:
    hooks_path = codex_home.resolve() / "hooks.json"
    if not hooks_path.exists():
        return {"changed": False, "hooks_path": str(hooks_path)}
    document = json.loads(hooks_path.read_text(encoding="utf-8-sig"))
    if not isinstance(document, dict):
        raise ValueError("现有 hooks.json 不是 JSON 对象。")
    cleaned = remove_managed_hooks(document)
    changed = cleaned != document
    if changed:
        atomic_write_json(hooks_path, cleaned)
    return {"changed": changed, "hooks_path": str(hooks_path)}


def hook_install_status(codex_home: Path) -> dict[str, Any]:
    config_path = codex_home / "config.toml"
    hooks_path = codex_home / "hooks.json"
    enabled = False
    if config_path.exists():
        text = config_path.read_text(encoding="utf-8-sig")
        enabled = hooks_feature_enabled(text)
    installed = False
    trusted_state_present = False
    if hooks_path.exists():
        document = json.loads(hooks_path.read_text(encoding="utf-8-sig"))
        hooks = document.get("hooks") if isinstance(document, dict) else None
        if isinstance(hooks, dict):
            installed = any(_is_managed_group(group) for groups in hooks.values() if isinstance(groups, list) for group in groups)
    return {"features_hooks_enabled": enabled, "managed_hooks_installed": installed, "hooks_path": str(hooks_path)}
