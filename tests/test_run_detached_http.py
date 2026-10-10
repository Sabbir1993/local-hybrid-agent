"""tests/test_run_detached_http.py - a run survives its connection, over real HTTP.

Starlette's TestClient buffers whole responses, so it cannot show a client that leaves mid-run. This starts the
hermetic mock app under a real uvicorn (one thread, random port) and talks to it with a real HTTP client:
the connection is dropped while a tool is still running, and the run is then found finished, intact and
replayable. The model is scripted and the companion is the temp-dir fake from tests/eval_mock.py.

Run: python -m unittest tests.test_run_detached_http -v
"""

import asyncio
import json
import queue
import socket
import sys
import threading
import time
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import eval_mock as em  # noqa: E402
from core.registry import registry  # noqa: E402
from routes.agent import run_manager  # noqa: E402

HEADERS = {"User-Agent": "A770NativeApp/1.0", "X-Device-Id": em.DEVICE}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def parse(lines):
    """[(seq|None, event, data)] from SSE lines."""
    out, seq, event = [], None, None
    for line in lines:
        line = line.strip()
        if line.startswith("id: "):
            seq = int(line[4:])
        elif line.startswith("event: "):
            event = line[7:]
        elif line.startswith("data: ") and event:
            try:
                out.append((seq, event, json.loads(line[6:])))
            except ValueError:
                pass
            seq, event = None, None
    return out


class Base(unittest.TestCase):
    slow_s = 1.2

    def setUp(self):
        run_manager.clear()
        self.tool_started = threading.Event()
        self.tool_cancelled = threading.Event()
        self.tool_finished = threading.Event()

        async def slow(args):
            self.tool_started.set()
            try:
                await asyncio.sleep(self.slow_s)
                self.tool_finished.set()
                return "slow result"
            except asyncio.CancelledError:
                self.tool_cancelled.set()
                raise

        registry.register("mcp__slow__wait", slow, {"type": "function", "function": {
            "name": "mcp__slow__wait", "description": "[mcp:slow] wait",
            "parameters": {"type": "object", "properties": {}}}},
            source="mcp:slow", meta={"label": "slow/wait", "read_only": True}, replace=True)
        self.env = em.MockEvalEnv().__enter__()
        import uvicorn
        self.port = free_port()
        cfg = uvicorn.Config(self.env.client.app, host="127.0.0.1", port=self.port, log_level="error", lifespan="off")
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                break
            time.sleep(0.05)
        self.assertTrue(self.server.started, "test server did not start")
        self.base = f"http://127.0.0.1:{self.port}"
        em._current["fifo"] = queue.Queue()
        for turn in [em.T(tool_calls=[em.TC("mcp__slow__wait")]), em.T(content="All done.")]:
            em._current["fifo"].put(dict(turn))
        em._current["extra_calls"] = 0
        em._current["requests"] = 0

    def tearDown(self):
        run_manager.clear()
        self.server.should_exit = True
        self.thread.join(5)
        registry.unregister_source("mcp:slow")
        self.env.__exit__(None, None, None)

    def payload(self, **extra):
        return {"messages": [{"role": "user", "content": "Use the slow connector once, then answer."}],
                "mode": "all-local", "max_steps": 6, "temperature": 0, "session_id": self.env.sid, **extra}

    def runs(self):
        return httpx.get(f"{self.base}/agent/runs", params={"session_id": self.env.sid}, headers=HEADERS).json()["runs"]

    def wait_for(self, pred, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            if pred():
                return True
            time.sleep(0.05)
        return False

    def read_until(self, resp, event_name):
        seen = []
        for line in resp.iter_lines():
            seen.append(line)
            if line.strip() == f"event: {event_name}":
                break
        return seen


class ClosingTheConnection(Base):
    def test_the_run_finishes_without_its_client_and_can_be_replayed(self):
        with httpx.stream("POST", f"{self.base}/agent/run", json=self.payload(client_run_id="client-run-0001"),
                          headers=HEADERS, timeout=60) as resp:
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.headers.get("x-run-id"), "client-run-0001", "the client's id is the run's id")
            self.read_until(resp, "tool_call")
        # the connection is closed here, while the tool is still running
        self.assertTrue(self.tool_started.wait(5), "the tool started")
        rows = self.runs()
        self.assertEqual([r["id"] for r in rows], ["client-run-0001"])
        self.assertTrue(rows[0]["running"], "still running after the client left")

        self.assertTrue(self.wait_for(lambda: not self.runs()[0]["running"]), "the run ended on its own")
        self.assertTrue(self.tool_finished.is_set(), "the tool ran to completion, it was not cancelled")
        self.assertFalse(self.tool_cancelled.is_set())
        self.assertEqual(self.runs()[0]["state"], "completed")

        # a client that comes back later gets the whole story
        full = httpx.get(f"{self.base}/agent/run/client-run-0001/events", headers=HEADERS, timeout=30)
        events = parse(full.text.splitlines())
        names = [e for _s, e, _d in events]
        self.assertEqual(names[0], "run")
        self.assertEqual(names[-1], "done")
        self.assertIn("tool_call", names)
        self.assertIn("All done.", "".join(d.get("text", "") for _s, e, d in events if e == "delta"))
        self.assertEqual([s for s, _e, _d in events], list(range(len(events))), "contiguous ids from 0")

        # ... and can resume from where it left off
        last = events[len(events) // 2][0]
        part = parse(httpx.get(f"{self.base}/agent/run/client-run-0001/events", params={"after": last},
                               headers=HEADERS, timeout=30).text.splitlines())
        self.assertEqual([s for s, _e, _d in part], list(range(last + 1, len(events))))

        # acknowledging frees it
        self.assertTrue(httpx.post(f"{self.base}/agent/run/client-run-0001/ack", headers=HEADERS).json()["known"])
        self.assertEqual(self.runs(), [])
        self.assertEqual(httpx.get(f"{self.base}/agent/run/client-run-0001/events", headers=HEADERS).status_code, 404)

    def test_reattaching_while_it_is_still_running_follows_it_live(self):
        with httpx.stream("POST", f"{self.base}/agent/run", json=self.payload(client_run_id="client-run-0002"),
                          headers=HEADERS, timeout=60) as resp:
            self.read_until(resp, "tool_call")
        self.assertTrue(self.tool_started.wait(5))
        t0 = time.time()
        with httpx.stream("GET", f"{self.base}/agent/run/client-run-0002/events", headers=HEADERS, timeout=60) as r2:
            events = parse(list(r2.iter_lines()))
        self.assertEqual(events[-1][1], "done", "followed to the end")
        self.assertGreater(time.time() - t0, 0.3, "it waited for the live run, not just a snapshot")
        self.assertEqual(events[0][0], 0, "from the beginning")


class Ownership(Base):
    slow_s = 30

    def test_another_user_cannot_list_replay_or_cancel_a_run(self):
        from core import deps as deps_mod
        from core import request_context as rc
        with httpx.stream("POST", f"{self.base}/agent/run", json=self.payload(client_run_id="client-run-0005"),
                          headers=HEADERS, timeout=60) as resp:
            self.read_until(resp, "tool_call")
        self.assertTrue(self.tool_started.wait(5))
        app = self.env.client.app
        mine = app.dependency_overrides[deps_mod.get_current_user]

        async def intruder():
            rc.set_current_user(em.UID + 1)
            rc.set_current_device(em.DEVICE)
            p = em._principal()
            p.id = em.UID + 1
            return p
        app.dependency_overrides[deps_mod.get_current_user] = intruder
        try:
            self.assertEqual(self.runs(), [], "not listed")
            self.assertEqual(httpx.get(f"{self.base}/agent/run/client-run-0005/events", headers=HEADERS).status_code, 404)
            self.assertEqual(httpx.post(f"{self.base}/agent/run/client-run-0005/cancel", headers=HEADERS).status_code, 404)
            self.assertEqual(httpx.post(f"{self.base}/agent/run/client-run-0005/ack", headers=HEADERS).json()["known"], False)
        finally:
            app.dependency_overrides[deps_mod.get_current_user] = mine
        self.assertTrue(self.runs()[0]["running"], "the owner's run was not touched")
        httpx.post(f"{self.base}/agent/run/client-run-0005/cancel", headers=HEADERS)


class Stopping(Base):
    slow_s = 30

    def test_cancel_stops_the_run_and_its_running_tool(self):
        with httpx.stream("POST", f"{self.base}/agent/run", json=self.payload(client_run_id="client-run-0003"),
                          headers=HEADERS, timeout=60) as resp:
            self.read_until(resp, "tool_call")
        self.assertTrue(self.tool_started.wait(5))
        self.assertTrue(self.runs()[0]["running"], "closing the tab did not stop it")

        r = httpx.post(f"{self.base}/agent/run/client-run-0003/cancel", headers=HEADERS)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["cancelled"])
        self.assertTrue(self.tool_cancelled.wait(5), "the tool that was running was cancelled too")
        # a run the user stopped has nothing left to replay: once it has wound down it is forgotten
        self.assertTrue(self.wait_for(lambda: self.runs() == [], 10), "the stopped run wound down and was released")
        self.assertEqual(httpx.get(f"{self.base}/agent/run/client-run-0003/events", headers=HEADERS).status_code, 404)
        again = httpx.post(f"{self.base}/agent/run/client-run-0003/cancel", headers=HEADERS)
        self.assertEqual(again.status_code, 404, "stopping a run that is already gone is harmless")

    def test_unknown_runs_are_404(self):
        self.assertEqual(httpx.post(f"{self.base}/agent/run/nope-nope-nope/cancel", headers=HEADERS).status_code, 404)
        self.assertEqual(httpx.get(f"{self.base}/agent/run/nope-nope-nope/events", headers=HEADERS).status_code, 404)


class Limits(Base):
    slow_s = 30

    def test_too_many_runs_is_refused_before_anything_starts(self):
        run_manager.APP_CONFIG.setdefault("agent", {})["max_active_runs_per_user"] = 1
        try:
            with httpx.stream("POST", f"{self.base}/agent/run", json=self.payload(client_run_id="client-run-0004"),
                              headers=HEADERS, timeout=60) as resp:
                self.read_until(resp, "tool_call")
            self.assertTrue(self.tool_started.wait(5))
            second = httpx.post(f"{self.base}/agent/run", json=self.payload(), headers=HEADERS, timeout=30)
            self.assertEqual(second.status_code, 429)
            self.assertEqual(second.json()["error"], "too_many_runs")
            self.assertEqual(len(self.runs()), 1)
        finally:
            run_manager.APP_CONFIG["agent"].pop("max_active_runs_per_user", None)
            httpx.post(f"{self.base}/agent/run/client-run-0004/cancel", headers=HEADERS)


if __name__ == "__main__":
    unittest.main()
