from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "codex_desktop_weixin"))
from control_catalog import LocalCatalog


class CatalogTests(unittest.TestCase):
    def test_saved_queue_is_bounded_readonly_and_not_all_history(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = {'queued-follow-ups': {'target': [{'id': 'stable-id', 'text': '中文😀', 'submission': {'hostId': 'local', 'status': 'queued'}}], 'other': []}}
            path = root / '.codex-global-state.json'
            path.write_text(json.dumps(state), encoding='utf-8')
            original = path.read_bytes()
            catalog = LocalCatalog(root)
            rows = catalog.saved_queue('target')
            self.assertEqual(rows[0]['id'], 'stable-id')
            self.assertEqual(rows[0]['summary'], '中文😀')
            self.assertEqual(catalog.saved_queue('other'), [])
            self.assertEqual(path.read_bytes(), original)

    def test_saved_queue_rows_preserve_native_fields_and_reject_cross_host_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / '.codex-global-state.json'
            row = {'id': 'stable-id', 'text': '中文😀', 'createdAt': 17,
                   'submission': {'hostId': 'local', 'status': 'queued'},
                   'futureField': {'kept': True}}
            path.write_text(json.dumps({'queued-follow-ups': {'target': [row]}}, ensure_ascii=False), encoding='utf-8')
            catalog = LocalCatalog(root)
            returned = catalog.saved_queue_rows('target')
            self.assertEqual(returned, [row])
            returned[0]['futureField']['kept'] = False
            self.assertEqual(catalog.saved_queue_rows('target')[0]['futureField']['kept'], True)
            path.write_text(json.dumps({'queued-follow-ups': {'target': [
                {**row, 'submission': {'hostId': 'remote', 'status': 'queued'}}]}}, ensure_ascii=False), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'cross-host-queue'):
                catalog.saved_queue_rows('target')

    def test_models_use_explicit_catalog_and_reject_duplicate_identifiers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model_file = root / 'models.json'
            (root / 'config.toml').write_text('model_catalog_json = ' + json.dumps(str(model_file)) + '\n', encoding='utf-8')
            model = {'slug': 'not-hardcoded', 'display_name': '本机模型', 'supported_reasoning_levels': [{'effort': 'high'}, {'effort': 'max'}]}
            hidden = {**model, 'slug': 'hidden', 'visibility': 'hide'}
            model_file.write_text(json.dumps({'models': [model, hidden]}), encoding='utf-8')
            catalog = LocalCatalog(root)
            self.assertEqual(catalog.models()['options'], [{'model': 'not-hardcoded', 'name': '本机模型', 'efforts': ['high', 'max']}])
            model_file.write_text(json.dumps({'models': [model, model]}), encoding='utf-8')
            with self.assertRaises(ValueError):
                catalog.models()

    def test_explicit_projects_with_shared_directory_are_not_guessed_from_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = sqlite3.connect(root / "state_5.sqlite")
            db.executescript("""CREATE TABLE projects(id TEXT,name TEXT,position INT,updated_at_ms INT);
                CREATE TABLE project_roots(project_id TEXT,position INT,path TEXT);
                CREATE TABLE threads(id TEXT,name TEXT,title TEXT,project_id TEXT,cwd TEXT,model TEXT,
                reasoning_effort TEXT,approval_mode TEXT,sandbox_policy TEXT,source TEXT,agent_role TEXT,
                archived INT,updated_at_ms INT,recency_at_ms INT);""")
            for i, t in enumerate(("thread-a", "thread-b", "unassigned")):
                db.execute("INSERT INTO threads VALUES(?,?,?,NULL,'D:/shared','model','max','never','{}','vscode',NULL,0,?,?)", (t, "同名对话", "旧标题", 10+i, 10+i))
            db.commit()
            db.close()
            (root / ".codex-global-state.json").write_text(json.dumps({
                "local-projects": {"a": {"id": "a", "name": "项目甲", "rootPaths": ["D:/shared"]}, "b": {"id": "b", "name": "项目乙", "rootPaths": ["D:/shared"]}},
                "thread-project-assignments": {"thread-a": {"projectKind": "local", "projectId": "a"}, "thread-b": {"projectKind": "local", "projectId": "b"}},
                "thread-project-membership-host-ids": {"thread-a": "local", "thread-b": "local"},
            }), encoding="utf-8")
            catalog = LocalCatalog(root)
            self.assertEqual(catalog.thread("thread-a")["project_name"], "项目甲")
            self.assertEqual(catalog.thread("thread-b")["project_name"], "项目乙")
            self.assertIsNone(catalog.thread("unassigned")["project_name"])
            self.assertEqual([r["id"] for r in catalog.threads(project_id="a")], ["thread-a"])
            self.assertEqual({r["id"] for r in catalog.projects()}, {"a", "b"})

    def test_effective_project_filter_is_applied_before_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with sqlite3.connect(root / "state_5.sqlite") as db:
                db.executescript("""CREATE TABLE projects(id TEXT,name TEXT,position INT,updated_at_ms INT);
                    CREATE TABLE project_roots(project_id TEXT,position INT,path TEXT);
                    CREATE TABLE threads(id TEXT,name TEXT,title TEXT,project_id TEXT,cwd TEXT,model TEXT,
                    reasoning_effort TEXT,approval_mode TEXT,sandbox_policy TEXT,source TEXT,agent_role TEXT,
                    archived INT,updated_at_ms INT,recency_at_ms INT);""")
                for i, t in enumerate(("valid", "moved", "projectless")):
                    db.execute("INSERT INTO threads VALUES(?,?,?,'backend-a','D:/shared','model','max','never','{}','vscode',NULL,0,?,?)", (t, t, t, i, i))
            db.close()
            (root / ".codex-global-state.json").write_text(json.dumps({
                "local-projects": {"a": {"id": "a", "name": "甲"}, "b": {"id": "b", "name": "乙"}},
                "thread-project-assignments": {"moved": {"projectKind": "local", "projectId": "b"}},
                "projectless-thread-ids": ["projectless"],
                "app-server-project-id-by-legacy-project-id-by-host": {"local:" + str(root): {"a": "backend-a"}},
            }), encoding="utf-8")
            catalog = LocalCatalog(root)
            self.assertEqual([r["id"] for r in catalog.threads(count=1, project_id="a")], ["valid"])
            self.assertEqual([r["id"] for r in catalog.threads(count=1, project_id="b")], ["moved"])
            self.assertIsNone(catalog.thread("projectless")["project_id"])


if __name__ == "__main__":
    unittest.main()
