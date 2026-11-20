"""One plan per task (core/agent_loop/plan_guard.py):
- a finished plan never sends the run back to create_plan (it answers instead)
- a new task in the same chat starts without the last task's plan; a continuation keeps it
- a small task runs without a plan"""
import unittest

from core.agent_loop import plan_guard as pg


def _plan(*statuses):
    return [{"ord": i + 1, "text": f"step {i + 1}", "status": s} for i, s in enumerate(statuses)]


class FinishedPlanDoesNotReopenTheGate(unittest.TestCase):
    def test_plan_made_this_run(self):
        self.assertFalse(pg.plan_made_this_run([]))
        self.assertFalse(pg.plan_made_this_run([{"name": "create_plan", "ok": False}]))     # refused: still no plan
        self.assertTrue(pg.plan_made_this_run([{"name": "read_file", "ok": True},
                                               {"name": "create_plan", "ok": True}]))

    def test_gate_stays_off_once_every_step_is_done(self):
        # the loop's condition (routes/agent/stream.py): required, no plan made this run, no open steps
        actions = [{"name": "create_plan", "ok": True}, {"name": "update_plan_item", "ok": True}]
        finished = _plan("done", "done", "failed")
        gate = (True and not pg.plan_made_this_run(actions) and not pg.open_items(finished))
        self.assertFalse(gate, "a finished plan must not ask for a new one")


class NewTaskOrContinuation(unittest.TestCase):
    def test_continuations_keep_the_plan(self):
        for q in ("yes", "ok go ahead", "continue", "proceed", "build it", "implement the plan",
                  "next step", "step 3 failed, try again", "keep going",
                  "Continue the previous task. Pick up where you stopped."):
            self.assertTrue(pg.continues_plan(q), q)

    def test_new_tasks_are_not_continuations(self):
        for q in ("add a dark mode toggle to the settings page", "what is my IP address",
                  "write a report of the open ports on this machine"):
            self.assertFalse(pg.continues_plan(q), q)

    def test_finished_plan_is_dropped_for_a_new_task(self):
        self.assertEqual(pg.plan_for_new_message(_plan("done", "done"), "now add a footer"), "drop")

    def test_part_done_plan_is_dropped_for_a_new_task_but_kept_to_continue(self):
        half = _plan("done", "in_progress", "pending")
        self.assertEqual(pg.plan_for_new_message(half, "what time is it in Dhaka"), "drop")
        self.assertEqual(pg.plan_for_new_message(half, "continue"), "keep")

    def test_untouched_plan_is_kept_for_the_plan_to_build_hand_off(self):
        fresh = _plan("in_progress", "pending", "pending")
        self.assertEqual(pg.plan_for_new_message(fresh, "build it with postgres instead of mysql"), "keep")

    def test_plan_mode_keeps_it(self):
        self.assertEqual(pg.plan_for_new_message(_plan("done", "done"), "change step 2", plan_mode=True), "keep")

    def test_no_plan(self):
        self.assertEqual(pg.plan_for_new_message([], "anything"), "keep")


class SmallTasksSkipThePlan(unittest.TestCase):
    def test_small(self):
        for q in ("create a login page", "fix the typo in header.php",
                  "write a function that reverses a string", "rename the variable cnt to count in utils.py"):
            self.assertTrue(pg.is_small_task(q), q)
            self.assertFalse(pg.needs_plan("creation", q, {}), q)

    def test_not_small(self):
        for q in ("build a todo app", "audit my full machine and generate report",
                  "create the model, then the controller", "create a.py and b.py",
                  "fix the login bug and add a test", "x " * 80):
            self.assertFalse(pg.is_small_task(q), q)

    def test_always_still_plans_a_small_task(self):
        self.assertTrue(pg.needs_plan("creation", "create a login page", {"plan_required": "always"}))


if __name__ == "__main__":
    unittest.main()
