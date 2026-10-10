"""tests/test_run_golden.py - the agent run's event stream, pinned.

The loop in routes/agent/stream.py is one 1,900-line generator, and refactors of it have silently dropped
behaviour before. Each scripted run below is recorded as a normalized trace (event names in order, tool names
and outcomes, the final text, how it ended) and compared with tests/golden/run_traces.json. Whatever wraps or
reshapes the run (the detached-run manager, a later split of the loop) must leave these traces identical.

Regenerate after an INTENDED behaviour change:  UPDATE_GOLDEN=1 python -m unittest tests.test_run_golden
Run: python -m unittest tests.test_run_golden -v
"""

import json
import os
import queue
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import eval_mock as em  # noqa: E402

GOLDEN = Path(__file__).resolve().parent / "golden" / "run_traces.json"
TASKS = ("compute_only", "read_after_write", "edit_flow", "append_build", "grep_search", "symbol_navigation",
         "unknown_tool_graceful", "missing_arg_recovery", "json_repair_truncated")


def capture(task: dict, client_headers=None) -> list:
    """Run one scripted task through POST /agent/run and return its raw (event, data) pairs."""
    events = []
    with em.MockEvalEnv() as env:
        em._current["fifo"] = queue.Queue()
        for turn in task["script"]:
            em._current["fifo"].put(dict(turn))
        em._current["extra_calls"] = 0
        em._current["requests"] = 0
        for rel, content in (task.get("seed_files") or {}).items():
            p = env.ws / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        payload = {"messages": [{"role": "user", "content": task["prompt"]}], "mode": "all-local",
                   "max_steps": task.get("max_steps", 12), "temperature": 0, "session_id": env.sid}
        headers = {"User-Agent": "A770NativeApp/1.0", "X-Device-Id": em.DEVICE, **(client_headers or {})}
        with env.client.stream("POST", "/agent/run", json=payload, headers=headers, timeout=120) as resp:
            assert resp.status_code == 200, resp.read()[:200]
            event = None
            for line in resp.iter_lines():
                line = line.strip() if isinstance(line, str) else line.decode("utf-8", "replace").strip()
                if line.startswith("event: "):
                    event = line[7:].strip()
                elif line.startswith("data: ") and event:
                    try:
                        events.append((event, json.loads(line[6:])))
                    except ValueError:
                        events.append((event, {}))
    return events


def normalize(events: list) -> list:
    """Stable view: what the run did, not when or with which random ids."""
    out = []
    for event, data in events:
        d = data if isinstance(data, dict) else {}
        if event == "delta":
            if out and out[-1][0] == "delta":
                out[-1][1]["text"] += d.get("text") or ""
            else:
                out.append(["delta", {"text": d.get("text") or ""}])
        elif event == "tool_call":
            out.append(["tool_call", {"name": d.get("name"), "args": sorted((d.get("args") or {}).keys())}])
        elif event == "tool_result":
            out.append(["tool_result", {"name": d.get("name"), "ok": d.get("ok")}])
        elif event == "done":
            out.append(["done", {"state": d.get("state"), "reason": d.get("reason")}])
        else:
            out.append([event, {}])
    return out


def task_by_name(name: str) -> dict:
    return next(t for t in em.MOCK_TASKS if t["name"] == name)


def record_all() -> dict:
    return {n: normalize(capture(task_by_name(n))) for n in TASKS}


class GoldenTraces(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get("UPDATE_GOLDEN"):
            GOLDEN.parent.mkdir(parents=True, exist_ok=True)
            GOLDEN.write_text(json.dumps(record_all(), indent=1, sort_keys=True) + "\n", encoding="utf-8")
        cls.golden = json.loads(GOLDEN.read_text(encoding="utf-8"))

    def test_every_pinned_task_exists(self):
        self.assertEqual(set(self.golden), set(TASKS))

    def test_traces_are_unchanged(self):
        for name in TASKS:
            with self.subTest(task=name):
                self.assertEqual(normalize(capture(task_by_name(name))), self.golden[name],
                                 f"the event stream of '{name}' changed; if intended, rerun with UPDATE_GOLDEN=1")

    def test_a_trace_is_deterministic(self):
        a = normalize(capture(task_by_name("edit_flow")))
        b = normalize(capture(task_by_name("edit_flow")))
        self.assertEqual(a, b, "a flaky trace would make every comparison above meaningless")

    def test_traces_are_not_trivially_empty(self):
        for name, trace in self.golden.items():
            with self.subTest(task=name):
                names = [e for e, _ in trace]
                self.assertIn("done", names)
                self.assertGreater(len(trace), 3)


if __name__ == "__main__":
    unittest.main()
