"""tests/test_read_ledger.py - repeated file reads in one run, and the analysis-request exemption from forced plans.

Run: python -m unittest tests.test_read_ledger -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.agent_loop import plan_guard
from core.agent_loop.clearing import CLEARED_PREFIX
from core.agent_loop.read_ledger import ReadLedger

def read(path, a, b, total=400):
    """What read_file returns for lines a-b: numbered rows plus the footer the ledger parses."""
    rows = "\n".join(f"{i:>6}\tline {i}" for i in range(a, b + 1))
    return f"{rows}\n[{path}: lines {a}-{b} of {total}]"


def tool_msg(tc_id, content):
    return {"role": "tool", "tool_call_id": tc_id, "content": content}


F = "src/styles.css"


class ReadLedgerTests(unittest.TestCase):
    def setUp(self):
        self.led = ReadLedger()
        self.msgs = []

    def did_read(self, tc, a, b, cleared=False):
        args = {"path": F, "offset": a, "limit": b - a + 1}
        res = read(F, a, b)
        self.led.record(args, tc, res)
        self.msgs.append(tool_msg(tc, CLEARED_PREFIX + ": read_file ...]" if cleared else res))

    def ask(self, a, limit):
        return self.led.plan({"path": F, "offset": a, "limit": limit}, self.msgs)

    def test_first_read_runs(self):
        self.assertIsNone(self.ask(185, 30))

    def test_exact_repeat_in_context_gets_a_note(self):
        self.did_read("t1", 185, 214)
        plan = self.ask(185, 30)
        self.assertIn("already in this conversation", plan["note"])
        self.assertIn("185-214", plan["note"])

    def test_a_third_identical_request_is_resent_with_an_order_to_act(self):
        self.did_read("t1", 175, 206)
        self.assertIn("note", self.ask(175, 32))                   # 1st: "you already have it"
        self.assertIn("note", self.ask(175, 32))                   # 2nd
        plan = self.ask(175, 32)                                   # 3rd: the lines again, plus escalate
        self.assertTrue(plan["escalate"])
        self.assertIn("STOP reading", plan["prefix"])

    def test_overlapping_request_fully_inside_what_it_has_gets_a_note(self):
        self.did_read("t1", 160, 219)
        self.assertIn("note", self.ask(185, 30))                  # 185-214 is inside 160-219

    def test_overlap_in_the_middle_that_saves_little_runs_as_asked(self):
        self.did_read("t1", 185, 214)
        self.assertIsNone(self.ask(160, 60))                       # 160-219 would "trim" to 160-219: no saving

    def test_overlap_at_the_edge_trims_to_the_tail(self):
        self.did_read("t1", 100, 199)
        plan = self.ask(150, 100)                                  # 150-249: 150-199 known, 200-249 new
        self.assertEqual((plan["args"]["offset"], plan["args"]["limit"]), (200, 50))

    def test_a_hole_in_the_middle_does_not_pretend_to_trim(self):
        # the screenshot case: 17 known lines inside a 500-line request would "trim" to the same 500 lines
        self.did_read("t1", 240, 256)
        self.assertIsNone(self.ask(1, 500))

    def test_asking_past_the_end_of_a_short_file_is_clamped_to_its_length(self):
        self.did_read("t1", 1, 256)                                # footer says "of 400" in this helper, so use a short one
        self.led._files[F]["total"] = 256
        plan = self.ask(1, 500)
        self.assertIn("already in this conversation", plan["note"])
        self.assertIn("nothing at line 257", self.ask(257, 50)["note"])

    def test_a_different_part_of_the_file_runs_untouched(self):
        self.did_read("t1", 1, 100)
        self.assertIsNone(self.ask(300, 50))

    def test_cleared_lines_may_be_read_once_more_then_are_blocked(self):
        self.did_read("t1", 185, 214, cleared=True)
        self.assertIsNone(self.ask(185, 30))                       # allowed: nothing of it is in the prompt
        self.assertIsNone(self.ask(185, 30))                       # second fresh read still allowed
        plan = self.ask(185, 30)
        self.assertIn("Do not read them again", plan["note"])
        self.assertIn("line 185", plan["note"])                    # the digest comes back

    def test_errors_are_not_recorded_and_a_write_resets(self):
        self.led.record({"path": F}, "t1", "error: File not found")
        self.assertIsNone(self.ask(1, 50))
        self.did_read("t2", 1, 50)
        self.led.clear()
        self.assertIsNone(self.ask(1, 50))


class AnalysisExemptionTests(unittest.TestCase):
    def test_init_prompt_and_analysis_requests_need_no_plan(self):
        from core.project_context import INIT_PROMPT
        self.assertTrue(plan_guard.is_analysis_request(INIT_PROMPT))
        self.assertFalse(plan_guard.needs_plan("creation", INIT_PROMPT))
        self.assertFalse(plan_guard.needs_plan("creation", "Analyze this project and explain how the build works", {}))

    def test_real_build_work_still_needs_a_plan(self):
        self.assertTrue(plan_guard.needs_plan("creation", "Build a todo app and add tests", {}))
        self.assertFalse(plan_guard.is_analysis_request("Fix the bug in the login project"))


class ProjectOverviewTests(unittest.TestCase):
    def test_summarize_finds_areas_languages_entries_and_manifests(self):
        from core.agent_tools.overview import summarize
        files = ["README.md", "package.json", "src/main.js", "src/util.js", "api/server.py", "api/db.py",
                 "docs/deep/a/b/readme.md", "tests/test_a.py"]
        s = summarize(files)
        self.assertEqual(s["areas"]["src"], 2)
        self.assertEqual(s["exts"][".py"], 3)
        self.assertIn("src/main.js", s["entries"])
        self.assertIn("api/server.py", s["entries"])
        self.assertEqual(s["manifests"][:2], ["README.md", "package.json"])     # shallow ones first
        self.assertNotIn("docs/deep/a/b/readme.md", s["manifests"])             # too deep to matter

    def test_tool_returns_one_overview_with_manifest_heads(self):
        import asyncio
        from pathlib import Path
        from unittest import mock
        from core.agent_tools import overview

        class Bridge:
            async def call(self, uid, op, params):
                if op == "fs.list":
                    return {"files": {"*": ["README.md", "package.json"], "*/*": ["src/main.js"]}.get(params["pattern"], [])}
                if op == "fs.read":
                    return {"content": "line1\nline2"}
                raise AssertionError(op)

        import core.agent_tools as pkg
        with mock.patch.object(overview, "_cb", return_value=Bridge()), \
             mock.patch.object(pkg, "active_workspace", return_value=Path("W")), \
             mock.patch.object(pkg, "_remote_uid", return_value=1):
            out = asyncio.run(overview.tool_project_overview({}))
        self.assertIn("Top-level areas", out)
        self.assertIn("--- README.md", out)
        self.assertIn("line2", out)
        self.assertIn("src/main.js", out)


if __name__ == "__main__":
    unittest.main()
