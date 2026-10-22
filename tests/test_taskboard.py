"""Taskboard: one list of background work read from the run journal, stop that reaches the owner of the
work, and per-user isolation (another user's id is a 404, never a 403).

Run: python -m unittest tests.test_taskboard -v
"""
import asyncio
import sys
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import runs, taskboard, tool_surface
from core.deps import get_current_user
from core.request_context import set_current_user
from core.taskboard import tools as tb_tools
from routes import taskboard as tb_routes
from routes.agent import run_manager

ME = 90_001 + uuid.uuid4().int % 1000      # the journal is the real usage.db: stay out of real users' rows
OTHER = ME + 5000


def rid(prefix="t"):
    return f"{prefix}-" + uuid.uuid4().hex[:12]


def run(coro):
    return asyncio.run(coro)


class ListTests(unittest.TestCase):
    def test_running_first_then_newest_and_scoped_to_the_user(self):
        ME = 5_000_000 + uuid.uuid4().int % 1_000_000      # a user of its own: other tests leave rows under ME
        OTHER = ME + 1
        done, live, kb, theirs = rid(), rid(), rid("kb"), rid()
        runs.begin(done, "agent", ME)
        runs.end(done, "completed")
        runs.begin(live, "subagent", ME, stage="build")
        runs.begin(kb, "kb_ingest", ME, ref="12")
        runs.begin(theirs, "agent", OTHER)
        tasks = taskboard.list_tasks(ME)
        ids = [t["id"] for t in tasks]
        self.assertNotIn(theirs, ids)
        self.assertEqual(set(ids), {done, live, kb})
        self.assertEqual(ids[-1], done)                                  # finished work sinks below the running
        self.assertEqual({t["kind"] for t in tasks}, {"agent", "subagent", "kb_ingest"})
        self.assertEqual({t["id"] for t in taskboard.list_tasks(ME, state="active")}, {live, kb})
        self.assertEqual([t["id"] for t in taskboard.list_tasks(ME, kind="kb_ingest")], [kb])
        self.assertEqual([t["id"] for t in taskboard.list_tasks(ME, state="completed")], [done])

    def test_titles_never_carry_a_prompt(self):
        r = rid()
        runs.begin(r, "media", ME, ref="image")
        self.assertEqual(taskboard.get_task(ME, r)["title"], "Media job - image")


class StopTests(unittest.TestCase):
    def tearDown(self):
        run_manager._RUNS.clear()

    def test_stop_cancels_a_detached_agent_run(self):
        r = rid()
        runs.begin(r, "agent", ME)
        h = run_manager.RunHandle(r, ME, 3)
        run_manager._RUNS[r] = h
        t = taskboard.get_task(ME, r)
        self.assertTrue(t["can_stop"] and t["can_attach"])
        self.assertEqual(t["attach"]["events"], f"/agent/run/{r}/events")
        res = taskboard.stop_task(ME, r)
        self.assertTrue(res["stopped"])
        self.assertTrue(h.cancel_requested and h.acked)

    def test_work_that_cannot_be_stopped_says_so(self):
        r = rid()
        runs.begin(r, "kb_ingest", ME)
        res = taskboard.stop_task(ME, r)
        self.assertEqual((res["ok"], res["stopped"]), (True, False))
        self.assertIn("cannot be stopped", res["note"])

    def test_finished_task_is_not_stopped(self):
        r = rid()
        runs.begin(r, "agent", ME)
        runs.end(r, "completed")
        self.assertIn("already finished", taskboard.stop_task(ME, r)["note"])

    def test_other_users_task_is_unknown(self):
        r = rid()
        runs.begin(r, "agent", OTHER)
        run_manager._RUNS[r] = run_manager.RunHandle(r, OTHER, None)
        self.assertIsNone(taskboard.get_task(ME, r))
        self.assertIsNone(taskboard.stop_task(ME, r))
        self.assertFalse(run_manager._RUNS[r].cancel_requested)

    def test_media_job_stop_goes_through_the_job(self):
        r = rid()
        runs.begin(f"media-{r}", "media", ME, ref="image")
        job = SimpleNamespace(done=False)
        with mock.patch("core.media.jobs.get_job", return_value=job), \
                mock.patch("core.media.jobs.cancel_job", return_value=True) as cancel:
            res = taskboard.stop_task(ME, f"media-{r}")
        self.assertTrue(res["stopped"])
        cancel.assert_called_once_with(r, ME)


class HttpTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(tb_routes.router)
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=ME)
        self.c = TestClient(app)

    def tearDown(self):
        run_manager._RUNS.clear()

    def test_list_get_stop_and_404_for_someone_elses(self):
        mine, theirs = rid(), rid()
        runs.begin(mine, "agent", ME)
        runs.begin(theirs, "agent", OTHER)
        run_manager._RUNS[mine] = run_manager.RunHandle(mine, ME, None)
        self.assertIn(mine, [t["id"] for t in self.c.get("/tasks?state=active").json()["tasks"]])
        self.assertEqual(self.c.get(f"/tasks/{mine}").json()["kind"], "agent")
        self.assertEqual(self.c.get(f"/tasks/{theirs}").status_code, 404)
        self.assertEqual(self.c.post(f"/tasks/{theirs}/stop").status_code, 404)
        with mock.patch.object(tb_routes, "audit_log") as audit:
            r = self.c.post(f"/tasks/{mine}/stop")
        self.assertTrue(r.json()["stopped"])
        audit.assert_called_once()
        self.assertEqual(self.c.get("/tasks/nope-nope").status_code, 404)


class ToolTests(unittest.TestCase):
    def setUp(self):
        set_current_user(ME)

    def tearDown(self):
        set_current_user(None)
        run_manager._RUNS.clear()

    def test_task_list_check_stop(self):
        r = rid()
        runs.begin(r, "agent", ME, stage="build")
        run_manager._RUNS[r] = run_manager.RunHandle(r, ME, None)
        out = run(tb_tools.tool_task_list({"state": "active"}))
        self.assertIn(r, out)
        self.assertIn("build", out)
        self.assertIn("can stop: yes", run(tb_tools.tool_task_check({"id": r})))
        self.assertTrue(run(tb_tools.tool_task_stop({"id": r})).startswith("stopped"))
        self.assertIn("unknown task id", run(tb_tools.tool_task_check({"id": "nope"})))

    def test_no_user_no_tasks(self):
        set_current_user(None)
        self.assertIn("no signed-in user", run(tb_tools.tool_task_list({})))

    def test_registers_under_the_tasks_source(self):
        from core.registry import registry
        tb_tools.register_task_tools()
        self.assertIsNotNone(registry.get("task_list"))


class SurfaceTests(unittest.TestCase):
    def test_tasks_family_is_on_demand(self):
        self.assertNotIn("tasks", tool_surface.needed_families("fix the failing test"))
        self.assertIn("tasks", tool_surface.needed_families("what is running in the background?"))
        tools = [{"function": {"name": "task_list"}}, {"function": {"name": "read_file"}}]
        names = lambda q: {t["function"]["name"] for t in tool_surface.filter_tools(tools, q)}
        self.assertEqual(names("fix the failing test"), {"read_file"})
        self.assertIn("task_list", names("show my background tasks"))


if __name__ == "__main__":
    unittest.main()
