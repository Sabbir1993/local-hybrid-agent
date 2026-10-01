"""tests/test_ingest_fidelity.py - R7: hostile-document extraction fidelity.

Company knowledge arrives as long PDFs and table-heavy DOCX, not clean TXT.
Three hostile docs with extraction assertions:
  1. table-heavy DOCX: cell text must survive (paragraphs-only extraction
     silently drops every table in the corpus);
  2. scanned (image-only) PDF: must fail with a SPECIFIC error (image-only,
     OCR unavailable) instead of the generic "no extractable text" - the
     admin has to know WHY, and that re-uploading the same scan changes
     nothing;
  3. 200-page PDF: extraction must reach the tail (pins the E1 400-chunk cap
     through real PDF bytes, not synthetic strings).

Run: python -m unittest tests.test_ingest_fidelity -v
"""

import tempfile
import unittest
from pathlib import Path

from core import knowledge_ingest


def _tmp(ext: str):
    d = tempfile.TemporaryDirectory()
    return d, Path(d.name) / f"doc{ext}"


class DocxTableTests(unittest.TestCase):
    def test_table_cells_are_extracted(self):
        import docx
        tmp, path = _tmp(".docx")
        try:
            d = docx.Document()
            d.add_paragraph("Handbook preamble about expense policy.")
            t = d.add_table(rows=2, cols=2)
            t.cell(0, 0).text = "tungsten"
            t.cell(0, 1).text = "invoice"
            t.cell(1, 0).text = "cobalt"
            t.cell(1, 1).text = "snapshot"
            d.add_paragraph("Closing paragraph.")
            d.sections[0].header.paragraphs[0].text = "QUASAR Division header"
            d.sections[0].footer.paragraphs[0].text = "Page footer"
            d.save(str(path))
            text = knowledge_ingest.extract_docx(path)
        finally:
            tmp.cleanup()
        for w in ("tungsten", "invoice", "cobalt", "snapshot", "preamble",
                  "QUASAR Division header", "Page footer"):
            self.assertIn(w, text, f"lost through extraction: {w}")
        # header boilerplate lands exactly once, not per page
        self.assertEqual(text.count("QUASAR Division header"), 1)


class ScannedPdfTests(unittest.TestCase):
    def _image_only_pdf(self, path: Path):
        from PIL import Image, ImageDraw
        from reportlab.pdfgen.canvas import Canvas
        img_path = str(path) + ".png"
        img = Image.new("RGB", (200, 100), "white")
        ImageDraw.Draw(img).text((10, 40), "scanned words", fill="black")
        img.save(img_path)
        c = Canvas(str(path))
        c.drawImage(img_path, 0, 0, width=200, height=100)
        c.showPage()
        c.save()

    def test_image_only_pdf_error_names_ocr(self):
        tmp, path = _tmp(".pdf")
        try:
            self._image_only_pdf(path)
            with self.assertRaises(ValueError) as ctx:
                knowledge_ingest.extract_pdf(path)
        finally:
            tmp.cleanup()
        msg = str(ctx.exception).lower()
        self.assertTrue("image" in msg or "ocr" in msg or "scan" in msg,
                        f"generic error hides the cause: {ctx.exception}")


class LongPdfTests(unittest.TestCase):
    def test_200_page_pdf_tail_survives_extraction(self):
        from reportlab.pdfgen.canvas import Canvas
        tmp, path = _tmp(".pdf")
        try:
            c = Canvas(str(path))
            for i in range(1, 201):
                c.drawString(72, 720, f"Handbook page {i} filler about policy.")
                if i == 200:
                    c.drawString(72, 700, "Teleportation reimbursement form Q-9 alpha-999-zulu.")
                c.showPage()
            c.save()
            text = knowledge_ingest.extract_pdf(path)
        finally:
            tmp.cleanup()
        self.assertIn("Handbook page 1", text)
        self.assertIn("alpha-999-zulu", text)


if __name__ == "__main__":
    unittest.main()
