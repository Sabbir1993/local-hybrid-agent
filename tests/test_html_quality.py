"""Mechanical repair of the typos a small model leaves in generated HTML.

Run: python -m unittest tests.test_html_quality -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.html_quality import ensure_basics, repair


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



class BasicsTests(unittest.TestCase):
    def test_a_bare_page_gets_lang_charset_viewport_and_title(self):
        out, notes = ensure_basics("<html><head></head><body><h1>Sales <b>report</b></h1><img src='a.png'></body></html>")
        self.assertIn('<html lang="en">', out)
        self.assertIn('<meta charset="utf-8">', out)
        self.assertIn('name="viewport"', out)
        self.assertIn("<title>Sales report</title>", out)
        self.assertEqual(notes[:4], ["lang", "charset", "viewport", "title"])
        self.assertIn("1 image(s) without alt text", notes)

    def test_bengali_text_gets_lang_bn(self):
        out, _ = ensure_basics("<html><head></head><body><p>বাংলাদেশ ব্যাংকের বার্ষিক প্রতিবেদন</p></body></html>")
        self.assertIn('lang="bn"', out)

    def test_a_complete_page_and_a_fragment_are_left_alone(self):
        page = ('<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
                '<title>T</title></head><body></body></html>')
        self.assertEqual(ensure_basics(page), (page, []))
        frag = "<div><img src='x'></div>"
        self.assertEqual(ensure_basics(frag), (frag, []))


if __name__ == "__main__":
    unittest.main()
