"""Tests for concurrent multi-agent parallel execution.

Run: python -m unittest tests.test_parallel_subagent -v
"""

import asyncio
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.subagent import (  # noqa: E402
    DENIED_TOOLS,
    SPAWN_PARALLEL_AGENTS_SCHEMA,
    run_parallel_subagents,
    tool_spawn_parallel_agents,
)


class ParallelSubagentSchemaTests(unittest.TestCase):
    def test_schema_structure(self):
        fn = SPAWN_PARALLEL_AGENTS_SCHEMA["function"]
        self.assertEqual(fn["name"], "spawn_parallel_agents")
        props = fn["parameters"]["properties"]
        self.assertIn("agents", props)
        self.assertEqual(props["agents"]["type"], "array")
        self.assertIn("agents", fn["parameters"]["required"])

    def test_denied_from_recursive_subagents(self):
        self.assertIn("spawn_parallel_agents", DENIED_TOOLS)


class ParallelSubagentExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_agents_list_rejected(self):
        res = await tool_spawn_parallel_agents({})
        self.assertTrue(res.startswith("error:"))

        res2 = await tool_spawn_parallel_agents({"agents": []})
        self.assertTrue(res2.startswith("error:"))

        res3 = await run_parallel_subagents([])
        self.assertTrue(res3.startswith("error:"))

    async def test_concurrent_execution_timing(self):
        """Verify that multiple subagents execute concurrently in parallel rather than serially."""
        call_times = []

        async def _mock_run(task, role=None, lane_override=None, tool_allowlist=None, max_steps=None):
            t_start = time.perf_counter()
            await asyncio.sleep(0.08)
            call_times.append((task, t_start, time.perf_counter()))
            return f"Completed audit for {task}"

        with mock.patch("core.subagent.runner.run_subagent", side_effect=_mock_run):
            specs = [
                {"task": "Audit agent loop core", "role": "explorer"},
                {"task": "Audit local model runtime", "role": "explorer"},
                {"task": "Audit verifier module", "role": "explorer"},
            ]
            t0 = time.perf_counter()
            res = await run_parallel_subagents(specs, max_concurrency=4)
            elapsed = time.perf_counter() - t0

            # 3 tasks * 0.08s serially = ~0.24s. In parallel it should take ~0.08s - 0.15s.
            self.assertLess(elapsed, 0.22, f"Expected parallel execution, took {elapsed:.2f}s")
            self.assertEqual(len(call_times), 3)

            # Check that tasks overlapped in execution
            starts = [t[1] for t in call_times]
            self.assertLess(max(starts) - min(starts), 0.05, "All subagents should have started at roughly the same time")

            # Verify combined result contains each subagent output
            self.assertIn("[parallel sub-agents · 3 spawned concurrently]", res)
            self.assertIn("Audit agent loop core", res)
            self.assertIn("Audit local model runtime", res)
            self.assertIn("Audit verifier module", res)

    async def test_fault_isolation(self):
        """If one subagent fails or throws, other subagents must still finish successfully."""
        async def _mock_run(task, role=None, lane_override=None, tool_allowlist=None, max_steps=None):
            if "failing" in task:
                raise RuntimeError("Hardware connection dropped")
            return f"OK: {task}"

        with mock.patch("core.subagent.runner.run_subagent", side_effect=_mock_run):
            specs = [
                {"task": "healthy subagent 1", "role": "coder"},
                {"task": "failing subagent", "role": "tester"},
                {"task": "healthy subagent 2", "role": "reviewer"},
            ]
            res = await run_parallel_subagents(specs)
            self.assertIn("OK: healthy subagent 1", res)
            self.assertIn("error: RuntimeError: Hardware connection dropped", res)
            self.assertIn("OK: healthy subagent 2", res)


if __name__ == "__main__":
    unittest.main()
