"""tests/test_agent_limits_routes.py - Settings endpoints for agent limits and the user's memory files.

Run: python -m unittest tests.test_agent_limits_routes -v
"""

import asyncio
import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import auth_db, deps
from core.auth import Principal
from core.agent_tools import limits
from routes.capabilities.endpoints import settings as caps
from routes.capabilities.models import AgentLimitsReq
from routes.agent import memory_endpoints
from routes.agent.base import router as agent_router


def principal(uid=1):
    return Principal(id=uid, username=f"u{uid}", display_name="u", is_super_admin=False,
                     must_change_password=False, role_names=["user"], permission_keys={"chat.use"})


class LimitsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg_path = Path(self.tmp.name) / "app.json"
        shutil.copy(Path(__file__).resolve().parents[1] / "config" / "app.json", self.cfg_path)
        self._patches = [
            mock.patch.object(caps, "CONFIG_FILE", self.cfg_path),
            mock.patch.dict("core.small_model.APP_CONFIG", {"agent": {}, "context": {}, "memory": {}}),
            mock.patch("routes.capabilities.endpoints.settings.audit_log", lambda *a, **k: None),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def post(self, **kw):
        return asyncio.run(caps.agent_limits_set(AgentLimitsReq(**kw), user=principal()))

    def test_defaults_are_served_with_ranges(self):
        v = asyncio.run(caps.agent_limits_get(user=principal()))
        self.assertEqual(v["values"]["executor_max_tokens"], 4096)
        self.assertEqual(v["values"]["compaction_threshold"], 0.70)
        self.assertEqual(v["ranges"]["executor_max_tokens"], [512, 16000])
        self.assertFalse(v["values"]["mirror_to_workspace"])
        self.assertFalse(v["values"]["memory_allow_cloud"], "memory stays off cloud lanes unless allowed")

    def test_change_is_clamped_persisted_and_live(self):
        out = self.post(executor_max_tokens=99999, verify_max_retries=0, compaction_threshold=0.1,
                        memory_file_max_bytes=4096, memory_allow_cloud=True, mirror_to_workspace=True)
        self.assertEqual(out["values"]["executor_max_tokens"], 16000)
        self.assertEqual(out["values"]["verify_max_retries"], 1)
        self.assertEqual(out["values"]["compaction_threshold"], 0.3)
        saved = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["agent"]["executor_max_tokens"], 16000)
        self.assertEqual(saved["memory"]["file_max_bytes"], 4096)
        self.assertTrue(saved["memory"]["allow_cloud"])
        self.assertEqual(limits.agent_limit("executor_max_tokens"), 16000, "applied without a restart")
        self.assertEqual(limits.effective_max_tokens(-1, "executor"), 16000)

    def test_executor_effort_ceiling_is_validated(self):
        self.assertEqual(self.post(executor_max_effort="ultra").status_code, 400)
        out = self.post(executor_max_effort="Medium")
        self.assertEqual(out["values"]["executor_max_effort"], "medium")

    def test_nothing_to_change_is_an_error(self):
        self.assertEqual(self.post().status_code, 400)

    def test_invalid_stored_values_fall_back_to_defaults(self):
        with mock.patch.dict("core.small_model.APP_CONFIG", {"agent": {"executor_max_tokens": "lots"}}):
            self.assertEqual(limits.agent_limit("executor_max_tokens"), 4096)


class MemoryRoutesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._saved = auth_db._auth_db
        with mock.patch.object(auth_db, "AUTH_DB_FILE", Path(self.tmp.name) / "auth.db"):
            auth_db._auth_db = auth_db._init_auth_db()
        now = time.time()
        for uid in (1, 2):
            auth_db._auth_db.execute(
                "INSERT INTO users (id, username, is_super_admin, created_at, updated_at) VALUES (?, ?, 0, ?, ?)",
                (uid, f"u{uid}", now, now))
        auth_db._auth_db.commit()
        self.app = FastAPI()
        self.app.include_router(agent_router)
        self.who = {"id": 1}
        self.app.dependency_overrides[deps.get_current_user] = lambda: principal(self.who["id"])
        self.client = TestClient(self.app)

    def tearDown(self):
        auth_db._auth_db.close()
        auth_db._auth_db = self._saved
        self.tmp.cleanup()

    def put(self, path, content, **kw):
        return self.client.put(f"/agent/memory/{path}", json={"content": content, **kw})

    def test_crud_and_privacy(self):
        r = self.put("preferences.md", "---\ndescription: How I like answers\n---\n- short answers\n")
        self.assertEqual(r.status_code, 200, r.text)
        ver = r.json()["version"]
        listing = self.client.get("/agent/memory").json()
        self.assertEqual([f["path"] for f in listing["files"]], ["preferences.md"])
        self.assertEqual(listing["limits"]["file_max_bytes"], 8192)
        got = self.client.get("/agent/memory/preferences.md").json()
        self.assertIn("short answers", got["body"])
        # stale version -> 409 with the current content
        self.assertEqual(self.put("preferences.md", "---\ndescription: d\n---\n- x", if_version="stale").status_code, 409)
        self.assertEqual(self.put("preferences.md", "---\ndescription: d\n---\n- x", if_version=ver).status_code, 200)
        # another user sees and can delete nothing
        self.who["id"] = 2
        self.assertEqual(self.client.get("/agent/memory").json()["files"], [])
        self.assertEqual(self.client.get("/agent/memory/preferences.md").status_code, 404)
        self.assertEqual(self.client.delete("/agent/memory/preferences.md").status_code, 404)
        self.who["id"] = 1
        self.assertEqual(self.client.delete("/agent/memory/preferences.md").status_code, 200)
        self.assertEqual(self.client.get("/agent/memory").json()["files"], [])

    def test_secret_and_bad_path_are_rejected(self):
        r = self.put("profile.md", "---\ndescription: d\n---\n- my password is Zx9-placeholder-Q\n")
        self.assertEqual(r.status_code, 400)
        self.assertNotIn("Zx9-placeholder-Q", r.text)
        self.assertEqual(self.put("notes.txt", "---\ndescription: d\n---\n- x").status_code, 400)

    def test_erase_everything(self):
        self.put("a.md", "---\ndescription: d\n---\n- x")
        self.put("b.md", "---\ndescription: d\n---\n- y")
        self.assertEqual(self.client.delete("/agent/memory").json()["deleted"], 2)


if __name__ == "__main__":
    unittest.main()
