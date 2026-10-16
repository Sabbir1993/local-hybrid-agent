"""tests/test_media_enhance.py - the quality tail added to a short, bare image prompt."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.media import enhance as en


class EnhanceTests(unittest.TestCase):
    def test_a_short_bare_prompt_gets_the_tail(self):
        out = en.enhance("a red bicycle by the river.", {})
        self.assertTrue(out.startswith("a red bicycle by the river, "))
        self.assertTrue(out.endswith(en.TAIL))

    def test_a_prompt_that_already_describes_the_look_is_left_alone(self):
        for p in ("a watercolor of a cat", "cinematic photo of a street at night", "logo for a bakery"):
            self.assertEqual(en.enhance(p, {}), p)

    def test_quoted_text_long_prompts_and_picture_edits_are_left_alone(self):
        for p, o in (('a sign that says "OPEN"', {}), (" ".join(["word"] * 40), {}), ("make it red", {"inputs": [{"png": b""}]})):
            self.assertEqual(en.enhance(p, o), p)

    def test_the_switch_turns_it_off(self):
        with mock.patch.object(en, "media_cfg", return_value={"enhance_prompt": False}):
            self.assertEqual(en.enhance("a red bicycle", {}), "a red bicycle")

    def test_empty_stays_empty(self):
        self.assertEqual(en.enhance("  ", {}), "")


if __name__ == "__main__":
    unittest.main()
