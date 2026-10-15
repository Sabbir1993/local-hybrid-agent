"""Chat cannot run scripts: a deck request that 'builds' by saving a .sh / .py gets told so, and tool markup never becomes the answer."""
import unittest

import core.agent_tools  # noqa: F401
from routes.chat import delivery as d


class ScriptGuard(unittest.TestCase):
    def test_script_for_a_deck_is_refused(self):
        self.assertTrue(d._script_for_deck({"path": "run_create-1.sh"}, "build", False, "x"))
        self.assertTrue(d._script_for_deck({"path": "make.py"}, "", False, "make a pptx about Dhaka"))
        self.assertTrue(d._script_for_deck({"file": "a.ps1"}, "", True, "x"))

    def test_normal_writes_are_untouched(self):
        self.assertFalse(d._script_for_deck({"path": "notes.md"}, "build", False, "x"))
        self.assertFalse(d._script_for_deck({"path": "script.py"}, "", False, "write me a python script to rename files"))
        self.assertFalse(d._script_for_deck({"path": "a.sh"}, "", False, "write a bash script"))

    def test_the_route_wires_both_guards(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1] / "routes" / "chat" / "run.py").read_text(encoding="utf-8")
        self.assertIn("_script_for_deck(args, brief_phase, pdf_build, last_query)", src)
        self.assertIn("_NO_TOOL_ANSWER", src)


if __name__ == "__main__":
    unittest.main()
