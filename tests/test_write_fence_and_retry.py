import asyncio
import unittest
from pathlib import Path
from unittest import mock

from core.agent_tools import file_ops


class WriteFenceAndRetry(unittest.TestCase):
    def setUp(self):
        self.mem = {}
        file_ops._SYNTAX_FAILS.clear()
        self.patches = [
            mock.patch.object(file_ops, "_resolve", side_effect=lambda a: (1, Path(a))),
            mock.patch.object(file_ops, "_read_existing", side_effect=self._read),
            mock.patch.object(file_ops, "_store", side_effect=self._store),
            mock.patch.object(file_ops, "_personal_protect", return_value=None),
            mock.patch.object(file_ops, "verify_after", return_value=""),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    async def _read(self, uid, p):
        return self.mem.get(str(p))

    async def _store(self, uid, p, before, after):
        self.mem[str(p)] = after

    def write(self, path, content):
        return asyncio.run(file_ops.tool_write_file({"path": path, "content": content}))

    def test_a_whole_file_fence_is_removed_before_saving(self):
        out = self.write("auth_test.py", "```python\nimport os\nprint(os.name)\n```")
        self.assertTrue(out.startswith("wrote"), out)
        self.assertEqual(self.mem["auth_test.py"], "import os\nprint(os.name)\n")

    def test_a_fence_with_prose_around_it_is_left_alone(self):
        self.assertEqual(file_ops._unwrap_fence("Here it is:\n```python\nx = 1\n```\nDone"),
                         "Here it is:\n```python\nx = 1\n```\nDone")

    def test_repeated_syntax_failures_tell_the_model_to_stop(self):
        bad = "this is not python ((("
        results = [self.write("auth_test.py", bad) for _ in range(3)]
        self.assertIn("pre-save syntax validation failed", results[0])
        self.assertIn("Stop writing it", results[2])
        self.assertNotIn("auth_test.py", self.mem)

    def test_a_good_write_clears_the_failure_count(self):
        self.write("auth_test.py", "((( bad")
        self.write("auth_test.py", "x = 1\n")
        self.assertEqual(file_ops._SYNTAX_FAILS, {})


if __name__ == "__main__":
    unittest.main()
