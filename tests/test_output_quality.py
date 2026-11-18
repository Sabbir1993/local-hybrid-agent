"""Output quality: theme-aware chart colours (deck and workbook), wide-glyph column widths."""
import unittest

from core.doc_ops.pptx_ops import design, theme
from core.sheet_engine import build


class DeckChartColours(unittest.TestCase):
    def test_every_theme_gets_distinct_colours_that_read_on_its_background(self):
        for name, t in theme.THEMES.items():
            cols = design._series_colors(t)
            self.assertGreaterEqual(len(cols), 8, name)
            self.assertEqual(len(cols), len(set(cols)), name)
            for c in cols:
                self.assertGreaterEqual(design._contrast(c, t["bg"]), 3.0, f"{name} {c}")


class WorkbookChartColours(unittest.TestCase):
    def test_palette_starts_with_the_accent_and_has_no_repeats(self):
        pal = build._chart_palette("1F4E79", 8)
        self.assertEqual(pal[0], "1F4E79")
        self.assertEqual(len(pal), len(set(pal)))

    def test_bad_accent_falls_back(self):
        self.assertEqual(build._chart_palette("nope")[0], "1F4E79")


class ColumnWidths(unittest.TestCase):
    def test_bengali_and_cjk_are_wider_than_their_length(self):
        self.assertEqual(build._text_width("Revenue"), 7)
        self.assertGreater(build._text_width("কখগঘ"), 4)            # Indic letters are wider than Latin ones
        self.assertEqual(build._text_width("日本語"), 6)


class PdfGlyphCheck(unittest.TestCase):
    def test_unprintable_characters_are_found(self):
        from core.agent_tools import pdf_check
        self.assertFalse(pdf_check.has_unreadable_glyphs("Settlement report for March"))
        self.assertTrue(pdf_check.has_unreadable_glyphs("Total ��"))
        self.assertTrue(pdf_check.has_unreadable_glyphs("Total (cid:1234) due"))


if __name__ == "__main__":
    unittest.main()
