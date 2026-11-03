"""A tool that fails with SQLite's 'database is locked' is run again before the model sees an error."""
import asyncio
import unittest
from unittest import mock

import core.agent_tools  # noqa: F401  (import order)
from core.agent_loop import execution


class LockRetryTests(unittest.TestCase):
    def run_it(self, results):
        calls = []

        async def fake(name, args):
            calls.append(1)
            return results[min(len(calls) - 1, len(results) - 1)]

        async def no_sleep(_):
            return None
        with mock.patch.object(execution, "_dispatch", fake), mock.patch.object(execution.asyncio, "sleep", no_sleep), \
                mock.patch.object(execution, "tool_allowed", return_value=True), \
                mock.patch.object(execution, "validate_and_repair_tool_args", side_effect=lambda n, a: (a, None)), \
                mock.patch.object(execution.schema_check, "check_required", return_value=None), \
                mock.patch.object(execution, "fast_sandbox_check", return_value=(True, "")), \
                mock.patch.object(execution, "personal_scope", return_value=False):
            return asyncio.run(execution.run_tool("write_file", {"path": "a.html", "content": "x"})), len(calls)

    def test_second_try_succeeds(self):
        res, n = self.run_it(["error: database is locked", "Wrote 1 chars"])
        self.assertEqual((res, n), ("Wrote 1 chars", 2))

    def test_gives_up_after_three_tries(self):
        res, n = self.run_it(["error: database is locked"])
        self.assertEqual((res, n), ("error: database is locked", 3))

    def test_other_errors_are_not_retried(self):
        res, n = self.run_it(["error: path required"])
        self.assertEqual((res, n), ("error: path required", 1))


if __name__ == "__main__":
    unittest.main()
