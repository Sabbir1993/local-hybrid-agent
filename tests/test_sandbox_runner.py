"""tests/test_sandbox_runner.py - Unit tests for pluggable sandbox execution backends."""

import asyncio
import base64
import os
import unittest
from unittest import mock

from core.sandbox.runners import BaseRunner, LocalJobRunner, RemoteRunner, WSL2Runner, get_runner


class TestLocalJobRunner(unittest.TestCase):
    def test_local_job_runner_simple_execution(self):
        async def _test():
            runner = LocalJobRunner()
            code = "print('hello from sandbox')\nwith open('output.txt', 'w') as f: f.write('sandbox data')"
            res = await runner.execute(code, inputs=[], timeout_s=10)
            self.assertTrue(res["ok"])
            self.assertEqual(res["exit_code"], 0)
            self.assertIn("hello from sandbox", res["stdout"])
            self.assertEqual(len(res["files"]), 1)
            self.assertEqual(res["files"][0]["name"], "output.txt")
            content = base64.b64decode(res["files"][0]["b64"]).decode("utf-8")
            self.assertEqual(content, "sandbox data")

        asyncio.run(_test())

    def test_local_job_runner_timeout(self):
        async def _test():
            runner = LocalJobRunner()
            code = "import time\ntime.sleep(5)\nprint('done')"
            res = await runner.execute(code, inputs=[], timeout_s=1)
            self.assertFalse(res["ok"])
            self.assertTrue(res["timed_out"])
            self.assertEqual(res["exit_code"], -1)

        asyncio.run(_test())


class TestSandboxRunnerFactory(unittest.TestCase):
    def test_get_runner_by_env(self):
        with mock.patch.dict(os.environ, {"SANDBOX_BACKEND": "local"}):
            runner = get_runner()
            self.assertIsInstance(runner, LocalJobRunner)

        with mock.patch.dict(os.environ, {"SANDBOX_BACKEND": "wsl2"}):
            runner = get_runner()
            self.assertIsInstance(runner, WSL2Runner)

        with mock.patch.dict(os.environ, {"SANDBOX_BACKEND": "remote"}):
            runner = get_runner()
            self.assertIsInstance(runner, RemoteRunner)


if __name__ == "__main__":
    unittest.main()
