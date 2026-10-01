"""tests/test_plan_guard.py - plan first, one step at a time, finish only when done.

Run: python -m unittest tests.test_plan_guard -v
"""
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.agent_loop import plan_guard as pg
from core.agent_loop.executor_view import _CONTROL_PREFIXES
from core.agent_tools import plans


def items(*statuses):
    return [{"ord": i + 1, "text": f"step {i + 1}", "status": s, "note": None} for i, s in enumerate(statuses)]


class NeedsPlan(unittest.TestCase):
    def test_modes(self):
        self.assertFalse(pg.needs_plan("creation", "build a game", {"plan_required": "off"}))
        self.assertTrue(pg.needs_plan("question", "hi?", {"plan_required": "always"}))
        self.assertFalse(pg.needs_plan("greeting", "hello", {"plan_required": "always"}))

    def test_auto(self):
        self.assertTrue(pg.needs_plan("creation", "make me a snake game", {}))
        self.assertFalse(pg.needs_plan("question", "what is 2+2?", {}))
        self.assertFalse(pg.needs_plan("action", "list the files", {}))
        self.assertTrue(pg.needs_plan("action", "fix the login bug and add a test for it", {}))
        self.assertTrue(pg.needs_plan("other", "x " * 150, {}))

    def test_bad_setting_falls_back_to_auto(self):
        self.assertEqual(pg.plan_mode_setting({"plan_required": "sometimes"}), "auto")


class Focus(unittest.TestCase):
    def test_current_item(self):
        self.assertEqual(pg.current_item(items("done", "in_progress", "pending"))["ord"], 2)
        self.assertEqual(pg.current_item(items("done", "pending", "pending"))["ord"], 2)
        self.assertIsNone(pg.current_item(items("done", "failed")))

    def test_focus_message_is_a_control_message(self):
        m = pg.focus_message(items("done", "in_progress", "pending"))
        self.assertTrue(m.startswith(_CONTROL_PREFIXES[1]))
        self.assertIn("#2", m)
        self.assertIn("Do not start step #3", m)
        self.assertEqual(pg.focus_message(items("done")), "")

    def test_stuck_message_is_a_control_message(self):
        self.assertTrue(pg.stuck_message(items("in_progress")[0], 8).startswith("[plan reminder]"))


class Order(unittest.TestCase):
    def test_done_out_of_order_refused(self):
        self.assertIn("#1", pg.check_update(items("in_progress", "pending"), 2, "done"))
        self.assertIsNone(pg.check_update(items("done", "in_progress"), 2, "done"))
        self.assertIsNone(pg.check_update(items("failed", "in_progress"), 2, "done"))

    def test_single_in_progress(self):
        self.assertIn("already in progress", pg.check_update(items("in_progress", "pending"), 2, "in_progress"))
        self.assertIsNone(pg.check_update(items("in_progress", "pending"), 1, "in_progress"))

    def test_stuck_action(self):
        self.assertEqual([pg.stuck_action(n, 8) for n in (0, 7, 8, 40)], ["ok", "ok", "warn", "warn"])
        self.assertEqual(pg.stuck_action(99, 0), "ok")


class PlanTools(unittest.TestCase):
    """create_plan / update_plan_item against an in-memory stand-in for the plan table."""

    def setUp(self):
        self.rows = []

        def set_items(sid, texts):
            self.rows[:] = [{"ord": i, "text": t, "status": "pending", "note": None} for i, t in enumerate(texts, 1)]
            return list(self.rows)

        def set_status(sid, no, status, note=None):
            for r in self.rows:
                if r["ord"] == no:
                    r["status"] = status
                    r["note"] = note or r["note"]
            return {}
        self._p = [mock.patch("core.db.db_get_plan_items", lambda sid: [dict(r) for r in self.rows]),
                   mock.patch("core.db.db_set_plan_items", set_items),
                   mock.patch("core.db.db_set_plan_item_status", set_status)]
        for p in self._p:
            p.start()
        plans.set_plan_context(1)

    def tearDown(self):
        for p in self._p:
            p.stop()
        plans.set_plan_context(None)

    def status(self):
        return [r["status"] for r in self.rows]

    def test_first_step_starts_in_progress(self):
        plans.tool_create_plan({"items": ["a", "b", "c"]})
        self.assertEqual(self.status(), ["in_progress", "pending", "pending"])

    def test_done_promotes_next(self):
        plans.tool_create_plan({"items": ["a", "b"]})
        out = plans.tool_update_plan_item({"item": 1, "status": "done"})
        self.assertEqual(self.status(), ["done", "in_progress"])
        self.assertIn("Next: step #2", out)
        self.assertIn("All steps are finished", plans.tool_update_plan_item({"item": 2, "status": "done"}))

    def test_out_of_order_done_is_an_error(self):
        plans.tool_create_plan({"items": ["a", "b"]})
        with self.assertRaises(ValueError):
            plans.tool_update_plan_item({"item": 2, "status": "done"})

    def test_failed_also_advances(self):
        plans.tool_create_plan({"items": ["a", "b"]})
        plans.tool_update_plan_item({"item": 1, "status": "failed", "note": "x"})
        self.assertEqual(self.status(), ["failed", "in_progress"])

    def test_recreate_does_not_wipe_progress_without_replace(self):
        plans.tool_create_plan({"items": ["a", "b", "c"]})
        plans.tool_update_plan_item({"item": 1, "status": "done"})
        with self.assertRaises(ValueError):
            plans.tool_create_plan({"items": ["x", "y"]})
        self.assertEqual(self.status(), ["done", "in_progress", "pending"])
        plans.tool_create_plan({"items": ["x", "y"], "replace": True})
        self.assertEqual(len(self.rows), 2)

    def test_a_finished_plan_can_be_replaced(self):
        plans.tool_create_plan({"items": ["a"]})
        plans.tool_update_plan_item({"item": 1, "status": "done"})
        plans.tool_create_plan({"items": ["new task"]})
        self.assertEqual(self.status(), ["in_progress"])


class RunIsWired(unittest.TestCase):
    src = (Path(__file__).resolve().parents[1] / "routes" / "agent" / "run.py").read_text(encoding="utf-8")

    def test_hooks_present(self):
        for needle in ("plan_guard.needs_plan(", "plan_gate = bool(", "plan_guard.focus_message(",
                       "plan_guard.stuck_action(", "item_nudges < MAX_PLAN_NUDGES + 1",
                       'name not in PLAN_MODE_TOOLS'):
            self.assertIn(needle, self.src, needle)

    def test_finish_is_withheld_while_steps_are_open(self):
        self.assertTrue(re.search(r"open_items\(db_get_plan_items\(req\.session_id\)\):\s*\n\s*tools_for_lane = without_finish", self.src))


if __name__ == "__main__":
    unittest.main()
