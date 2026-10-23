"""File-building turns: gentle DRY, no raw tool-call text, "continue" keeps the page build, a refused big write gets a retry.

Run: python -m unittest tests.test_build_sampling -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import deck_research as dr
from core.agent_loop.degeneration import sanitize_user_facing_content
from routes.chat.run import _TOOL_TEXT_RX

RUN_SRC = (Path(__file__).resolve().parents[1] / "routes" / "chat" / "run.py").read_text(encoding="utf-8")
STRICT = {"dry_multiplier": 0.6, "dry_base": 1.75, "dry_allowed_length": 2, "dry_penalty_last_n": 4096}


class BuildSamplerTests(unittest.TestCase):
    def test_a_strict_panel_dry_is_replaced_by_the_build_dry(self):
        out = dr.build_turn_sampler(dict(STRICT), {})
        self.assertEqual(out["dry_allowed_length"], 10)
        self.assertEqual(out["dry_penalty_last_n"], 1024)

    def test_no_panel_dry_gets_the_build_dry(self):
        self.assertEqual(dr.build_turn_sampler(None, {})["dry_allowed_length"], 10)

    def test_a_gentler_panel_dry_is_kept(self):
        panel = {"dry_multiplier": 0.3, "dry_allowed_length": 16}
        self.assertEqual(dr.build_turn_sampler(dict(panel), {}), panel)

    def test_other_panel_fields_survive(self):
        out = dr.build_turn_sampler({**STRICT, "seed": 7, "repeat_last_n": 64}, {})
        self.assertEqual((out["seed"], out["repeat_last_n"]), (7, 64))

    def test_operator_can_turn_the_build_dry_off(self):
        cfg = {"chat": {"build_dry": {"multiplier": 0}}}
        self.assertEqual(dr.build_turn_sampler(dict(STRICT), cfg), STRICT)


class ToolTextTests(unittest.TestCase):
    CUT = "I have enough data.\n\n<tool_call>\n<function=write_file>\n<parameter=path>\nx.html\n<parameter=content>\n<!DOCTYPE html>"

    def test_an_unclosed_call_is_removed_from_the_answer(self):
        self.assertEqual(sanitize_user_facing_content(self.CUT), "I have enough data.")

    def test_the_stream_recognises_the_start_of_a_text_call(self):
        for text in ("<tool_call>", "x <function=write_file>", "<function name=\"a\">"):
            self.assertTrue(_TOOL_TEXT_RX.search(text), text)
        self.assertFalse(_TOOL_TEXT_RX.search("a function (x) => y, <b>bold</b>"))


class WiringTests(unittest.TestCase):
    def test_a_bare_continue_keeps_the_page_build_route(self):
        self.assertIn("builds.is_continue(own_q) or re.search(", RUN_SRC)

    def test_a_refused_big_write_gets_one_more_round(self):
        self.assertIn("size_retried", RUN_SRC)
        self.assertIn("turn_limit = max(turn_limit, turn + 2)", RUN_SRC)

    def test_streamed_text_calls_are_held_back(self):
        self.assertIn("if _safe and not tool_text_open:", RUN_SRC)

    def test_builds_use_the_build_sampler(self):
        self.assertIn("deck_research.build_turn_sampler(sampler_fields, APP_CONFIG)", RUN_SRC)


if __name__ == "__main__":
    unittest.main()
