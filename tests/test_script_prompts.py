"""tests/test_script_prompts.py - fewer run_python approval prompts.

Run: python -m unittest tests.test_script_prompts -v
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.agent_loop import script_nudge
from core.agent_loop.executor_view import _CONTROL_PREFIXES
from core.agent_tools import limits

RUN = "\n".join(p.read_text(encoding="utf-8")
                for p in (ROOT / "routes" / "agent" / "run.py",
                          ROOT / "routes" / "agent" / "stream.py"))
JS = (ROOT / "static" / "js" / "workspace.js").read_text(encoding="utf-8")


class Nudge(unittest.TestCase):
    def test_fires_at_each_multiple_only(self):
        self.assertEqual([n for n in range(1, 13) if script_nudge.should_nudge(n - 1, n, 4)], [4, 8, 12])
        self.assertFalse(script_nudge.should_nudge(9, 10, 0))

    def test_message_is_a_control_message(self):
        self.assertTrue(script_nudge.message(4).startswith(_CONTROL_PREFIXES[0]))

    def test_limit_is_configurable_and_clamped(self):
        self.assertEqual(limits.AGENT_LIMITS["python_prompt_nudge"], (4, 3, 20))


class Wiring(unittest.TestCase):
    def test_executor_has_grep(self):
        self.assertIn('"list_files", "project_overview", "grep", "run_python"', RUN)

    def test_run_python_description_forbids_file_inspection(self):
        from core.agent_tools import schemas
        d = next(t["function"]["description"] for t in schemas.AGENT_TOOLS if t["function"]["name"] == "run_python")
        self.assertIn("NEVER use it to read, search or list files", d)

    def test_run_scope_is_wired_and_nothing_is_saved_for_code(self):
        self.assertIn('"run_python" not in run_allow', RUN)
        self.assertIn('run_allow.add("run_python")', RUN)
        self.assertIn("For this run", JS)
        from routes.agent.permissions import _pattern_error
        self.assertIn("only be allowed once", _pattern_error("python *", {"kind": "python"}))

    def test_denial_blocks_workaround(self):
        self.assertIn("Do not run a similar one", RUN)


if __name__ == "__main__":
    unittest.main()
