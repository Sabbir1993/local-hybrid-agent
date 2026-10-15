"""pdf_see (vision) and pdf_ocr (Tesseract, vision fallback). The model and the program are mocked; everything else is real."""
import asyncio
import io
import unittest
from unittest import mock

import core.agent_tools  # noqa: F401
from pypdf import PdfReader

from core.pdf_tools import ocr
from tests.pdf_fixtures import scanned_pdf, text_pdf
from tests.test_pdf_tools import Base


def run(coro):
    return asyncio.run(coro)


class See(Base):
    def call(self, **args):
        from core.agent_tools import TOOL_IMPLS
        return run(TOOL_IMPLS["pdf_see"](args))

    def test_pages_are_drawn_locally_and_described(self):
        self.put("d.pdf", text_pdf(3))
        seen = []

        async def fake(data, question="", force_local=False):
            seen.append((data[:3], question, force_local))
            return "A page with a heading and one line of text"
        with mock.patch("core.small_model.vision.describe_image_bytes", fake):
            out = self.call(file="d.pdf", pages="1-2", question="What is on the page?")
        self.assertIn("--- page 1 ---", out)
        self.assertIn("--- page 2 ---", out)
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0][0], b"\xff\xd8\xff")                     # a real JPEG went to the model
        self.assertTrue(all(s[2] for s in seen))                         # local vision only by default
        self.assertIn("What is on the page?", seen[0][1])

    def test_cloud_vision_needs_the_switch(self):
        from core.pdf_tools import see
        with mock.patch("core.small_model.APP_CONFIG", {"chat": {"pdf_see_cloud": True}}):
            self.assertTrue(see.cloud_allowed())
        with mock.patch("core.small_model.APP_CONFIG", {"chat": {}}):
            self.assertFalse(see.cloud_allowed())

    def test_at_most_four_pages_per_call_and_it_says_so(self):
        self.put("d.pdf", text_pdf(8))

        async def fake(data, question="", force_local=False):
            return "ok"
        with mock.patch("core.small_model.vision.describe_image_bytes", fake):
            out = self.call(file="d.pdf", pages="all")
        self.assertEqual(out.count("--- page"), 4)
        self.assertIn("4 more page(s)", out)

    def test_no_vision_model_gives_a_clear_message(self):
        self.put("d.pdf", text_pdf(1))

        async def fake(data, question="", force_local=False):
            return "error: no image model available (nothing loaded)"
        with mock.patch("core.small_model.vision.describe_image_bytes", fake):
            out = self.call(file="d.pdf")
        self.assertTrue(out.startswith("error:"))
        self.assertIn("vision model", out)

    def test_card_numbers_in_the_answer_are_masked(self):
        self.put("d.pdf", text_pdf(1))

        async def fake(data, question="", force_local=False):
            return "The page shows card 4111 1111 1111 1111"
        with mock.patch("core.small_model.vision.describe_image_bytes", fake):
            self.assertNotIn("4111 1111 1111 1111", self.call(file="d.pdf"))


class Ocr(Base):
    def call(self, **args):
        from core.agent_tools import TOOL_IMPLS
        return run(TOOL_IMPLS["pdf_ocr"](args))

    def test_language_codes_are_validated_and_checked_against_the_install(self):
        self.assertEqual(ocr.check_lang(None, ["eng", "ben", "osd"]), "eng+ben")
        self.assertEqual(ocr.check_lang(None, ["eng"]), "eng")
        self.assertEqual(ocr.check_lang("eng", []), "eng")
        for bad in ("eng; rm -rf /", "../x", "e", "ENG"):
            with self.assertRaises(ocr.PdfError):
                ocr.check_lang(bad, ["eng"])
        with self.assertRaises(ocr.PdfError) as cm:
            ocr.check_lang("fra", ["eng", "ben"])
        self.assertIn("not installed", str(cm.exception))

    def test_default_pages_are_the_scanned_ones(self):
        data = scanned_pdf()
        self.assertEqual(ocr.selected_pages(data, None), [1])
        self.assertEqual(ocr.selected_pages(text_pdf(3), None), [1, 2, 3])
        self.assertEqual(ocr.selected_pages(text_pdf(3), "2"), [2])

    def test_with_tesseract_a_searchable_copy_is_saved(self):
        self.put("scan.pdf", scanned_pdf())

        def fake_run(cmd, **kw):
            if "--list-langs" in cmd:
                return mock.Mock(stdout="List of available languages (2):\neng\nben\n")
            base = cmd[2]
            from pathlib import Path
            Path(base + ".pdf").write_bytes(text_pdf(1, prefix="Recognised"))
            return mock.Mock(stdout="")
        with mock.patch.object(ocr, "find_tesseract", return_value="tesseract"), mock.patch("subprocess.run", fake_run):
            out = self.call(file="scan.pdf")
        self.assertIn("Recognised 1 page(s) with Tesseract (eng+ben)", out)
        self.assertIn("[DOWNLOAD:", out)
        result = PdfReader(io.BytesIO(self.out(out.split("\n")[0])))
        self.assertIn("Recognised 1 heading", result.pages[0].extract_text())

    def test_the_command_line_holds_no_user_text(self):
        self.put("scan.pdf", scanned_pdf())
        cmds = []

        def fake_run(cmd, **kw):
            cmds.append(cmd)
            if "--list-langs" in cmd:
                return mock.Mock(stdout="List\neng\n")
            from pathlib import Path
            Path(cmd[2] + ".pdf").write_bytes(text_pdf(1))
            return mock.Mock(stdout="")
        with mock.patch.object(ocr, "find_tesseract", return_value="tesseract"), mock.patch("subprocess.run", fake_run):
            self.call(file="scan.pdf", lang="eng")
        run_cmd = next(c for c in cmds if "--list-langs" not in c)
        self.assertEqual(run_cmd[0], "tesseract")
        self.assertEqual(run_cmd[3:], ["-l", "eng", "pdf"])
        self.assertTrue(run_cmd[1].endswith(".png"))

    def test_without_tesseract_the_vision_model_transcribes_text_only(self):
        self.put("scan.pdf", scanned_pdf())

        async def fake(data, question="", force_local=False):
            return "Invoice 1042 total 4,800"
        with mock.patch.object(ocr, "find_tesseract", return_value=None), mock.patch("core.small_model.vision.describe_image_bytes", fake):
            out = self.call(file="scan.pdf")
        self.assertIn("vision model transcribed", out)
        self.assertIn("Invoice 1042", out)
        self.assertNotIn("[DOWNLOAD:", out)

    def test_with_neither_the_message_says_how_to_enable_it(self):
        self.put("scan.pdf", scanned_pdf())

        async def fake(data, question="", force_local=False):
            return "error: no image model available"
        with mock.patch.object(ocr, "find_tesseract", return_value=None), mock.patch("core.small_model.vision.describe_image_bytes", fake):
            out = self.call(file="scan.pdf")
        self.assertTrue(out.startswith("error:"))
        self.assertIn("Tesseract", out)

    def test_the_switch(self):
        self.put("scan.pdf", scanned_pdf())
        with mock.patch.object(ocr, "enabled", return_value=False):
            self.assertIn("switched off", self.call(file="scan.pdf"))


if __name__ == "__main__":
    unittest.main()
