"""Chats are named by topic (core/session_title.py), not by their first words: "Hi" waits for the real question."""
import asyncio
import sqlite3
import time
import unittest
from unittest import mock

from core import session_title
from core.db import sessions


def _db():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER, title TEXT, "
              "created_at REAL, user_id INTEGER, agent_id INTEGER, model TEXT, title_auto INTEGER DEFAULT 0)")
    c.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER, role TEXT, "
              "content TEXT, meta TEXT, created_at REAL)")
    return c


def _reply(text):
    return {"choices": [{"message": {"content": text}}]}


class TitleCleaning(unittest.TestCase):
    def test_model_formatting_is_removed(self):
        self.assertEqual(session_title.clean_title('<think>hm</think>\n**Title: "Bangladesh Economy Overview."**'),
                         "Bangladesh Economy Overview")
        self.assertEqual(session_title.clean_title("# RAG Basics"), "RAG Basics")

    def test_none_and_empty_mean_no_title(self):
        for raw in ("NONE", "none.", "", "   ", "<think>only thinking</think>"):
            self.assertIsNone(session_title.clean_title(raw))

    def test_long_titles_are_cut_at_a_word(self):
        t = session_title.clean_title("Bangladesh economy and its growth drivers in the last ten years explained simply")
        self.assertLessEqual(len(t), session_title.MAX_TITLE_CHARS + 1)
        self.assertTrue(t.endswith("…"))

    def test_bangla_title_is_kept(self):
        self.assertEqual(session_title.clean_title("বাংলাদেশের অর্থনীতি"), "বাংলাদেশের অর্থনীতি")

    def test_small_talk(self):
        for msg in ("Hi", "hello!", "Assalamu alaikum", "thanks", "হাই", "Good morning"):
            self.assertTrue(session_title._is_small_talk(msg), msg)
        for msg in ("Hi, what is RAG?", "write a paragraph about bangladesh economy", "What os RAG"):
            self.assertFalse(session_title._is_small_talk(msg), msg)


class SuggestTitle(unittest.TestCase):
    def setUp(self):
        self.db = db = _db()
        owners = lambda sid: {r["id"]: r["user_id"] for r in db.execute("SELECT id, user_id FROM sessions")}.get(sid)
        for p in (mock.patch.object(sessions, "_projects_db", db),
                  mock.patch.object(sessions, "db_session_owner", owners),
                  mock.patch.object(sessions, "_at_rest", lambda t: t)):
            p.start()
            self.addCleanup(p.stop)
        self.calls = []
        self.answer = "Bangladesh Economy Overview"

        async def fake_post_chat(job, payload, user_id=None, force_local=False, only_if_running=False, timeout=None):
            self.calls.append((job, payload, only_if_running))
            if isinstance(self.answer, Exception):
                raise self.answer
            return _reply(self.answer), None

        p = mock.patch.object(session_title.lanes, "post_chat", fake_post_chat)
        p.start()
        self.addCleanup(p.stop)

    def _chat(self, *turns, auto=True, title="Hi"):
        sid = sessions.db_create_session(0, title, owner_user_id=1, auto_title=auto)["id"]
        for u, a in turns:
            self.db.execute("INSERT INTO messages (session_id, role, content, created_at) VALUES (?, 'user', ?, ?)", (sid, u, time.time()))
            self.db.execute("INSERT INTO messages (session_id, role, content, created_at) VALUES (?, 'assistant', ?, ?)", (sid, a, time.time()))
        return sid

    def _suggest(self, sid, user=1):
        return asyncio.run(session_title.suggest_title(sid, user))

    def _row(self, sid):
        return self.db.execute("SELECT title, title_auto FROM sessions WHERE id = ?", (sid,)).fetchone()

    def test_greeting_only_waits_without_asking_a_model(self):
        sid = self._chat(("Hi", "Hello! How can I help?"))
        res = self._suggest(sid)
        self.assertEqual((res["changed"], res["auto"]), (False, True))
        self.assertEqual(self.calls, [])
        self.assertEqual(tuple(self._row(sid)), ("Hi", 1))

    def test_real_question_after_hi_names_the_chat_once(self):
        sid = self._chat(("Hi", "Hello!"), ("write a paragraph about bangladesh economy", "Bangladesh's economy ..."))
        res = self._suggest(sid)
        self.assertEqual((res["title"], res["changed"], res["auto"]), ("Bangladesh Economy Overview", True, False))
        self.assertEqual(tuple(self._row(sid)), ("Bangladesh Economy Overview", 0))
        job, payload, only_running = self.calls[0]
        self.assertEqual(job, "session_title")
        self.assertTrue(only_running)                     # never loads a model just to name a chat
        self.assertIn("bangladesh economy", payload["messages"][1]["content"])
        # settled: a later reply does not ask again
        self._suggest(sid)
        self.assertEqual(len(self.calls), 1)

    def test_the_first_topic_names_the_chat_not_a_later_one(self):
        sid = self._chat(("Hi", "Hello!"), ("Hi, I need detail of cyber security", "Cybersecurity is ..."),
                         ("when should qwen 4-27B release?", "No date yet ..."))
        self._suggest(sid)
        sent = self.calls[0][1]["messages"][1]["content"]
        self.assertIn("cyber security", sent)
        self.assertNotIn("qwen", sent.lower())
        self.assertNotIn("User: Hi\n", sent)            # greetings are left out too

    def test_a_rename_by_the_user_is_never_replaced(self):
        sid = self._chat(("What is RAG?", "RAG is ..."))
        sessions.db_update_session_title(sid, "My notes", owner_user_id=1)
        res = self._suggest(sid)
        self.assertEqual((res["title"], res["changed"]), ("My notes", False))
        self.assertEqual(self.calls, [])

    def test_chats_without_the_flag_keep_their_title(self):
        sid = self._chat(("What is RAG?", "RAG is ..."), auto=False, title="What is RAG")
        self.assertFalse(self._suggest(sid)["changed"])
        self.assertEqual(self.calls, [])

    def test_model_failure_keeps_trying_on_the_next_reply(self):
        sid = self._chat(("What is RAG?", "RAG is ..."))
        self.answer = RuntimeError("no model available")
        res = self._suggest(sid)
        self.assertEqual((res["changed"], res["auto"]), (False, True))
        self.answer = "Retrieval Augmented Generation"
        self.assertEqual(self._suggest(sid)["title"], "Retrieval Augmented Generation")

    def test_a_failed_model_call_never_ends_auto_titling(self):
        # 4+ questions and the model is down: that is not "no topic", the next reply must still try
        sid = self._chat(*[("What is RAG?", "RAG is ...")] * (session_title.MAX_USER_TURNS + 1))
        self.answer = RuntimeError("no model available")
        res = self._suggest(sid)
        self.assertTrue(res["auto"])
        self.assertIn("error", res)
        self.assertEqual(self._row(sid)["title_auto"], 1)

    def test_none_after_max_turns_ends_auto_titling(self):
        sid = self._chat(*[("ok so", "sure")] * session_title.MAX_USER_TURNS)
        self.answer = "NONE"
        self.assertFalse(self._suggest(sid)["auto"])

    def test_backfill_model_url_must_be_local(self):
        self.assertEqual(session_title._local_url("http://127.0.0.1:8090/"), "http://127.0.0.1:8090")
        for bad in ("http://10.0.0.5:8090", "https://api.example.com", "ftp://localhost"):
            with self.assertRaises(ValueError):
                session_title._local_url(bad)

    def test_gives_up_after_max_turns_of_small_talk(self):
        sid = self._chat(*[("Hi", "Hello!")] * session_title.MAX_USER_TURNS)
        res = self._suggest(sid)
        self.assertFalse(res["auto"])
        self.assertEqual(tuple(self._row(sid)), ("Hi", 0))
        self.assertEqual(self.calls, [])

    def test_only_the_owner(self):
        sid = self._chat(("What is RAG?", "RAG is ..."))
        with self.assertRaises(PermissionError):
            self._suggest(sid, user=2)


class Backfill(SuggestTitle):
    """Old chats (made before topic titles) are retitled only when their title is the automatic one."""

    def setUp(self):
        super().setUp()
        import core.db as core_db
        p = mock.patch.object(core_db, "_projects_db", self.db)
        p.start()
        self.addCleanup(p.stop)

    def _old_chat(self, title, *turns, agent_id=None):
        sid = self._chat(*turns, auto=False, title=title)
        if agent_id is not None:
            self.db.execute("UPDATE sessions SET agent_id = ? WHERE id = ?", (agent_id, sid))
        return sid

    def _run(self, apply):
        return asyncio.run(session_title.backfill(apply=apply, log=lambda *_: None))

    def test_placeholder_detection(self):
        self.assertTrue(session_title.is_placeholder("Hi", "hi"))
        self.assertTrue(session_title.is_placeholder("What os RAG", "What os RAG ?"))
        self.assertTrue(session_title.is_placeholder("New Chat", "anything"))
        self.assertTrue(session_title.is_placeholder("Hi, I need detail of cyber secur…", "Hi, I need detail of cyber security"))
        self.assertFalse(session_title.is_placeholder("My cyber notes", "Hi, I need detail of cyber security"))

    def test_dry_run_changes_nothing(self):
        sid = self._old_chat("Hi", ("Hi", "Hello!"), ("write a paragraph about bangladesh economy", "..."))
        stats = self._run(apply=False)
        self.assertEqual(stats["candidates"], 1)
        self.assertEqual(tuple(self._row(sid)), ("Hi", 0))
        self.assertEqual(self.calls, [])

    def test_apply_retitles_placeholders_only(self):
        auto = self._old_chat("Hi", ("Hi", "Hello!"), ("write a paragraph about bangladesh economy", "..."))
        renamed = self._old_chat("My economy notes", ("write a paragraph about bangladesh economy", "..."))
        agent = self._old_chat("🤖 Helper", ("write a paragraph about bangladesh economy", "..."), agent_id=7)
        empty = self._chat(auto=False, title="Standalone")
        greeting = self._old_chat("Hi", ("Hi", "Hello!"))
        stats = self._run(apply=True)
        self.assertEqual(stats["renamed"], 1)
        self.assertEqual(self._row(auto)["title"], "Bangladesh Economy Overview")
        self.assertEqual(self._row(renamed)["title"], "My economy notes")
        self.assertEqual(self._row(agent)["title"], "🤖 Helper")
        self.assertEqual(self._row(empty)["title"], "Standalone")
        # greetings only: keeps "Hi" and stays automatic, so a later question can still name it
        self.assertEqual(tuple(self._row(greeting)), ("Hi", 1))
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
