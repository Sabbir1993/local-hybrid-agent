"""Generated PDFs carry a title, a language and sensible properties; the printing command asks for tags and bookmarks."""
import tempfile
import unittest
from pathlib import Path

import core.agent_tools  # noqa: F401
from pypdf import PdfReader

from core.agent_tools import pdf_render as pr
from tests.pdf_fixtures import text_pdf

try:
    HAVE_BROWSER = bool(pr._find_chromium_binary())
except Exception:
    HAVE_BROWSER = False


class Shell(unittest.TestCase):
    def test_title_and_language(self):
        html = "<h1>Board <em>Paper</em> &amp; Plan</h1><p>Body text here</p>"
        self.assertEqual(pr._doc_title(html), "Board Paper &amp; Plan")
        self.assertEqual(pr._doc_lang(html), "en")
        bn = "<h1>বাংলাদেশ বাজার বিশ্লেষণ</h1><p>এটি একটি বিস্তারিত প্রতিবেদন যা বোর্ডের জন্য তৈরি করা হয়েছে এবং সম্পূর্ণ বাংলায় লেখা</p>"
        self.assertEqual(pr._doc_lang(bn), "bn")

    def test_the_locked_down_page_has_lang_and_title(self):
        out = pr._lock_down_html("<!doctype html><h1>Report</h1><p>x</p>")
        self.assertIn('<html lang="en">', out)
        self.assertIn("<title>Report</title>", out)
        self.assertIn("Content-Security-Policy", out)
        self.assertIn("script-src 'none'", out)                           # the lock-down is unchanged

    def test_an_untitled_page_gets_a_default(self):
        self.assertEqual(pr._doc_title("<p>no heading</p>"), "Document")

    def test_properties_are_written_to_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.pdf"
            p.write_bytes(text_pdf(2))
            pr.finalize_pdf(p, "<h1>Hercules TAP</h1>")
            r = PdfReader(str(p))
            self.assertEqual(r.metadata.get("/Title"), "Hercules TAP")
            self.assertEqual(str(r.trailer["/Root"].get("/Lang")), "en")
            self.assertTrue(r.trailer["/Root"]["/ViewerPreferences"]["/DisplayDocTitle"])
            self.assertEqual(len(r.pages), 2)

    def test_a_failure_to_set_properties_never_breaks_the_pdf(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "broken.pdf"
            p.write_bytes(b"%PDF-1.4 not really")
            pr.finalize_pdf(p, "<h1>x</h1>")                               # must not raise
            self.assertEqual(p.read_bytes(), b"%PDF-1.4 not really")

    def test_the_print_command_asks_for_tags_and_bookmarks(self):
        src = Path(pr.__file__).read_text(encoding="utf-8")
        self.assertIn("--export-tagged-pdf", src)
        self.assertIn("--generate-pdf-document-outline", src)


@unittest.skipUnless(HAVE_BROWSER, "no Chrome/Edge to print with")
class Printed(unittest.TestCase):
    def test_a_real_render_has_title_language_and_text(self):
        import core.doc_ops as D
        data = D.render_pdf("# Hercules TAP Bangladesh\n\n## Summary\n\nThe plan in short.\n\n## Budget\n\n| Item | BDT |\n|---|---|\n| Dev | 85,000 |\n", "tap")
        r = PdfReader(__import__("io").BytesIO(data))
        self.assertIn("Hercules TAP", str(r.metadata.get("/Title")))
        self.assertEqual(str(r.trailer["/Root"].get("/Lang")), "en")
        self.assertIn("The plan in short", "\n".join(p.extract_text() for p in r.pages))

    def test_bengali_text_prints_without_boxes(self):
        import io
        import core.doc_ops as D
        import pdfplumber
        data = D.render_pdf("# বাংলাদেশ বাজার\n\nএটি একটি প্রতিবেদন যা বোর্ডের জন্য তৈরি করা হয়েছে এবং সম্পূর্ণ বাংলায় লেখা।\n", "bn")
        self.assertEqual(str(PdfReader(io.BytesIO(data)).trailer["/Root"].get("/Lang")), "bn")
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            text = pdf.pages[0].extract_text() or ""
        self.assertTrue(any("ঀ" <= ch <= "৿" for ch in text))          # Bengali glyphs are in the text layer


if __name__ == "__main__":
    unittest.main()
