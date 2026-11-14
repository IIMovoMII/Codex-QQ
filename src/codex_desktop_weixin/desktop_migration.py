"""Move only the existing gateway and ACP executable paths to packaged assets."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import tomllib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from runtime_io import acquire_file_lock, atomic_write_json, ensure_runtime_dirs


class MigrationError(ValueError):
    pass


def _same_path(left: str | Path, right: str | Path) -> bool:
    return os.path.normcase(os.path.normpath(str(Path(left).resolve()))) == os.path.normcase(
        os.path.normpath(str(Path(right).resolve())))


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_inputs(data_dir: Path, gateway: Path, python: Path, backend: Path) -> dict[str, Any] | None:
    settings_path = data_dir / "settings.json"
    if not settings_path.is_file():
        return None
    try:
        settings_bytes = settings_path.read_bytes()
        settings = json.loads(settings_bytes.decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise MigrationError("settings_invalid") from None
    if not isinstance(settings, dict) or settings.get("mode") != "cc-connect":
        return None
    config_value = settings.get("config_path")
    if not isinstance(config_value, str) or not config_value:
        raise MigrationError("config_path_invalid")
    config_path = Path(config_value)
    try:
        config_bytes = config_path.read_bytes()
        config_text = config_bytes.decode("utf-8-sig")
        document = tomllib.loads(config_text)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise MigrationError("gateway_config_invalid") from None
    if not gateway.is_file() or not python.is_file() or not backend.is_dir():
        raise MigrationError("packaged_runtime_missing")
    project_name = settings.get("project")
    projects = document.get("projects")
    if not isinstance(project_name, str) or not isinstance(projects, list):
        raise MigrationError("gateway_project_missing")
    matches = [index for index, item in enumerate(projects)
               if isinstance(item, dict) and item.get("name") == project_name]
    if len(matches) != 1:
        raise MigrationError("gateway_project_ambiguous")
    index = matches[0]
    options = projects[index].get("agent", {}).get("options", {})
    if not isinstance(options, dict):
        raise MigrationError("gateway_agent_options_invalid")
    command = options.get("cmd")
    work_dir = options.get("work_dir")
    if (not isinstance(command, list) or len(command) < 2
            or any(not isinstance(item, str) for item in command)
            or not isinstance(work_dir, str)):
        raise MigrationError("gateway_agent_command_invalid")
    source_root = Path(work_dir)
    old_script = source_root / "src" / "codex_desktop_weixin" / "qq_desktop_bridge.py"
    new_script = backend / "codex_desktop_weixin" / "qq_desktop_bridge.py"
    current_gateway = settings.get("executable")
    if not isinstance(current_gateway, str) or not current_gateway:
        raise MigrationError("gateway_executable_invalid")
    old_gateway = source_root / "vendor" / "cc-connect" / "v1.5.0-desktop-control" / "bin" / "cc-connect-desktop-control.exe"
    if not (_same_path(current_gateway, old_gateway) or _same_path(current_gateway, gateway)):
        raise MigrationError("gateway_executable_path_unrecognized")
    target_settings = dict(settings)
    target_settings["executable"] = str(gateway.resolve())
    target_document = copy.deepcopy(document)
    target_options = target_document["projects"][index]["agent"]["options"]

    if _same_path(command[-1], new_script) and _same_path(work_dir, backend):
        target_command = list(command)
    elif _same_path(command[-1], old_script) and _same_path(work_dir, source_root):
        target_command = [str(python.resolve()), *command[1:-1], str(new_script.resolve())]
    else:
        raise MigrationError("gateway_source_path_unrecognized")

    if command[1:3] != ["-X", "utf8"] or len(command) != 4:
        raise MigrationError("gateway_python_command_unrecognized")
    target_options["cmd"] = target_command
    target_options["work_dir"] = str(backend.resolve())
    if settings.get("executable") == target_settings["executable"] and document == target_document:
        return None
    updated_text = _project_assignments(
        config_text, project_name,
        {"cmd": _toml_value(target_command), "work_dir": _toml_value(str(backend.resolve()))},
    )
    try:
        updated_document = tomllib.loads(updated_text)
    except tomllib.TOMLDecodeError:
        raise MigrationError("gateway_config_render_invalid") from None
    if updated_document != target_document:
        raise MigrationError("gateway_config_semantics_changed")
    settings_after = (json.dumps(target_settings, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    return {
        "settings_path": settings_path,
        "settings_before": settings_bytes,
        "settings_after": settings_after,
        "config_path": config_path,
        "config_before": config_bytes,
        "config_after": updated_text.encode("utf-8"),
    }


def _toml_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _project_assignments(text: str, project_name: str, updates: dict[str, str]) -> str:
    lines = text.splitlines(keepends=True)
    active_project = -1
    project_index = -1
    table: tuple[str, ...] = ()
    found = {key: 0 for key in updates}
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped == "[[projects]]":
            project_index += 1
            active_project = project_index
            table = ("projects",)
            output.append(line)
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            header = stripped.strip("[]")
            table = tuple(part.strip().strip('"') for part in header.split("."))
            output.append(line)
            continue
        if active_project >= 0 and table == ("projects",) and stripped.startswith("name") and "=" in stripped:
            try:
                current_name = tomllib.loads("value = " + stripped.split("=", 1)[1]).get("value")
            except tomllib.TOMLDecodeError:
                raise MigrationError("gateway_project_name_invalid") from None
            if current_name != project_name:
                active_project = -1
            output.append(line)
            continue
        if active_project >= 0 and table == ("projects", "agent", "options"):
            match = re.match(r"^(\s*)([A-Za-z0-9_-]+)(\s*=)(.*?)(\r?\n)?$", line)
            if match and match.group(2) in updates:
                key = match.group(2)
                found[key] += 1
                if found[key] > 1 or "#" in match.group(4):
                    raise MigrationError("gateway_config_assignment_ambiguous")
                output.append(f"{match.group(1)}{key}{match.group(3)} {updates[key]}{match.group(5) or ''}")
                continue
        output.append(line)
    if any(count != 1 for count in found.values()):
        raise MigrationError("gateway_config_assignment_missing")
    return "".join(output)


def migration_required(data_dir: Path, gateway: Path, python: Path, backend: Path) -> bool:
    return _read_inputs(data_dir.resolve(), gateway.resolve(), python.resolve(), backend.resolve()) is not None


def legacy_launcher_path(data_dir: Path) -> Path | None:
    settings_path = data_dir.resolve() / "settings.json"
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8-sig"))
        if not isinstance(settings, dict) or settings.get("mode") != "cc-connect":
            return None
        config_path = Path(settings.get("config_path", ""))
        document = tomllib.loads(config_path.read_text(encoding="utf-8-sig"))
        project_name = settings.get("project")
        projects = document.get("projects")
        if not isinstance(project_name, str) or not isinstance(projects, list):
            return None
        matches = [item for item in projects
                   if isinstance(item, dict) and item.get("name") == project_name]
        if len(matches) != 1:
            return None
        options = matches[0].get("agent", {}).get("options", {})
        command = options.get("cmd") if isinstance(options, dict) else None
        work_dir = options.get("work_dir") if isinstance(options, dict) else None
        if (not isinstance(command, list) or len(command) != 4
                or not all(isinstance(value, str) for value in command)
                or not isinstance(work_dir, str)):
            return None
        source_root = Path(work_dir)
        expected_script = source_root / "src" / "codex_desktop_weixin" / "qq_desktop_bridge.py"
        if (_same_path(command[-1], expected_script) and command[1:3] == ["-X", "utf8"]):
            return source_root / "bin" / "CodexQQ助手.exe"
    except (OSError, ValueError, TypeError, tomllib.TOMLDecodeError):
        return None
    return None


def migrate_paths(data_dir: Path, gateway: Path, python: Path, backend: Path) -> dict[str, Any]:
    data_dir = data_dir.resolve()
    paths = ensure_runtime_dirs(data_dir)
    lock = acquire_file_lock(paths["state"] / "install-migration.lock")
    if lock is None:
        raise MigrationError("migration_busy")
    try:
        recover_paths(data_dir)
        plan = _read_inputs(data_dir, gateway.resolve(), python.resolve(), backend.resolve())
        if plan is None:
            return {"changed": False, "backupDir": ""}
        transaction = uuid.uuid4().hex
        backup_dir = paths["root"] / "install-backups" / f"desktop-app-{transaction}"
        backup_dir.mkdir(parents=True, exist_ok=False)
        files = []
        for name, before, after in (
            (plan["settings_path"].name, plan["settings_before"], plan["settings_after"]),
            (plan["config_path"].name, plan["config_before"], plan["config_after"]),
        ):
            backup = backup_dir / name
            with backup.open("xb") as stream:
                stream.write(before)
                stream.flush()
                os.fsync(stream.fileno())
            files.append({"name": name, "sha256_before": _sha(before), "sha256_after": _sha(after)})
        atomic_write_json(backup_dir / "manifest.json", {
            "version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "files": files,
        })
        settings_stage = plan["settings_path"].with_name(f".{plan['settings_path'].name}.{transaction}.tmp")
        config_stage = plan["config_path"].with_name(f".{plan['config_path'].name}.{transaction}.tmp")
        stages = ((settings_stage, plan["settings_after"]), (config_stage, plan["config_after"]))
        marker = paths["state"] / f"desktop-migration-{transaction}.json"
        try:
            for stage, payload in stages:
                with stage.open("xb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
            atomic_write_json(marker, {
                "version": 1,
                "backup_dir": str(backup_dir),
                "settings_path": str(plan["settings_path"]),
                "settings_sha256": _sha(plan["settings_after"]),
                "config_path": str(plan["config_path"]),
                "config_sha256": _sha(plan["config_after"]),
                "settings_stage": str(settings_stage),
                "config_stage": str(config_stage),
            })
            os.replace(config_stage, plan["config_path"])
            os.replace(settings_stage, plan["settings_path"])
            if (_sha(plan["config_path"].read_bytes()) != _sha(plan["config_after"])
                    or _sha(plan["settings_path"].read_bytes()) != _sha(plan["settings_after"])):
                raise MigrationError("migration_readback_failed")
            marker.unlink()
        except BaseException:
            for stage, _ in stages:
                stage.unlink(missing_ok=True)
            _restore_pair(plan, backup_dir)
            marker.unlink(missing_ok=True)
            raise
        return {"changed": True, "backupDir": str(backup_dir)}
    finally:
        os.close(lock[0])


def recover_paths(data_dir: Path) -> None:
    """Roll a torn pair back from its immutable backup before planning again."""
    for marker in (data_dir / 'state').glob('desktop-migration-*.json'):
        match = re.fullmatch(r'desktop-migration-([0-9a-f]{32})\.json', marker.name)
        if not match:
            continue
        record = json.loads(marker.read_text(encoding='utf-8'))
        backup = data_dir / 'install-backups' / ('desktop-app-' + match[1])
        if record.get('version') != 1 or not _same_path(record['backup_dir'], backup):
            raise MigrationError('migration_marker_invalid')
        settings_before = (backup / 'settings.json').read_bytes()
        config_path = Path(json.loads(settings_before.decode('utf-8-sig'))['config_path'])
        if (not _same_path(record['settings_path'], data_dir/'settings.json')
                or not _same_path(record['config_path'], config_path)):
            raise MigrationError('migration_marker_path_changed')
        manifest = json.loads((backup/'manifest.json').read_text())
        hashes = {item['name']: item for item in manifest['files']}
        for key, target, before in (('settings', data_dir/'settings.json', settings_before),
                                    ('config', config_path, (backup/config_path.name).read_bytes())):
            item = hashes[target.name]
            if _sha(before) != item['sha256_before'] or record[key+'_sha256'] != item['sha256_after']:
                raise MigrationError('migration_backup_invalid')
            current = _sha(target.read_bytes())
            if current not in (item['sha256_before'], item['sha256_after']):
                raise MigrationError('migration_intervening_edit_preserved')
        for target in (config_path, data_dir/'settings.json'):
            before = (backup/target.name).read_bytes()
            if target.read_bytes() != before:
                stage = target.with_name('.'+target.name+'.recover-'+match[1]+'.tmp')
                with stage.open('xb') as stream:
                    stream.write(before); stream.flush(); os.fsync(stream.fileno())
                os.replace(stage, target)
        for key, target in (('config', config_path), ('settings', data_dir/'settings.json')):
            expected = target.with_name(f'.{target.name}.{match[1]}.tmp')
            if not _same_path(record[key+'_stage'], expected):
                raise MigrationError('migration_stage_path_changed')
            expected.unlink(missing_ok=True)
        marker.unlink()


def _restore_pair(plan: dict[str, Any], backup_dir: Path) -> None:
    for path_key, before_key, after_key in (
        ("config_path", "config_before", "config_after"),
        ("settings_path", "settings_before", "settings_after"),
    ):
        target = plan[path_key]
        expected_before = _sha(plan[before_key])
        expected_after = _sha(plan[after_key])
        try:
            current_hash = _sha(target.read_bytes())
        except OSError:
            current_hash = ""
        if current_hash == expected_before:
            continue
        if current_hash != expected_after:
            raise MigrationError("migration_recovery_required")
        backup = backup_dir / target.name
        if _sha(backup.read_bytes()) != expected_before:
            raise MigrationError("migration_backup_invalid")
        restore = target.with_name(f".{target.name}.restore-{uuid.uuid4().hex}.tmp")
        try:
            with restore.open("xb") as stream:
                stream.write(backup.read_bytes())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(restore, target)
        finally:
            restore.unlink(missing_ok=True)
