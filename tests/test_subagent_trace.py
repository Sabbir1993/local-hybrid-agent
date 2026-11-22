"""Tests for Subagent Execution Tracing and Diagnostics.

Run: python -m unittest tests.test_subagent_trace -v
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.subagent import (
    get_subagent_traces,
    clear_subagent_traces,
    run_parallel_subagents,
    tool_spawn_agent,
    tool_spawn_parallel_agents,
    run_subagent,
)


class SubagentTraceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        clear_subagent_traces()

    def tearDown(self):
        clear_subagent_traces()

    async def test_trace_out_captured_in_run_subagent(self):
        trace = {}
        # Call run_subagent with an empty task -> should record failure_reason
        res = await run_subagent("  ", trace_out=trace)
        self.assertTrue(res.startswith("error:"))
        self.assertEqual(trace["status"], "error")
        self.assertIn("Task is required", trace["failure_reason"])

    async def test_trace_out_invalid_role(self):
        trace = {}
        res = await run_subagent("Inspect code", role="non_existent_role_xyz", trace_out=trace)
        self.assertTrue(res.startswith("error:"))
        self.assertEqual(trace["status"], "error")
        self.assertIn("Unknown role", trace["failure_reason"])

    async def test_parallel_subagents_captures_traces_in_contextvar(self):
        async def _mock_run(task, role=None, lane_override=None, tool_allowlist=None, max_steps=None, trace_out=None):
            if trace_out is not None:
                trace_out.update({
                    "id": f"sub_test_{role}",
                    "role": role or "generic",
                    "task": task,
                    "lane": "main",
                    "max_steps": max_steps or 8,
                    "status": "success" if "ok" in task else "step_limit",
                    "failure_reason": None if "ok" in task else "Reached maximum step limit (8/8) without completing the task.",
                    "steps": [
                        {
                            "step": 1,
                            "thought": "Reading files...",
                            "tools": [{"name": "read_file", "args": {"path": "test.py"}, "result": "content", "ok": True}],
                            "duration_s": 0.5,
                        }
                    ],
                    "files_modified": [],
                    "final_content": f"Result for {task}",
                    "duration_s": 1.2,
                })
            return f"[sub-agent · role={role} · 4 msgs · lane=main · status={'success' if 'ok' in task else 'step_limit'}]\nResult for {task}"

        with mock.patch("core.subagent.runner.run_subagent", side_effect=_mock_run):
            specs = [
                {"task": "Task ok", "role": "reviewer"},
                {"task": "Task fail", "role": "reviewer", "max_steps": 8},
            ]
            res = await run_parallel_subagents(specs)
            self.assertIn("[parallel sub-agents · 2 spawned concurrently]", res)

            traces = get_subagent_traces()
            self.assertEqual(len(traces), 2)
            self.assertEqual(traces[0]["role"], "reviewer")
            self.assertEqual(traces[0]["status"], "success")
            self.assertIsNone(traces[0]["failure_reason"])
            self.assertEqual(len(traces[0]["steps"]), 1)

            self.assertEqual(traces[1]["role"], "reviewer")
            self.assertEqual(traces[1]["status"], "step_limit")
            self.assertIn("Reached maximum step limit", traces[1]["failure_reason"])

    async def test_tool_spawn_agent_captures_trace(self):
        async def _mock_run(task, role=None, lane_override=None, tool_allowlist=None, max_steps=None, trace_out=None):
            if trace_out is not None:
                trace_out.update({
                    "id": "sub_single",
                    "role": role or "explorer",
                    "task": task,
                    "status": "success",
                    "steps": [],
                })
            return f"[sub-agent · role={role} · 2 msgs · status=success]\nAll good"

        with mock.patch("core.subagent.runner.run_subagent", side_effect=_mock_run):
            res = await tool_spawn_agent({"task": "Explore repo", "role": "explorer"})
            self.assertIn("All good", res)
            traces = get_subagent_traces()
            self.assertEqual(len(traces), 1)
            self.assertEqual(traces[0]["role"], "explorer")
            self.assertEqual(traces[0]["status"], "success")


if __name__ == "__main__":
    unittest.main()
