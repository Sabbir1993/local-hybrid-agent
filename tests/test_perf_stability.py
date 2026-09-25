"""Performance and stability round: per-thread SQLite connections, the
foreign-key repair, the cached memory search, monitor message counts and
atomic config writes. Everything runs on temp files; the embedder is stubbed."""

import asyncio
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from core import auth_db, config, db, db_repair, memory, monitor
from core.sqlite_util import ThreadLocalDB, transaction


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()


# ---------------- 1. SQLite connections ----------------
class ThreadLocalDBTests(_Tmp):
    def test_concurrent_writers_all_land(self):
        tdb = ThreadLocalDB(self.dir / "t.db")
        tdb.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, who INTEGER, n INTEGER)")
        tdb.commit()
        errors = []

        def work(who):
            try:
                for n in range(40):
                    tdb.execute("INSERT INTO t (who, n) VALUES (?, ?)", (who, n))
                    tdb.commit()
            except Exception as e:   # "database is locked" would land here
                errors.append(e)

        threads = [threading.Thread(target=work, args=(w,)) for w in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(tdb.execute("SELECT COUNT(*) FROM t").fetchone()[0], 8 * 40)
        self.assertEqual(tdb.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
        tdb.close()

    def test_each_thread_gets_its_own_connection(self):
        tdb = ThreadLocalDB(self.dir / "t.db")
        main = tdb.conn()
        other = []
        t = threading.Thread(target=lambda: other.append(tdb.conn()))
        t.start()
        t.join()
        self.assertIsNot(main, other[0])
        tdb.close()

    def test_transaction_rolls_back_and_frees_the_lock(self):
        tdb = ThreadLocalDB(self.dir / "t.db")
        tdb.execute("CREATE TABLE t (v INTEGER)")
        tdb.commit()
        with self.assertRaises(RuntimeError):
            with tdb.transaction() as c:
                c.execute("INSERT INTO t VALUES (1)")
                raise RuntimeError("boom")
        self.assertFalse(tdb.in_transaction)
        done = []
        t = threading.Thread(target=lambda: (tdb.execute("INSERT INTO t VALUES (2)"), tdb.commit(),
                                             done.append(1)))
        t.start()
        t.join(5)
        self.assertEqual(done, [1])
        self.assertEqual([r[0] for r in tdb.execute("SELECT v FROM t")], [2])
        tdb.close()

    def test_transaction_helper_accepts_plain_connection(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t (v INTEGER)")
        with transaction(conn) as c:
            c.execute("INSERT INTO t VALUES (1)")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM t").fetchone()[0], 1)


# ---------------- 2. foreign keys ----------------
def _broken_projects_db(path: Path) -> None:
    """The live projects.db shape: children point at dropped *_old tables."""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        PRAGMA foreign_keys=OFF;
        CREATE TABLE projects (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE "sessions" (id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER REFERENCES "projects_old"(id) ON DELETE CASCADE,
            title TEXT NOT NULL, created_at REAL NOT NULL, user_id INTEGER);
        CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL REFERENCES "sessions_old"(id) ON DELETE CASCADE,
            role TEXT NOT NULL, content TEXT NOT NULL, meta TEXT, created_at REAL NOT NULL);
        CREATE INDEX idx_messages_session ON messages(session_id);
        CREATE TABLE plan_items (id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            ord INTEGER NOT NULL, text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            note TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
        INSERT INTO projects VALUES (1, 'p', 0);
        INSERT INTO sessions VALUES (1, 1, 's1', 0, 7), (2, 1, 's2', 0, 7);
        INSERT INTO messages (session_id, role, content, created_at) VALUES (1, 'user', 'a', 0), (2, 'user', 'b', 0);
        INSERT INTO plan_items (session_id, ord, text, created_at, updated_at) VALUES (1, 1, 'step', 0, 0);
    """)
    conn.commit()
    conn.close()


class RepairTests(_Tmp):
    def setUp(self):
        super().setUp()
        self._bk = mock.patch.object(db_repair, "BACKUP_DIR", self.dir / "backups")
        self._bk.start()

    def tearDown(self):
        self._bk.stop()
        super().tearDown()

    def test_projects_rebuild_then_cascade(self):
        path = self.dir / "projects.db"
        _broken_projects_db(path)
        report = db_repair.inspect(path)
        self.assertEqual(report["rebuild"], {"messages": {"sessions_old": "sessions"},
                                             "sessions": {"projects_old": "projects"}})
        res = db_repair.repair(path)
        self.assertTrue(res["ok"], res)
        self.assertTrue(Path(res["backup"]).exists())
        self.assertTrue(db_repair.inspect(path)["clean"])

        tdb = ThreadLocalDB(path)
        tdb.foreign_keys = True
        self.assertEqual(tdb.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 2)
        self.assertEqual(tdb.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        # indexes survive the rebuild
        self.assertIn("idx_messages_session",
                      [r[1] for r in tdb.execute("PRAGMA index_list(messages)")])
        tdb.execute("INSERT INTO messages (session_id, role, content, created_at) VALUES (1, 'user', 'c', 0)")
        tdb.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            tdb.execute("INSERT INTO messages (session_id, role, content, created_at) VALUES (99, 'user', 'x', 0)")
        tdb.rollback()
        tdb.execute("DELETE FROM projects WHERE id = 1")
        tdb.commit()
        for table in ("sessions", "messages", "plan_items"):
            self.assertEqual(tdb.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)
        tdb.close()

    def test_orphans_are_nulled_or_deleted(self):
        path = self.dir / "auth.db"
        conn = sqlite3.connect(str(path))
        conn.executescript("""
            CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT);
            CREATE TABLE audit_log (id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id),
                                    username TEXT, action TEXT NOT NULL);
            CREATE TABLE auth_sessions (id TEXT PRIMARY KEY,
                                        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE);
            INSERT INTO users VALUES (1, 'alice');
            INSERT INTO audit_log VALUES (1, 1, 'alice', 'login'), (2, 42, 'gone', 'login');
            INSERT INTO auth_sessions VALUES ('keep', 1), ('orphan', 42);
        """)
        conn.commit()
        conn.close()
        res = db_repair.repair(path)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["orphans"], {"audit_log.user_id": {"action": "set NULL", "rows": 1},
                                          "auth_sessions.user_id": {"action": "delete", "rows": 1}})
        conn = sqlite3.connect(str(path))
        self.assertEqual(conn.execute("SELECT id, user_id, username FROM audit_log ORDER BY id").fetchall(),
                         [(1, 1, "alice"), (2, None, "gone")])   # audit rows are kept
        self.assertEqual([r[0] for r in conn.execute("SELECT id FROM auth_sessions")], ["keep"])
        conn.close()

    def test_clean_db_is_untouched(self):
        path = self.dir / "ok.db"
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE a (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        res = db_repair.repair(path)
        self.assertTrue(res["ok"] and res["clean"])
        self.assertIsNone(res["backup"])
        self.assertFalse((self.dir / "backups").exists())

    def test_unfixable_reference_is_refused(self):
        path = self.dir / "bad.db"
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE c (id INTEGER PRIMARY KEY, p INTEGER REFERENCES nowhere(id))")
        conn.commit()
        conn.close()
        res = db_repair.repair(path)
        self.assertFalse(res["ok"])
        self.assertIn("no replacement", res["error"])


class DeleteUserTests(_Tmp):
    def setUp(self):
        super().setUp()
        self._saved = auth_db._auth_db
        with mock.patch.object(auth_db, "AUTH_DB_FILE", self.dir / "auth.db"):
            auth_db._auth_db = auth_db._init_auth_db()
        auth_db._auth_db.foreign_keys = True

    def tearDown(self):
        auth_db._auth_db.close()
        auth_db._auth_db = self._saved
        super().tearDown()

    def _user(self, name):
        now = time.time()
        cur = auth_db.db().execute(
            "INSERT INTO users (username, created_at, updated_at) VALUES (?, ?, ?)", (name, now, now))
        auth_db.db().commit()
        return cur.lastrowid

    def test_delete_user_keeps_audit_rows(self):
        admin, bob = self._user("admin"), self._user("bob")
        c = auth_db.db()
        c.execute("INSERT INTO audit_log (ts, user_id, username, action, result) VALUES (?, ?, 'bob', 'login', 'allow')",
                  (time.time(), bob))
        c.execute("INSERT INTO auth_sessions (id, user_id, created_at, last_seen_at, expires_at) VALUES ('s', ?, 0, 0, 0)",
                  (bob,))
        role = c.execute("SELECT id FROM roles LIMIT 1").fetchone()[0]
        c.execute("INSERT INTO user_roles (user_id, role_id, assigned_at, assigned_by) VALUES (?, ?, 0, ?)",
                  (admin, role, bob))
        c.commit()
        self.assertEqual(c.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        auth_db.delete_user(bob)
        self.assertIsNone(c.execute("SELECT id FROM users WHERE id = ?", (bob,)).fetchone())
        row = c.execute("SELECT user_id, username FROM audit_log WHERE action = 'login'").fetchone()
        self.assertEqual((row[0], row[1]), (None, "bob"))
        self.assertEqual(c.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0], 0)
        self.assertIsNone(c.execute("SELECT assigned_by FROM user_roles WHERE user_id = ?", (admin,)).fetchone()[0])


class SessionDeleteForgetsMemoryTests(_Tmp):
    def test_delete_session_drops_its_chunks(self):
        with mock.patch.object(db, "PROJECTS_DB_FILE", self.dir / "projects.db"):
            tdb = db._init_projects_db()
        tdb.foreign_keys = True
        with mock.patch.object(db, "_projects_db", tdb), \
             mock.patch("core.memory.delete_session_chunks") as forget:
            pid = tdb.execute("INSERT INTO projects (name, created_at, user_id) VALUES ('p', 0, 7)").lastrowid
            sids = [tdb.execute("INSERT INTO sessions (project_id, title, created_at, user_id) VALUES (?, 's', 0, 7)",
                                (pid,)).lastrowid for _ in range(2)]
            for sid in sids:
                tdb.execute("INSERT INTO messages (session_id, role, content, created_at) VALUES (?, 'user', 'x', 0)",
                            (sid,))
            tdb.commit()
            db.db_delete_session(sids[0], 7)
            forget.assert_called_once_with([sids[0]])
            self.assertEqual(tdb.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)
            forget.reset_mock()
            db.db_delete_project(pid, 7)
            forget.assert_called_once_with([sids[1]])
            for table in ("projects", "sessions", "messages"):
                self.assertEqual(tdb.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)
        tdb.close()


# ---------------- 3. memory search ----------------
def _reference_memory_scores(entries, query, qvec):
    """The pre-cache scoring loop, kept here as the reference."""
    import numpy as np
    words = [w.lower() for w in __import__("re").findall(r"\w{3,}", query)][:8]
    lex, cos = [], []
    for _s, _p, text, v in entries:
        lex.append(float(sum(text.lower().count(w) for w in words)))
        if v is not None:
            a, b = np.asarray(qvec, dtype=np.float32), np.asarray(v, dtype=np.float32)
            den = float(np.linalg.norm(a) * np.linalg.norm(b))
            cos.append(float(np.dot(a, b) / den) if den > 1e-9 else 0.0)
        else:
            cos.append(0.0)
    ln, cn = memory._norm(lex), memory._norm(cos)
    out = [{"path": e[1], "score": 0.5 * cn[i] + 0.5 * ln[i]} for i, e in enumerate(entries)]
    out.sort(key=lambda r: r["score"], reverse=True)
    return out


class MemorySearchTests(unittest.TestCase):
    ENTRIES = [
        ("session", "session:1", "Bangladesh Bank policy rate discussion", [0.9, 0.1, 0.0]),
        ("session", "session:1", "lunch menu for friday", [0.0, 0.2, 0.9]),
        ("session", "session:2", "someone else's policy notes", [0.95, 0.05, 0.0]),
        ("session", "session:1", "policy rate history, no vector", None),
        ("workspace", "ws:a", "policy rate workspace file", [1.0, 0.0, 0.0]),
    ]

    def _search(self, query, qvec, user):
        async def fake_embed(texts):
            return [qvec]
        owners = {1: 7, 2: 8}
        with mock.patch.object(memory, "_cached_entries", return_value=memory._build_cache(self.ENTRIES)), \
             mock.patch.object(memory, "_embed_texts", side_effect=fake_embed), \
             mock.patch("core.db.db_session_owner", side_effect=lambda sid: owners.get(sid)):
            return asyncio.run(memory.search_memory_hybrid(query, k=10, requesting_user_id=user))

    def test_scores_match_reference_loop(self):
        q, qv = "policy rate", [1.0, 0.0, 0.0]
        got = self._search(q, qv, 7)
        mine = [e for e in self.ENTRIES if e[1] == "session:1"]
        ref = _reference_memory_scores(mine, q, qv)
        self.assertEqual([r["path"] for r in got], [r["path"] for r in ref])
        for g, r in zip(got, ref):
            self.assertAlmostEqual(g["score"], r["score"], places=5)
        self.assertEqual(got[0]["text"], "Bangladesh Bank policy rate discussion")

    def test_other_users_sessions_and_workspace_never_returned(self):
        texts = [r["text"] for r in self._search("policy", [1.0, 0.0, 0.0], 7)]
        self.assertNotIn("someone else's policy notes", texts)
        self.assertNotIn("policy rate workspace file", texts)
        self.assertEqual(self._search("policy", [1.0, 0.0, 0.0], None), [])

    def test_vector_dim_mismatch_scores_zero_not_crash(self):
        cache = memory._build_cache(self.ENTRIES)
        self.assertEqual(memory._cosines(cache, [0, 1], [1.0, 0.0]), [0.0, 0.0])


class MemoryCacheInvalidationTests(_Tmp):
    def setUp(self):
        super().setUp()
        self._saved = memory._conn
        memory._conn = None
        self._p = mock.patch.object(memory, "MEMORY_DB_FILE", self.dir / "memory.db")
        self._p.start()
        memory._set_cache({"gen": None})

    def tearDown(self):
        if memory._conn is not None:
            memory._conn.close()
        memory._conn = self._saved
        memory._set_cache({"gen": None})
        self._p.stop()
        super().tearDown()

    def test_write_from_another_connection_refreshes_cache(self):
        memory._store_vecs([("knowledge", "kb:1", 0, "first chunk", 1.0)], [[1.0, 0.0]])
        self.assertEqual(len(memory._cached_entries()["entries"]), 1)
        gen = memory._cached_entries()["gen"]
        self.assertIs(memory._cached_entries(), memory._cached_entries())   # no reload when unchanged
        other = sqlite3.connect(str(self.dir / "memory.db"))   # e.g. the DB explorer
        other.execute("INSERT INTO chunks (source, path, chunk_idx, text) VALUES ('knowledge', 'kb:2', 0, 'second')")
        other.commit()
        other.close()
        cache = memory._cached_entries()
        self.assertNotEqual(cache["gen"], gen)
        self.assertEqual(sorted(e[2] for e in cache["entries"]), ["first chunk", "second"])

    def test_delete_session_chunks(self):
        memory._store_vecs([("session", "session:5", 0, "a", 1.0), ("session", "session:6", 0, "b", 1.0)],
                           [[1.0], [1.0]])
        memory.delete_session_chunks([5])
        self.assertEqual([e[1] for e in memory._cached_entries()["entries"]], ["session:6"])


# ---------------- 4. monitor ----------------
class MonitorTests(unittest.TestCase):
    def test_n_msgs_sets_prompt_count_without_a_body(self):
        rid = monitor.monitor_begin("agent/test", True, n_msgs=5, model="m", source="local")
        try:
            rec = monitor._monitor_state["active"][rid]
            self.assertEqual(rec["prompt_tokens"], 5)
            self.assertEqual(rec["model"], "m")
        finally:
            monitor._monitor_state["active"].pop(rid, None)

    def test_body_bytes_still_supported(self):
        rid = monitor.monitor_begin("proxy", False, json.dumps({"messages": [1, 2], "model": "x"}).encode())
        try:
            rec = monitor._monitor_state["active"][rid]
            self.assertEqual((rec["prompt_tokens"], rec["model"]), (2, "x"))
        finally:
            monitor._monitor_state["active"].pop(rid, None)


# ---------------- 6. config writes ----------------
class ConfigWriteTests(_Tmp):
    def test_concurrent_updates_keep_every_key(self):
        path = self.dir / "app.json"
        path.write_text("{}", encoding="utf-8")
        with mock.patch("core.cloud.reload"):
            threads = [threading.Thread(target=config.update_app_config,
                                        args=(lambda c, i=i: c.__setitem__(f"k{i}", i), path))
                       for i in range(20)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data, {f"k{i}": i for i in range(20)})
        self.assertEqual([p.name for p in self.dir.iterdir()], ["app.json"])   # no temp files left

    def test_failed_write_leaves_old_file(self):
        path = self.dir / "app.json"
        path.write_text('{"keep": true}', encoding="utf-8")
        with self.assertRaises(TypeError):
            config.atomic_write_json(path, {"bad": object()})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"keep": True})
        self.assertEqual([p.name for p in self.dir.iterdir()], ["app.json"])

    def test_write_app_config_targets_given_path(self):
        path = self.dir / "app.json"
        with mock.patch("core.cloud.reload") as reload:
            config.write_app_config({"a": 1}, path)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"a": 1})
        reload.assert_called_once()


if __name__ == "__main__":
    unittest.main()
