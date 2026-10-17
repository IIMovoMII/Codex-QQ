"""Bounded read-only catalog for the verified local state_5 schema."""

from __future__ import annotations

import json
import sqlite3
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from control_actions import normalize_saved_queue


class LocalCatalog:
    def __init__(self, codex_root: Path) -> None:
        self.path = codex_root / "state_5.sqlite"
        self.global_path = codex_root / ".codex-global-state.json"
        self.queue_path = codex_root / 'queue_1.sqlite'

    def _desktop_assignments(self) -> tuple[dict, dict, dict, set]:
        if not self.global_path.exists():
            return {}, {}, {}, set()
        value = json.loads(self.global_path.read_text(encoding="utf-8"))
        projects = value.get("local-projects") or {}
        assignments = value.get("thread-project-assignments") or {}
        hosts = value.get("thread-project-membership-host-ids") or {}
        mapping = (value.get("app-server-project-id-by-legacy-project-id-by-host") or {}).get("local:" + str(self.path.parent), {})
        if not all(isinstance(x, dict) for x in (projects, assignments, hosts, mapping)):
            raise ValueError("Unsupported Desktop project metadata shape")
        assignments = {k: v for k, v in assignments.items() if isinstance(v, dict) and v.get("projectKind") == "local" and hosts.get(k, "local") == "local"}
        return projects, assignments, mapping, set(value.get("projectless-thread-ids") or [])

    def _query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.4)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            return [dict(row) for row in db.execute(sql, params)]
        finally:
            db.close()

    def saved_queue(self, thread_id: str) -> list[dict]:
        """Desktop local saved queue only, never claims server-queue completeness."""
        if not 0 < self.global_path.stat().st_size <= 8 * 1024 * 1024:
            raise ValueError('desktop-state-too-large')
        value = json.loads(self.global_path.read_text(encoding='utf-8'))
        queues = value.get('queued-follow-ups')
        if not isinstance(queues, dict):
            raise ValueError('local-queue-unavailable')
        return normalize_saved_queue(queues.get(thread_id, []))

    def projects(self, *, limit: int = 50) -> list[dict[str, Any]]:
        projects, _, mapping, _ = self._desktop_assignments()
        if projects:
            return [{"id": p["id"], "backend_id": mapping.get(p["id"], p["id"]), "name": p["name"], "cwd": (p.get("rootPaths") or [""])[0]} for p in projects.values() if isinstance(p, dict) and p.get("id") and p.get("name")][:limit]
        # A project can have several roots, and several projects can share a root.
        return self._query("""SELECT p.id,p.name,
            (SELECT path FROM project_roots WHERE project_id=p.id ORDER BY position LIMIT 1) AS cwd
            FROM projects p ORDER BY p.position,p.updated_at_ms DESC LIMIT ?""", (min(limit, 50),))

    def threads(self, *, count: int = 10, search: str = "", project_id: str | None = None) -> list[dict[str, Any]]:
        projects, assignments, mapping, projectless = self._desktop_assignments()
        clauses = ["t.archived=0", "t.source IN ('vscode','app','desktop')", "t.agent_role IS NULL"]
        params: list[Any] = []
        if search:
            clauses.append("instr(lower(coalesce(nullif(t.name,''),t.title)),lower(?))>0")
            params.append(search)
        if project_id is not None:
            assigned = [t for t, a in assignments.items() if a.get("projectId") == project_id]
            candidates = [project_id, mapping.get(project_id, project_id)]
            clause = "t.project_id IN (?,?)"
            params.extend(candidates)
            if assigned:
                clause += " OR t.id IN (" + ",".join("?" for _ in assigned) + ")"
                params.extend(assigned)
            clauses.append("(" + clause + ")")
            # Explicit Desktop assignments override stale backend project IDs.
            # Exclude overrides before LIMIT, otherwise newer moved/projectless
            # rows hide valid older results when a small count is requested.
            excluded = (set(assignments) | projectless) - set(assigned)
            if excluded:
                clauses.append("t.id NOT IN (" + ",".join("?" for _ in excluded) + ")")
                params.extend(excluded)
        params.append(min(max(count, 1), 50))
        rows = self._query("""SELECT t.id,coalesce(nullif(t.name,''),nullif(t.title,''),'未命名对话') AS name,
            t.project_id,p.name AS project_name,t.cwd,t.model,t.reasoning_effort,
            t.approval_mode,t.sandbox_policy,t.updated_at_ms
            FROM threads t LEFT JOIN projects p ON t.project_id=p.id WHERE """ +
            " AND ".join(clauses) + " ORDER BY coalesce(t.recency_at_ms,t.updated_at_ms) DESC LIMIT ?", tuple(params))
        for row in rows:
            assignment = assignments.get(row["id"])
            if assignment and assignment.get("projectId") in projects:
                row["project_id"] = assignment["projectId"]
                row["project_name"] = projects[row["project_id"]]["name"]
            elif row["id"] in projectless:
                row["project_id"], row["project_name"] = None, None
        return [row for row in rows if project_id is None or row.get("project_id") in (project_id, mapping.get(project_id))]

    def thread(self, thread_id: str) -> dict[str, Any] | None:
        rows = self._query("""SELECT t.id,coalesce(nullif(t.name,''),nullif(t.title,''),'未命名对话') AS name,
            t.project_id,p.name AS project_name,t.cwd,t.model,t.reasoning_effort,
            t.approval_mode,t.sandbox_policy,t.source,t.agent_role
            FROM threads t LEFT JOIN projects p ON t.project_id=p.id
            WHERE t.id=? AND t.archived=0 AND t.source IN ('vscode','app','desktop')
            AND t.agent_role IS NULL LIMIT 1""", (thread_id,))
        if not rows:
            return None
        row = rows[0]
        projects, assignments, _, projectless = self._desktop_assignments()
        assignment = assignments.get(thread_id)
        if assignment and assignment.get("projectId") in projects:
            row["project_id"] = assignment["projectId"]
            row["project_name"] = projects[row["project_id"]]["name"]
        elif thread_id in projectless:
            row["project_id"], row["project_name"] = None, None
        return row

    def models(self) -> dict[str, Any]:
        """Read the configured catalog, never substitute a hard-coded model list.

        This is discovery metadata, not an entitlement or inference guarantee.
        Cache-only installations must have a recent native cache.
        """
        config_path = self.path.parent / "config.toml"
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        configured = config.get("model_catalog_json")
        profile = config.get("profile")
        if profile:
            if not isinstance(profile, str) or Path(profile).name != profile or "/" in profile or "\\" in profile:
                raise ValueError("unsupported-model-profile")
            profile_path = self.path.parent / (profile + ".config.toml")
            if not profile_path.exists():
                raise ValueError("model-profile-unavailable")
            extra = tomllib.loads(profile_path.read_text(encoding="utf-8"))
            configured = extra.get("model_catalog_json", configured)
        path = Path(configured) if isinstance(configured, str) and configured else self.path.parent / "models_cache.json"
        if not path.is_absolute():
            raise ValueError("relative-model-catalog")
        if not 0 < path.stat().st_size <= 8 * 1024 * 1024:
            raise ValueError("model-catalog-too-large")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not configured:
            stamp = datetime.fromisoformat(value["fetched_at"].replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - stamp).total_seconds()
            if not 0 <= age <= 86400:
                raise ValueError("stale-model-catalog")
        raw = value.get("models")
        if not isinstance(raw, list) or not raw or len(raw) > 200:
            raise ValueError("unsupported-model-catalog")
        options = []
        for row in raw:
            if not isinstance(row, dict) or not isinstance(row.get("supported_reasoning_levels", []), list):
                raise ValueError("invalid-model-option")
            if row.get("visibility") == "hide":
                continue
            model = row.get("slug")
            efforts = [x.get("effort") for x in row.get("supported_reasoning_levels", []) if isinstance(x, dict)]
            if not isinstance(model, str) or not model or not efforts or not all(isinstance(x, str) and x for x in efforts):
                raise ValueError("invalid-model-option")
            options.append({"model": model, "name": str(row.get("display_name") or model), "efforts": efforts})
        if not options or len({x['model'] for x in options}) != len(options):
            raise ValueError("ambiguous-model-catalog")
        return {"source": "已配置的模型目录" if configured else "近期原生模型缓存", "options": options}

    @staticmethod
    def label(row: dict[str, Any]) -> str:
        project = str(row.get("project_name") or "无项目").replace("\n", " ")[:40]
        title = str(row.get("name") or "未命名对话").replace("\n", " ")[:80]
        return f"［{project}］{title}"
