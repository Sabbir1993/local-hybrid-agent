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
