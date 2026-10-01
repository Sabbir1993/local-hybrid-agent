"""tests/test_working_memory.py - structured compaction summary, persistence and PLAN.md mirror.

Covers brief acceptance case 6 (a long session crosses the compaction threshold and continues from
the summary and the plan). Run: python -m unittest tests.test_working_memory -v
"""

import asyncio
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import context_budget, working_memory as wm
from core.agent_loop import compact_messages
from core.sqlite_util import ThreadLocalDB


def call(cid, name, **args):
    import json
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


def result(cid, text):
    return {"role": "tool", "tool_call_id": cid, "content": text}


PLAN = [
    {"ord": 1, "text": "create app skeleton", "status": "done", "note": None},
    {"ord": 2, "text": "add the scoring module", "status": "in_progress", "note": None},
    {"ord": 3, "text": "write the README", "status": "pending", "note": None},
]


def long_run():
    msgs = [{"role": "system", "content": "S" * 500},
            {"role": "user", "content": "build a snake game in game/ with scoring"}]
    msgs += [call("c1", "write_file", path="game/main.py", content="x"),
             result("c1", "wrote 500 chars (20 lines) to game/main.py (created)\nverify: OK (python syntax)"),
             call("c2", "read_file", path="game/main.py"), result("c2", "1\tx\n" * 300),
             call("c3", "edit_file", path="game/main.py", old_string="a", new_string="b"),
             result("c3", "edited game/main.py at line 3: 1 replacement(s)\nverify: FAILED - SyntaxError at line 3: bad. Attempt 1/3."),
             call("c4", "run_shell", command="python game/main.py"), result("c4", "error: exit code 1 Traceback"),
             ]
    for i in range(6):
        msgs += [call(f"r{i}", "read_file", path=f"game/f{i}.py"), result(f"r{i}", "line\n" * 800)]
    return msgs


class SummaryTests(unittest.TestCase):
    def test_summary_has_goal_files_problems_plan_and_next_step(self):
        s = wm.build_summary(long_run(), PLAN)
        self.assertIn("Goal: build a snake game", s)
        self.assertIn("game/main.py (created, edited 1x)", s)
        self.assertIn("Open problems:", s)
        self.assertIn("SyntaxError at line 3", s)
        self.assertIn("[x] 1. create app skeleton", s)
        self.assertIn("Next step: 2. add the scoring module", s)
        self.assertLessEqual(len(s), wm.MAX_SUMMARY_CHARS)

    def test_problem_disappears_once_the_file_is_fixed(self):
        msgs = long_run() + [call("c9", "edit_file", path="game/main.py", old_string="b", new_string="c"),
                             result("c9", "edited game/main.py at line 3: 1 replacement(s)\nverify: OK (python syntax)")]
        self.assertNotIn("SyntaxError at line 3", wm.build_summary(msgs, PLAN))

    def test_finished_plan_says_so(self):
        done = [dict(i, status="done") for i in PLAN]
        self.assertIn("all plan steps are done", wm.build_summary(long_run(), done))

    def test_control_messages_are_not_mistaken_for_the_goal(self):
        msgs = long_run() + [{"role": "user", "content": "[plan reminder] 1/3 done"}]
        self.assertIn("Goal: build a snake game", wm.build_summary(msgs, PLAN))


class CompactionTests(unittest.TestCase):
    def test_summary_is_placed_in_the_digest_and_returned(self):
        msgs = long_run()
        out_list = []
        out = compact_messages(msgs, 3000, summary_fn=lambda: wm.build_summary(msgs, PLAN), summary_out=out_list)
        digest = out[1]["content"]
        self.assertIn("[TASK STATE]", digest)
        self.assertIn("Next step: 2. add the scoring module", digest)
        self.assertEqual(len(out_list), 1)

    def test_no_summary_work_when_nothing_is_compacted(self):
        called = []
        out = compact_messages([{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}], 100000,
                               summary_fn=lambda: called.append(1) or "x")
        self.assertEqual(called, [])
        self.assertEqual(len(out), 2)

    def test_failing_summary_never_breaks_compaction(self):
        def boom():
            raise RuntimeError("x")
        out = compact_messages(long_run(), 3000, summary_fn=boom)
        self.assertIn("CONVERSATION DIGEST", out[1]["content"])

    def test_threshold_comes_from_config(self):
        with mock.patch.dict("core.small_model.APP_CONFIG", {"context": {"compaction_threshold": 0.5}}):
            self.assertAlmostEqual(context_budget.margin(), 0.5)
            self.assertLess(context_budget.budget_for("executor", 32768), 32768 * 0.51)
        with mock.patch.dict("core.small_model.APP_CONFIG", {"context": {"compaction_threshold": 5}}):
            self.assertAlmostEqual(context_budget.margin(), 0.95)
        with mock.patch.dict("core.small_model.APP_CONFIG", {}, clear=True):
            self.assertAlmostEqual(context_budget.margin(), 0.70)


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.db = ThreadLocalDB(":memory:", row_factory=sqlite3.Row)
        self.db.executescript(
            "CREATE TABLE session_working_memory (session_id INTEGER PRIMARY KEY, user_id INTEGER, "
            "content TEXT NOT NULL, updated_at REAL NOT NULL);")
        self.p = mock.patch.object(wm, "_get_projects_db", lambda: self.db)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_save_replaces_and_load_is_per_user(self):
        wm.save(5, 1, "first")
        wm.save(5, 1, "second")
        self.assertEqual(wm.load(5, 1), "second")
        self.assertEqual(wm.load(5, 2), "", "another user's session id never returns it")
        self.assertEqual(wm.load(None, 1), "")

    def test_card_numbers_are_masked_at_rest(self):
        wm.save(6, 1, "paid with 4111 1111 1111 1111 earlier")
        self.assertNotIn("4111 1111 1111 1111", wm.load(6, 1))

    def test_prompt_block_frames_it_as_notes(self):
        b = wm.prompt_block("Goal: x")
        self.assertIn("WORKING MEMORY", b)
        self.assertIn("may be out of date", b)
        self.assertEqual(wm.prompt_block("  "), "")

    def test_plan_markdown(self):
        md = wm.plan_markdown(PLAN)
        self.assertIn("- [x] create app skeleton", md)
        self.assertIn("- [~] add the scoring module", md)
        self.assertIn("- [ ] write the README", md)

    def test_mirror_writes_two_files_on_the_device(self):
        writes = {}

        async def fake_call(uid, op, params, timeout=None):
            writes[params["path"]] = params["content"]
            return {}
        with mock.patch("core.companion_bridge.call", fake_call):
            asyncio.run(wm.mirror_to_device(1, r"C:\proj", "Goal: x", PLAN))
        keys = sorted(Path(k).name for k in writes)
        self.assertEqual(keys, ["PLAN.md", "working_memory.md"])
        self.assertTrue(any(".agent" in k for k in writes))


if __name__ == "__main__":
    unittest.main()
