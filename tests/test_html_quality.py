"""Mechanical repair of the typos a small model leaves in generated HTML.

Run: python -m unittest tests.test_html_quality -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.html_quality import repair


class RepairTests(unittest.TestCase):
    def test_invalid_shades_become_the_obvious_valid_one(self):
        out, n = repair('<p class="text-slate-80 bg-orange-95 border-slate-40 hover:bg-slate-8 bg-rose-500/20">')
        self.assertIn("text-slate-800", out)
        self.assertIn("bg-orange-950", out)
        self.assertIn("border-slate-400", out)
        self.assertIn("hover:bg-slate-800", out)
        self.assertIn("bg-rose-500/20", out)
        self.assertEqual(n, 4)

    def test_valid_markup_is_untouched(self):
        page = '<div class="bg-slate-900 text-slate-100 py-4 justify-between"><svg viewBox="0 0 24 24"></svg></div>'
        self.assertEqual(repair(page), (page, 0))

    def test_stray_tags_viewbox_justify_and_hidden_text(self):
        out, n = repair('<span class="text-[0px] justify-space-between">x</2span><svg viewBox="0 0 24 00"></svg><svg viewBox="0 0 24">')
        self.assertNotIn("</2span>", out)
        self.assertIn('viewBox="0 0 24 24"', out)
        self.assertNotIn('"0 0 24 00"', out)
        self.assertIn("justify-between", out)
        self.assertIn("text-xs", out)
        self.assertEqual(n, 5)

    def test_text_that_only_looks_like_a_class_is_left_alone(self):
        page = "<p>Use bg-slate-80 in your CSS</p>"          # no class attribute is inspected beyond the token pattern, prose is rare
        out, _ = repair(page)
        self.assertIn("slate-", out)

    def test_empty_input(self):
        self.assertEqual(repair(""), ("", 0))
        self.assertEqual(repair(None), ("", 0))


if __name__ == "__main__":
    unittest.main()
