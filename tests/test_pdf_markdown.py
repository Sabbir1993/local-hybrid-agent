"""tests/test_pdf_markdown.py - Markdown -> HTML/PDF for generated documents (core/agent_tools/dom_convert.py,
pdf_render.py): GitHub tables (also the sloppy shapes models write), lists, quotes, no raw pipes in the PDF.
Run: python -m unittest tests.test_pdf_markdown -v"""

import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.agent_tools import dom_convert, pdf_render

TABLE = "| Dimension | IBBL | BRAC Bank |\n|---|---|---|\n| Primary engine | Corporate | SME |\n| Moat | Shariah | Agents |"


def body(md):
    return dom_convert._markdown_to_html_dom(md, title="t")


class HtmlTests(unittest.TestCase):
    def test_plain_table_becomes_a_table(self):
        h = body("## 2.1 Money\n\n" + TABLE + "\n\nafter")
        self.assertEqual(h.count("<table"), 1)
        self.assertIn("<th>Dimension</th>", h)
        self.assertIn("<td>Moat</td>", h)
        self.assertNotIn("|---|", h)

    def test_blank_lines_between_rows_do_not_break_the_table(self):
        sloppy = "\n\n".join(TABLE.splitlines())            # what the model wrote into the file
        h = body("Intro\n\n" + sloppy)
        self.assertEqual(h.count("<table"), 1)
        self.assertEqual(h.count("<tr>"), 3)
        self.assertNotIn("| Dimension", h)

    def test_table_right_after_a_paragraph_line(self):
        h = body("Here is the comparison:\n" + TABLE)
        self.assertEqual(h.count("<table"), 1)

    def test_two_tables_stay_two(self):
        h = body(TABLE + "\n\ntext between\n\n" + TABLE)
        self.assertEqual(h.count("<table"), 2)

    def test_alignment_inline_markup_and_escaped_pipe(self):
        h = body("| a | b |\n|:--:|--:|\n| **bold** `code` | x \\| y |")
        self.assertIn('text-align:center', h)
        self.assertIn("<strong>bold</strong>", h)
        self.assertIn('<code class="inline-code">code</code>', h)
        self.assertIn("x | y", h)

    def test_raw_html_is_escaped_never_passed_through(self):
        h = body("<script>alert(1)</script> <img src=x onerror=alert(1)>\n\n| a |\n|---|\n| <b onclick=x>y</b> |")
        self.assertNotIn("<script", h)
        self.assertNotIn("<img", h)
        self.assertNotIn("<b onclick", h)
        self.assertIn("&lt;script&gt;", h)

    def test_blocks_get_the_document_theme_classes(self):
        h = body("# H1\n\n## H2\n\n### H3\n\ntext\n\n- a\n- b\n\n1. x\n2. y\n\n> q\n\n---\n\n```py\nx = 1\n```")
        for cls in ('doc-h1', 'doc-h2', 'doc-h3', 'doc-p', 'doc-list', 'doc-ordered', 'doc-quote', 'doc-hr', 'code-block'):
            self.assertIn(cls, h, cls)

    def test_task_boxes_are_dropped(self):
        h = body("- [ ] open item\n- [x] done item\n- plain")
        self.assertNotIn("[ ]", h)
        self.assertNotIn("[x]", h)
        self.assertIn("<li>open item</li>", h)
        self.assertIn("✓ done item", h)

    def test_falls_back_to_the_simple_converter_without_markdown_it(self):
        with mock.patch.object(dom_convert, "markdown_to_body_html", return_value=None):
            h = body(TABLE)
        self.assertEqual(h.count("<table"), 1)

    def test_normalize_keeps_code_blocks_and_prose_alone(self):
        s = "para\n\nanother para\n\n```\n| not | a table |\n\n| still code |\n```"
        self.assertEqual(dom_convert.normalize_tables(s), s)


class ReportlabFallbackTests(unittest.TestCase):
    """No Chrome/Edge on the PC: the ReportLab path must not print raw pipes either."""

    def test_fallback_pdf_has_a_real_table(self):
        import pdfplumber
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "report.pdf"
            md = "# Report\n\nIntro\n\n" + "\n\n".join(TABLE.splitlines()) + "\n\nEnd"
            with mock.patch.object(pdf_render, "_render_html_to_pdf", return_value=False):
                self.assertTrue(pdf_render._save_text_or_markdown_as_pdf(out, md))
            with pdfplumber.open(out) as pdf:
                text = "\n".join(p.extract_text() or "" for p in pdf.pages)
                tables = [t for p in pdf.pages for t in p.extract_tables()]
        self.assertNotIn("|---|", text)
        self.assertNotIn("| Dimension", text)
        self.assertIn("Primary engine", text)
        self.assertTrue(tables and tables[0][0][:3] == ["Dimension", "IBBL", "BRAC Bank"], tables)


class BrowserRenderTests(unittest.TestCase):
    """The HTML -> PDF step: current Edge exits at once and writes the file a moment later; a wrong wait made
    every document silently fall back to the plain ReportLab writer (no tables, raw backticks)."""

    def _run(self, delay, content=b"%PDF-1.4 fake pdf body"):
        import threading
        import time as _time
        seen = {}

        def fake_run(cmd, **kw):
            pdf = next(a.split("=", 1)[1] for a in cmd if a.startswith("--print-to-pdf="))
            seen["cmd"] = cmd

            def later():
                _time.sleep(delay)
                Path(pdf).write_bytes(content[:8])        # header first ...
                _time.sleep(0.4)
                Path(pdf).write_bytes(content)            # ... then the finished file
            threading.Thread(target=later, daemon=True).start()
            return mock.Mock(returncode=0, stderr="")

        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(pdf_render, "_find_chromium_binary", return_value="browser.exe"), \
                    mock.patch.object(pdf_render.subprocess, "run", side_effect=fake_run):
                out = Path(d) / "x.pdf"
                ok = pdf_render._render_html_to_pdf("<html><body>x</body></html>", out)
                size = out.stat().st_size if out.exists() else 0
        return ok, size, seen.get("cmd", [])

    def test_waits_for_a_pdf_the_browser_writes_after_it_exited(self):
        ok, size, _ = self._run(delay=0.6)
        self.assertTrue(ok)
        self.assertEqual(size, len(b"%PDF-1.4 fake pdf body"))     # the finished file, not the half-written one

    def test_gives_up_when_no_pdf_ever_appears(self):
        with mock.patch.object(pdf_render, "_PDF_WAIT_S", 1):
            ok, _, _ = self._run(delay=30)
        self.assertFalse(ok)

    def test_a_stale_file_is_not_mistaken_for_the_new_render(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(pdf_render, "_find_chromium_binary", return_value="browser.exe"), \
                    mock.patch.object(pdf_render, "_PDF_WAIT_S", 1), \
                    mock.patch.object(pdf_render.subprocess, "run", return_value=mock.Mock(returncode=0, stderr="")):
                out = Path(d) / "x.pdf"
                out.write_bytes(b"%PDF-1.4 old file")
                self.assertFalse(pdf_render._render_html_to_pdf("<html></html>", out))

    def test_no_flag_that_hangs_the_print_job(self):
        _, _, cmd = self._run(delay=0.1)
        self.assertFalse(any("scriptEnabled" in a for a in cmd), cmd)         # hangs --print-to-pdf in current Edge
        self.assertTrue(any(a.startswith("--user-data-dir=") for a in cmd))    # throwaway profile
        self.assertIn("--proxy-server=127.0.0.1:9", cmd)                       # network stays off

    @unittest.skipUnless(pdf_render._find_chromium_binary(), "no Chrome/Edge on this PC")
    def test_real_browser_renders_the_themed_pdf_with_a_table(self):
        import pdfplumber
        md = "# T\n\n## 1. Profiles\n\n" + "\n\n".join(TABLE.splitlines()) + "\n\nuse `code` here"
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "real.pdf"
            self.assertTrue(pdf_render._save_text_or_markdown_as_pdf(out, md))
            with pdfplumber.open(out) as pdf:
                text = "\n".join(pg.extract_text() or "" for pg in pdf.pages)
                tables = [t for pg in pdf.pages for t in pg.extract_tables()]
        self.assertNotIn("cid:", text)                # not the ReportLab fallback's bullets
        self.assertNotIn("`", text)                   # inline code rendered, not shown raw
        self.assertNotIn("| Dimension", text)
        self.assertTrue(tables and tables[0][0][:3] == ["Dimension", "IBBL", "BRAC Bank"], tables)


if __name__ == "__main__":
    unittest.main()
