"""tests/test_run_lifecycle.py - honest agent runs: heartbeat while waiting, in-app approval for device/browser actions.

Run: python -m unittest tests.test_run_lifecycle -v
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import companion_bridge, device_approval
from core.request_context import set_device_approved
from core.small_model import APP_CONFIG
from routes.agent import permissions as perms


def run(coro):
    return asyncio.run(coro)


async def collect(agen):
    return [item async for item in agen]


class PermissionStream(unittest.TestCase):
    def test_pings_while_waiting_then_reports_the_decision(self):
        async def go():
            ev = asyncio.Event()
            perms._perm_pending["r1"] = {"event": ev, "result": None, "user_id": 1}

            async def answer():
                await asyncio.sleep(0.12)
                perms._perm_pending["r1"]["result"] = {"allow": True, "decision": "project", "note": ""}
                ev.set()
            asyncio.ensure_future(answer())
            return await collect(perms._permission_stream("r1", ev, ping_s=0.03, wait_s=2))
        items = run(go())
        self.assertTrue(any(k == "ping" for k, _ in items))
        self.assertTrue(all(v == perms.PING for k, v in items if k == "ping"))
        kind, (allowed, note, decision) = items[-1]
        self.assertEqual((kind, allowed, decision), ("done", True, "project"))
        self.assertNotIn("r1", perms._perm_pending)

    def test_timeout_denies_and_cleans_up(self):
        async def go():
            ev = asyncio.Event()
            perms._perm_pending["r2"] = {"event": ev, "result": None, "user_id": 1}
            return await collect(perms._permission_stream("r2", ev, ping_s=0.02, wait_s=0.1))
        items = run(go())
        kind, (allowed, note, decision) = items[-1]
        self.assertEqual((kind, allowed, decision), ("done", False, "timeout"))
        self.assertIn("timed out", note)
        self.assertNotIn("r2", perms._perm_pending)

    def test_a_disconnect_mid_request_still_cleans_up(self):
        """Both pops used to sit inline after the ping yield, so GeneratorExit skipped both and
        the entry -- holding the full command or script text -- stayed in a module global for
        the life of the process. Nothing else ever reclaimed it."""
        async def go():
            ev = asyncio.Event()
            perms._perm_pending["r3"] = {"event": ev, "cmd": "rm -rf build", "result": None, "user_id": 1}
            g = perms._permission_stream("r3", ev, ping_s=0.02, wait_s=5)
            await g.__anext__()          # first ping: waiting on the user
            await g.aclose()             # the browser tab closed
        run(go())
        self.assertNotIn("r3", perms._perm_pending,
                         "an abandoned permission request must not stay in the global map")

    def test_many_abandoned_requests_leave_nothing_behind(self):
        async def go():
            for i in range(50):
                ev = asyncio.Event()
                perms._perm_pending[f"r{i}"] = {"event": ev, "cmd": "x" * 50, "result": None, "user_id": 1}
                g = perms._permission_stream(f"r{i}", ev, ping_s=0.01, wait_s=5)
                await g.__anext__()
                await g.aclose()
        run(go())
        self.assertEqual(perms._perm_pending, {})


class Keepalive(unittest.TestCase):
    def test_pings_during_a_slow_tool_and_returns_its_result(self):
        async def slow():
            await asyncio.sleep(0.12)
            return "result"
        items = run(collect(perms.keepalive(slow(), interval=0.03)))
        self.assertTrue(any(k == "ping" for k, _ in items))
        self.assertEqual(items[-1], ("done", "result"))

    def test_an_error_inside_the_tool_is_raised(self):
        async def boom():
            raise ValueError("x")
        with self.assertRaises(ValueError):
            run(collect(perms.keepalive(boom(), interval=0.03)))

    def test_closing_the_stream_cancels_the_tool(self):
        state = {"cancelled": False}

        async def slow():
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                state["cancelled"] = True
                raise

        async def go():
            g = perms.keepalive(slow(), interval=0.02)
            await g.__anext__()            # first ping: the tool is running
            await g.aclose()               # the client went away
            await asyncio.sleep(0.05)
        run(go())
        self.assertTrue(state["cancelled"])

    def test_cancelling_actually_awaits_the_task(self):
        """task.cancel() is a request, not a fact. keepalive used to fire it and move on, so
        a tool that ignored cancellation kept running on the device after the client left."""
        holder = {}

        async def stubborn():
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                holder["cleanup_started"] = True
                await asyncio.sleep(0.05)      # takes a moment to actually unwind
                holder["finished"] = True
                raise

        async def go():
            g = perms.keepalive(stubborn(), interval=0.02)
            await g.__anext__()
            await g.aclose()
            await asyncio.sleep(0.02)
            # inspect the task keepalive created, via a task left behind by the coroutine
            for t in asyncio.all_tasks():
                if t is not asyncio.current_task():
                    holder["task"] = t

        run(go())
        # the coroutine got far enough to start cleaning up, i.e. cancel was delivered...
        self.assertTrue(holder.get("cleanup_started"))
        # ...and by the time keepalive returned, nothing of it was still running
        task = holder.get("task")
        self.assertTrue(task is None or task.done(), "keepalive returned before the task finished")

    def test_a_tool_that_never_returns_is_cancelled_and_reported(self):
        """Unbounded before: a tool that never returned held its slot for the rest of the run,
        and the agent's wall-clock budget is only checked between steps."""
        cancelled = {"v": False}

        async def hang():
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled["v"] = True
                raise

        items = run(collect(perms.keepalive(hang(), interval=0.01, max_s=0.05)))
        kind, result = items[-1]
        self.assertEqual(kind, "done")
        self.assertTrue(result.startswith("error:"), result)
        self.assertIn("longer than", result)
        self.assertTrue(cancelled["v"])

    def test_the_timeout_message_never_carries_the_tool_arguments(self):
        # the args of e.g. run_python are user code; a cancellation notice goes to the model
        # and into history, so it must name the tool and nothing else
        async def hang():
            await asyncio.sleep(30)

        async def secret():
            await asyncio.sleep(30)

        items = run(collect(perms.keepalive(secret(), interval=0.01, max_s=0.05)))
        _k, result = items[-1]
        self.assertIn("secret", result)                  # the function name is fine
        self.assertNotIn("SECRET-CONTENT-9f3a", result)  # its arguments are not


class LocalClientTimeouts(unittest.TestCase):
    """The local llama-server clients were timeout=None. Nothing could end an in-flight
    generation: the agent's wall-clock budget is only checked between steps, so one wedged
    request held the SSE stream and a GPU slot for as long as it liked."""

    def _assert_bounded(self, client, label):
        t = client.timeout
        for name in ("connect", "read", "write", "pool"):
            self.assertIsNotNone(getattr(t, name), f"{label}.{name} is unbounded")
            self.assertGreater(getattr(t, name), 0, f"{label}.{name} must be positive")

    def test_main_lane_client_is_bounded(self):
        from core.state import ProxyState
        self._assert_bounded(ProxyState().client, "main")

    def test_small_model_client_is_bounded(self):
        from core.small_model.instance import SmallModelInstance
        inst = SmallModelInstance("executor", {"model": None, "port": 8099})
        try:
            self._assert_bounded(inst.client, "executor")
        finally:
            run(inst.client.aclose())

    def test_a_slow_but_progressing_stream_is_not_cut_off(self):
        """httpx applies read per chunk, so these timeouts only fire on SILENCE. That is what
        makes them safe to introduce: a long generation that is still producing tokens never
        trips one. Assert the semantic we rely on rather than just the number."""
        import httpx
        t = httpx.Timeout(5.0, read=600.0, write=30.0, pool=10.0)
        self.assertEqual(t.read, 600.0)
        self.assertNotEqual(t.read, None)


class BackgroundWorkIsReleased(unittest.TestCase):
    """Handles that were discarded: log-pump tasks, HTTP pools, the MCP connect task. Each
    one accumulated per load/reconfigure/boot and only died with the process."""

    def test_log_pumps_are_tracked_and_cancelled_on_stop(self):
        from core.state import ProxyState
        st = ProxyState()

        async def go():
            cancelled = []

            async def pump():
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    cancelled.append(1)
                    raise

            task = asyncio.create_task(pump())
            st._log_tasks.add(task)
            await asyncio.sleep(0)          # let it actually start, else cancel pre-empts it
            self.assertIn(task, st._log_tasks)
            st._stop_process_locked()          # no process: just the pump teardown
            await asyncio.sleep(0.02)
            return cancelled

        try:
            self.assertEqual(run(go()), [1])
            self.assertEqual(st._log_tasks, set(), "handles must be discarded once cancelled")
        finally:
            run(st.client.aclose())

    def test_stop_clears_the_profile_under_the_same_lock(self):
        """The route used to await stop() and then write state.profile itself, leaving a
        window where a concurrent request saw a cleared profile with a live process."""
        import inspect

        from core.state import ProxyState
        src = inspect.getsource(ProxyState.stop)
        self.assertIn("clear_profile", src)
        self.assertEqual(src.count("async with self.lock"), 1)
        self.assertLess(src.index("async with self.lock"), src.index("self.profile = None"),
                        "the clear must happen inside the lock, not after it")

    def test_the_stop_route_no_longer_writes_state_directly(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent / "routes" / "control" / "server_endpoints.py"
               ).read_text(encoding="utf-8")
        self.assertIn("state.stop(clear_profile=True)", src)
        self.assertNotIn("state.profile = None", src)

    def test_a_reconfigured_lane_releases_its_old_pool(self):
        from unittest import mock

        from core.small_model.manager import SmallModelManager
        mgr = SmallModelManager.__new__(SmallModelManager)   # skip __init__: no config needed
        old = mock.Mock()
        old.is_up.return_value = False
        mgr.instances = {"executor": old}
        closed = []
        mgr._close_later = closed.append
        with mock.patch.dict(APP_CONFIG["small_models"], {}, clear=False):
            mgr.reconfigure("executor", None)
        self.assertEqual(closed, [old], "the popped lane's client must be closed")
        self.assertNotIn("executor", mgr.instances)

    def test_aclose_all_reaches_every_lane(self):
        import asyncio as _a
        from unittest import mock

        from core.small_model.manager import SmallModelManager
        mgr = SmallModelManager.__new__(SmallModelManager)
        a, b = mock.AsyncMock(), mock.AsyncMock()
        mgr.instances = {"executor": a, "vision": b}
        _a.run(mgr.aclose_all())
        a.aclose.assert_awaited_once()
        b.aclose.assert_awaited_once()

    def test_the_mcp_connect_task_is_kept(self):
        """It spawns npx/uvx children for stdio servers, so on shutdown it has to be cancelled
        and awaited rather than left to fire while the loop is closing."""
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent / "core" / "startup.py").read_text(encoding="utf-8")
        self.assertIn("mcp_task = asyncio.create_task(connect_all_mcp())", src)
        # it must be in the same tuple the gather awaits, not just assigned
        gather_args = src.split("await asyncio.gather")[0].rsplit("background = (", 1)[-1]
        self.assertIn("mcp_task", gather_args)

    def test_shutdown_awaits_its_cancellations_and_closes_clients(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent / "core" / "startup.py").read_text(encoding="utf-8")
        self.assertIn("return_exceptions=True", src)
        self.assertIn("await state.client.aclose()", src)
        self.assertIn("await small_models.aclose_all()", src)


class StepErrorIsSanitised(unittest.TestCase):
    """A transport error used to lose the run at whatever step it happened on, and the raw
    exception went to the browser -- httpx errors carry local paths and driver messages."""

    def test_a_wedge_is_reported_as_a_timeout_the_user_can_act_on(self):
        import httpx
        from routes.agent.run import _classify_step_error
        msg, kind = _classify_step_error(httpx.ReadTimeout("stalled"))
        self.assertEqual(kind, "timeout")
        self.assertIn("stopped responding", msg)
        self.assertIn("try again", msg)

    def test_a_lost_local_server_says_so(self):
        import httpx
        from routes.agent.run import _classify_step_error
        msg, kind = _classify_step_error(httpx.ConnectError("refused"))
        self.assertEqual(kind, "error")
        self.assertIn("connection to the local model server", msg)

    def test_no_transport_detail_reaches_the_client(self):
        import httpx
        from routes.agent.run import _classify_step_error
        leaky = "couldn't connect to E:/AI/vulkan-arc/llama-vulkan/llama-server.exe on port 8090"
        for err in (httpx.ConnectError(leaky), httpx.ReadTimeout(leaky),
                    httpx.RemoteProtocolError(leaky), OSError(leaky)):
            msg, _kind = _classify_step_error(err)
            for secret in ("E:/AI", "llama-vulkan", "8090", "llama-server.exe"):
                self.assertNotIn(secret, msg, f"{type(err).__name__} leaked {secret}")

    def test_an_unknown_error_gives_no_detail_either(self):
        from routes.agent.run import _classify_step_error
        msg, kind = _classify_step_error(RuntimeError("secret internal detail at /etc/app.ini"))
        self.assertEqual(kind, "error")
        self.assertIn("RuntimeError", msg)
        self.assertNotIn("secret internal detail", msg)
        self.assertNotIn("/etc/app.ini", msg)


class VerifierReviseIsBounded(unittest.TestCase):
    def test_revise_has_a_finite_timeout(self):
        import inspect

        from core.verifier.engine import revise
        src = inspect.getsource(revise)
        self.assertIn("REVISE_TIMEOUT_S", src)
        self.assertNotIn("timeout=None", src)

    def test_the_budget_is_a_real_number(self):
        from core.verifier.constants import REVISE_TIMEOUT_S, VERIFY_TIMEOUT_S
        self.assertGreater(REVISE_TIMEOUT_S, 0)
        self.assertGreaterEqual(REVISE_TIMEOUT_S, VERIFY_TIMEOUT_S)


class DeviceApproval(unittest.TestCase):
    def test_local_dev_servers_need_no_question(self):
        for url in ("http://localhost:8080/index.html", "http://127.0.0.1:3000", "http://app.localhost/x", "http://[::1]:5173/"):
            self.assertIsNone(device_approval.request_for("browser_navigate", {"url": url}), url)

    def test_other_sites_are_asked_once_per_origin_key(self):
        r = device_approval.request_for("browser_navigate", {"url": "https://example.com/a/b?c=1"})
        self.assertEqual(r["kind"], device_approval.KIND_OPEN)
        self.assertEqual(r["key"], "https://example.com")
        self.assertIn("https://example.com", r["text"])

    def test_page_script_shows_the_script(self):
        r = device_approval.request_for("browser_eval", {"expression": "document.title"})
        self.assertEqual(r["kind"], device_approval.KIND_EVAL)
        self.assertIn("document.title", r["text"])
        self.assertIsNone(device_approval.request_for("browser_eval", {"expression": "  "}))

    def test_device_actions_and_plain_tools(self):
        self.assertEqual(device_approval.request_for("mobile_install", {"path": "app.apk"})["kind"], device_approval.KIND_DEVICE)
        self.assertEqual(device_approval.request_for("mobile_boot", {"avd": "Pixel_7"})["kind"], device_approval.KIND_DEVICE)
        self.assertIsNone(device_approval.request_for("mobile_connect", {}))
        for name in ("read_file", "browser_snapshot", "mobile_tap", "run_shell"):
            self.assertIsNone(device_approval.request_for(name, {"url": "https://x.com"}), name)


class ApprovedFlag(unittest.TestCase):
    def setUp(self):
        self.sent = []

        async def fake_once(uid, op, params, timeout):
            self.sent.append((op, dict(params)))
            return {}
        p = mock.patch.object(companion_bridge.rpc, "_call_once", fake_once)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(set_device_approved, False)

    def test_flag_is_added_only_for_confirmable_ops_and_only_when_approved(self):
        async def go():
            set_device_approved(True)
            await companion_bridge.call(1, "browser.eval", {"expression": "1"})
            await companion_bridge.call(1, "browser.snapshot", {})
            set_device_approved(False)
            await companion_bridge.call(1, "browser.eval", {"expression": "2"})
        run(go())
        self.assertTrue(self.sent[0][1].get("approved_in_app"))
        self.assertNotIn("approved_in_app", self.sent[1][1])      # not a confirmable op
        self.assertNotIn("approved_in_app", self.sent[2][1])      # not approved in the app


class RunWiring(unittest.TestCase):
    """routes/agent/run.py is one big generator with no end-to-end harness: pin the wiring in source."""

    src = Path("routes/agent/run.py").read_text(encoding="utf-8")

    def test_every_terminal_event_carries_a_state(self):
        import re
        found = list(re.finditer(r"event: done\\ndata: (.*?)\\n\\n", self.src))    # the source holds a backslash + n
        self.assertGreaterEqual(len(found), 5)
        for m in found:
            self.assertIn("state", m.group(1), m.group(0)[:90])

    def test_permission_waits_and_tools_keep_the_stream_alive(self):
        self.assertNotIn("await _await_permission(", self.src)
        self.assertGreaterEqual(self.src.count("_permission_stream(preq_id, ev)"), 4)   # shell, python, media, device
        self.assertIn("keepalive(run_tool(", self.src)

    def test_an_open_tool_call_is_closed_when_the_run_fails(self):
        self.assertIn("pending_tool", self.src)
        self.assertIn("'interrupted': True", self.src)
        self.assertIn("\"tool_start\"", self.src)

    def test_hooks_cannot_drop_a_tool_result(self):
        i = self.src.index('await fire_hook("after_tool"')
        self.assertIn("try:", self.src[i - 60:i])


if __name__ == "__main__":
    unittest.main()
