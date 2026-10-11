import tempfile
import unittest
from pathlib import Path

from core import kb_mentions, kb_structure, knowledge_ingest
from core.memory.constants import _chunk_text, _knowledge_id_from_path


def entries_for(text, kid=1):
    return [("knowledge", f"kb:{kid}", c, None) for c in kb_structure.add_context(_chunk_text(text, 10000))]


class RowsToTextTests(unittest.TestCase):
    def test_header_row_rows_and_numbers(self):
        t = kb_structure.rows_to_text([["Name", "Role", "Phone"], ["Alice", "Eng", "0171"]], "Staff")
        self.assertEqual(t.splitlines(), ["[Sheet: Staff]", "Columns: Name | Role | Phone", "R2: Alice | Eng | 0171"])

    def test_empty_cells_keep_their_column(self):
        t = kb_structure.rows_to_text([["Name", "Role", "Phone"], ["Alice", None, "0171"]])
        self.assertIn("R2: Alice |  | 0171", t)

    def test_header_repeats_so_every_chunk_can_carry_it(self):
        rows = [["Name", "Role"]] + [[f"P{i}", "x"] for i in range(40)]
        t = kb_structure.rows_to_text(rows)
        self.assertGreaterEqual(t.count("Columns: Name | Role"), 3)

    def test_numeric_first_row_is_data_not_a_header(self):
        t = kb_structure.rows_to_text([[1, 2, 3], [4, 5, 6]])
        self.assertNotIn("Columns:", t)

    def test_blank_rows_are_skipped_but_row_numbers_stay_true(self):
        t = kb_structure.rows_to_text([["A", "B"], [None, None], ["x", "y"]])
        self.assertIn("R3: x | y", t)


class AddContextTests(unittest.TestCase):
    def test_later_chunks_get_sheet_columns_and_page(self):
        out = kb_structure.add_context(["[Sheet: S]\nColumns: a | b\nR2: 1 | 2", "R3: 3 | 4", "[Page 4]\ntext", "more"])
        self.assertTrue(out[1].startswith("[Sheet: S]\nColumns: a | b\n"))
        self.assertTrue(out[3].endswith("more") and "[Page 4]" in out[3])

    def test_a_new_sheet_drops_the_old_columns(self):
        out = kb_structure.add_context(["[Sheet: A]\nColumns: a | b\nR2: 1 | 2", "[Sheet: B]\nR1: x", "R2: y"])
        self.assertNotIn("Columns: a | b", out[2])
        self.assertIn("[Sheet: B]", out[2])

    def test_plain_text_is_unchanged(self):
        self.assertEqual(kb_structure.add_context(["a", "b"]), ["a", "b"])


class RoundTripTests(unittest.TestCase):
    def test_xlsx_row_is_found_with_its_header_sheet_and_row_number(self):
        import openpyxl
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "staff.xlsx"
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "Dhaka"
            ws.append(["Name", "Role", "Phone"])
            for i in range(60):
                ws.append([f"Person {i}", "Engineer", f"01{i:03d}"])
            ws.append(["Rahim Uddin", None, "0199"])                # empty Role cell must not shift the phone
            wb.create_sheet("Khulna").append(["Employee", "Grade"])
            wb["Khulna"].append(["Rahim Uddin", "G7"])
            wb.save(path)
            text = knowledge_ingest.extract_xlsx(path)
        r = kb_mentions.find_mentions("Rahim Uddin", entries_for(text), {1: "Staff"}, _knowledge_id_from_path, {1})
        out = kb_mentions.render("Rahim Uddin", r)
        self.assertIn("Columns: Name | Role | Phone", out)
        self.assertIn("Sheet Dhaka, row 62] Rahim Uddin |  | 0199", out)
        self.assertIn("Columns: Employee | Grade", out)             # the second sheet has its own header
        self.assertIn("Sheet Khulna", out)

    def test_csv_keeps_columns_aligned(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "a.csv"
            path.write_text("Name,Role,Phone\nAlice,,0171\n", encoding="utf-8")
            text = knowledge_ingest.extract_csv(path)
        self.assertIn("R2: Alice |  | 0171", text)

    def test_pdf_pages_are_marked(self):
        from reportlab.pdfgen.canvas import Canvas
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "a.pdf"
            c = Canvas(str(path))
            c.drawString(72, 720, "first page text")
            c.showPage()
            c.drawString(72, 720, "Rahim Uddin is the CFO")
            c.showPage()
            c.save()
            text = knowledge_ingest.extract_pdf(path)
        r = kb_mentions.find_mentions("Rahim Uddin", entries_for(text), {1: "Handbook"}, _knowledge_id_from_path, {1})
        self.assertIn("p.2", kb_mentions.render("Rahim Uddin", r))


if __name__ == "__main__":
    unittest.main()
