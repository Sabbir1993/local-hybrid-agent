"""PDF forms, true redaction and password protection."""
import io
import unittest

import core.agent_tools  # noqa: F401
from pypdf import PdfReader

from tests.pdf_fixtures import text_pdf
from tests.test_pdf_tools import Base


def form_pdf() -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.drawString(72, 780, "Application form")
    c.acroForm.textfield(name="full_name", x=72, y=700, width=200, height=20, borderStyle="inset")
    c.acroForm.textfield(name="city", x=72, y=660, width=200, height=20, borderStyle="inset")
    c.acroForm.checkbox(name="agree", x=72, y=620, size=14, checked=False)
    c.acroForm.choice(name="plan", x=72, y=580, width=150, height=20, options=["Basic", "Pro", "Enterprise"], value="Basic")
    c.showPage()
    c.save()
    return buf.getvalue()


class Forms(Base):
    def test_describe_lists_fields_and_types(self):
        self.put("f.pdf", form_pdf())
        out = self.tool("pdf_form", file="f.pdf", describe=True)
        self.assertIn("4 form field(s)", out)
        self.assertIn("full_name (text)", out)
        self.assertIn("plan (choice)", out)
        self.assertIn("Enterprise", out)

    def test_fill_saves_a_new_file_with_the_values(self):
        self.put("f.pdf", form_pdf())
        res = self.tool("pdf_form", file="f.pdf", values={"full_name": "Ayesha Rahman", "city": "Dhaka"})
        self.assertIn("Filled 2 field(s)", res)
        fields = PdfReader(io.BytesIO(self.out(res))).get_fields()
        self.assertEqual(str(fields["full_name"].get("/V")), "Ayesha Rahman")
        self.assertEqual(str(fields["city"].get("/V")), "Dhaka")

    def test_an_unknown_field_is_an_error_that_lists_the_real_ones(self):
        self.put("f.pdf", form_pdf())
        res = self.tool("pdf_form", file="f.pdf", values={"surname": "X"})
        self.assertTrue(res.startswith("error:"))
        self.assertIn("full_name", res)

    def test_a_pdf_without_a_form(self):
        self.put("p.pdf", text_pdf(1))
        self.assertIn("no fillable form fields", self.tool("pdf_form", file="p.pdf", describe=True))
        self.assertTrue(self.tool("pdf_form", file="p.pdf", values={"a": "b"}).startswith("error:"))


class Redaction(Base):
    def text_of(self, link):
        import pdfplumber
        with pdfplumber.open(io.BytesIO(self.out(link))) as pdf:
            return "\n".join((p.extract_text() or "") for p in pdf.pages)

    def test_terms_are_really_removed_from_text_and_bytes(self):
        self.put("d.pdf", text_pdf(3, extra={2: "Contact Rahman Hossain about the Hercules deal"}))
        res = self.tool("pdf_redact", file="d.pdf", terms=["Rahman Hossain"])
        self.assertIn("Redacted 1 item(s) on 1 page(s)", res)
        self.assertIn("Verified", res)
        text = self.text_of(res)
        self.assertNotIn("Hossain", text)
        self.assertNotIn(b"Hossain", self.out(res))
        self.assertIn("Page 1 heading", text)                           # untouched pages keep their text layer
        self.assertIn("Page 3 heading", text)

    def test_card_numbers_are_redacted_but_other_numbers_are_not(self):
        self.put("c.pdf", text_pdf(1, extra={1: "Card 4111 1111 1111 1111 and invoice 1234 5678 9012 3456 code"}))
        res = self.tool("pdf_redact", file="c.pdf", pan=True)
        self.assertIn("1 card number", res)
        self.assertNotIn("4111", self.text_of(res))

    def test_patterns(self):
        self.put("p.pdf", text_pdf(1, extra={1: "Ref ABC-12345 and ABC-99999 end"}))
        res = self.tool("pdf_redact", file="p.pdf", patterns=[r"ABC-\d{5}"])
        self.assertIn("2 pattern", res)
        self.assertNotIn("ABC-", self.text_of(res))

    def test_no_match_saves_nothing(self):
        self.put("d.pdf", text_pdf(2))
        res = self.tool("pdf_redact", file="d.pdf", terms=["zzzzqq"])
        self.assertTrue(res.startswith("error: no match"))
        self.assertNotIn("DOWNLOAD", res)

    def test_bad_requests(self):
        self.put("d.pdf", text_pdf(1))
        self.assertIn("nothing to redact", self.tool("pdf_redact", file="d.pdf"))
        self.assertIn("not a valid regular expression", self.tool("pdf_redact", file="d.pdf", patterns=["("]))

    def test_the_original_is_kept(self):
        data = text_pdf(1, extra={1: "secret word here"})
        self.put("d.pdf", data)
        self.tool("pdf_redact", file="d.pdf", terms=["secret"])
        self.assertEqual((self.root / "user_21" / "d.pdf").read_bytes(), data)


class Protection(Base):
    def test_protect_then_unprotect(self):
        self.put("d.pdf", text_pdf(2))
        res = self.tool("pdf_protect", file="d.pdf", action="protect", user_password="open-sesame")
        self.assertIn("AES-256", res)
        locked = PdfReader(io.BytesIO(self.out(res)))
        self.assertTrue(locked.is_encrypted)
        self.put("locked.pdf", self.out(res))
        self.assertIn("password protected", self.tool("pdf_read", file="locked.pdf"))
        res2 = self.tool("pdf_protect", file="locked.pdf", action="unprotect", password="open-sesame")
        self.assertFalse(PdfReader(io.BytesIO(self.out(res2))).is_encrypted)

    def test_rules(self):
        self.put("d.pdf", text_pdf(1))
        self.assertIn("at least 6", self.tool("pdf_protect", file="d.pdf", action="protect", user_password="abc"))
        self.assertIn("not password protected", self.tool("pdf_protect", file="d.pdf", action="unprotect", password="whatever"))
        self.put("locked.pdf", self.out(self.tool("pdf_protect", file="d.pdf", action="protect", user_password="open-sesame")))
        self.assertIn("does not open", self.tool("pdf_protect", file="locked.pdf", action="unprotect", password="wrong-one"))

    def test_the_password_never_appears_in_the_result_text(self):
        self.put("d.pdf", text_pdf(1))
        self.assertNotIn("open-sesame", self.tool("pdf_protect", file="d.pdf", action="protect", user_password="open-sesame"))


if __name__ == "__main__":
    unittest.main()
