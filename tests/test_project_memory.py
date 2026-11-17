"""tests/test_project_memory.py - Persistent project-level memory & cross-session tests."""

import sqlite3
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import working_memory as wm
from core.db import (
    db_create_project,
    db_delete_project,
    db_get_project_memory,
    db_set_project_memory,
    db_delete_project_memory,
    db_session_project_id,
    db_create_session,
)


class ProjectMemoryTests(unittest.TestCase):
    def setUp(self):
        self.suffix = uuid.uuid4().hex[:8]
        self.created_projects = []

    def tearDown(self):
        for pid, uid in self.created_projects:
            try:
                db_delete_project(pid, uid)
            except Exception:
                pass

    def test_project_memory_crud_and_isolation(self):
        # Create two projects for two users
        p1 = db_create_project(f"proj-alpha-{self.suffix}", "/tmp/p1", owner_user_id=101)
        p2 = db_create_project(f"proj-beta-{self.suffix}", "/tmp/p2", owner_user_id=102)
        self.created_projects.extend([(p1["id"], 101), (p2["id"], 102)])

        # Set memory for user 101 in proj-alpha
        db_set_project_memory(p1["id"], owner_user_id=101, key="tech_stack", content="FastAPI + PostgreSQL 16")
        db_set_project_memory(p1["id"], owner_user_id=101, key="conventions", content="Use type hints and pytest")

        mem1 = db_get_project_memory(p1["id"], owner_user_id=101)
        self.assertIn("tech_stack", mem1)
        self.assertEqual(mem1["tech_stack"]["content"], "FastAPI + PostgreSQL 16")
        self.assertIn("conventions", mem1)
        self.assertEqual(mem1["conventions"]["content"], "Use type hints and pytest")

        # User 102 should NOT be able to read or access user 101's project memory
        with self.assertRaises(PermissionError):
            db_get_project_memory(p1["id"], owner_user_id=102)

        with self.assertRaises(PermissionError):
            db_set_project_memory(p1["id"], owner_user_id=102, key="hack", content="leak")

        # Update an existing key
        db_set_project_memory(p1["id"], owner_user_id=101, key="tech_stack", content="FastAPI + PostgreSQL 17")
        mem_updated = db_get_project_memory(p1["id"], owner_user_id=101)
        self.assertEqual(mem_updated["tech_stack"]["content"], "FastAPI + PostgreSQL 17")

        # Delete single key
        deleted = db_delete_project_memory(p1["id"], owner_user_id=101, key="conventions")
        self.assertTrue(deleted)
        mem_after_del = db_get_project_memory(p1["id"], owner_user_id=101)
        self.assertNotIn("conventions", mem_after_del)
        self.assertIn("tech_stack", mem_after_del)

        # Wipe all project memory
        db_delete_project_memory(p1["id"], owner_user_id=101)
        mem_empty = db_get_project_memory(p1["id"], owner_user_id=101)
        self.assertEqual(len(mem_empty), 0)

    def test_project_memory_cascades_on_delete_project(self):
        p = db_create_project(f"cascade-test-{self.suffix}", "/tmp/c", owner_user_id=200)
        db_set_project_memory(p["id"], owner_user_id=200, key="db_port", content="5433")
        self.assertEqual(len(db_get_project_memory(p["id"], owner_user_id=200)), 1)

        # Delete project
        db_delete_project(p["id"], owner_user_id=200)

        # Project should be gone
        with self.assertRaises(PermissionError):
            db_get_project_memory(p["id"], owner_user_id=200)

    def test_session_project_mapping(self):
        p = db_create_project(f"session-map-{self.suffix}", "/tmp/s", owner_user_id=300)
        self.created_projects.append((p["id"], 300))
        s1 = db_create_session(p["id"], "Session 1", owner_user_id=300)
        s2 = db_create_session(p["id"], "Session 2", owner_user_id=300)

        # Both sessions map to the same project_id
        self.assertEqual(db_session_project_id(s1["id"]), p["id"])
        self.assertEqual(db_session_project_id(s2["id"]), p["id"])

        # Standalone session with no project
        s_none = db_create_session(None, "Standalone", owner_user_id=300)
        self.assertIsNone(db_session_project_id(s_none["id"]))

    def test_prompt_block_formatting(self):
        mem = {
            "database": {"content": "PostgreSQL on port 5434\nDB: billing_prod"},
            "auth": {"content": "JWT with RS256 signing"}
        }
        blk = wm.project_prompt_block(mem)
        self.assertIn("--- PROJECT PERSISTENT MEMORY", blk)
        self.assertIn("### database\nPostgreSQL on port 5434\nDB: billing_prod", blk)
        self.assertIn("### auth\nJWT with RS256 signing", blk)
        self.assertIn("--- END PROJECT PERSISTENT MEMORY ---", blk)

        # Empty dict produces empty string
        self.assertEqual(wm.project_prompt_block({}), "")


if __name__ == "__main__":
    unittest.main()
