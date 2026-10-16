"""Working on a workbook that already exists in the chat: one column of one sheet, the right file, no second copy."""
import io
import unittest

import core.agent_tools  # noqa: F401
import openpyxl

import core.doc_ops as D
from routes.chat import delivery as dv
from routes.chat import file_intent as fi

BOOK = """# Break-even
## Sheet: Costs
| Item | Cost (BDT) | Month |
|---|---|---|
| Dev | 85000 | 1 |
| Ops | 3000 | 2 |
| Licences | 1200 | 3 |
## Sheet: Revenue
| Month | Revenue |
|---|---|
| 1 | 0 |
| 2 | 500 |
"""


def make():
    data, spec = D.create("b.xlsx", BOOK)
    return data, spec


def vals(data, sheet):
    return [[c.value for c in r] for r in openpyxl.load_workbook(io.BytesIO(data))[sheet].iter_rows()]


class SetColumn(unittest.TestCase):
    def test_values_go_into_that_column_only(self):
        data, spec = make()
        out = D.edit(data, "b.xlsx", [{"op": "set_column", "sheet": "Costs", "column": "Cost (BDT)", "values": [90000, 3500, 1500]}], source_spec=spec)
        rows = vals(out.data, "Costs")
        self.assertEqual([r[1] for r in rows[1:]], [90000, 3500, 1500])
        self.assertEqual([r[0] for r in rows[1:]], ["Dev", "Ops", "Licences"])          # other columns untouched
        self.assertEqual([r[2] for r in rows[1:]], [1, 2, 3])
        self.assertEqual(vals(out.data, "Revenue"), vals(data, "Revenue"))                  # the other sheet is untouched

    def test_a_formula_column_found_by_letter_or_partial_header(self):
        data, spec = make()
        out = D.edit(data, "b.xlsx", [{"op": "set_column", "sheet": "Costs", "column": "month", "formula": "=B{r}*2"}], source_spec=spec)
        self.assertEqual([r[2] for r in vals(out.data, "Costs")[1:]], ["=B2*2", "=B3*2", "=B4*2"])
        out2 = D.edit(data, "b.xlsx", [{"op": "set_column", "sheet": "Revenue", "column": "B", "values": [10, 20]}], source_spec=spec)
        self.assertEqual([r[1] for r in vals(out2.data, "Revenue")[1:]], [10, 20])

    def test_a_missing_column_or_sheet_says_what_exists(self):
        data, spec = make()
        for op, needle in (({"op": "set_column", "sheet": "Costs", "column": "Nope", "values": [1]}, "Item"),
                           ({"op": "set_column", "sheet": "Nowhere", "column": "Cost", "values": [1]}, "Costs"),
                           ({"op": "set_column", "sheet": "Costs", "column": "Item"}, "values")):
            with self.assertRaises(Exception) as cm:
                D.edit(data, "b.xlsx", [op], source_spec=spec)
            self.assertIn(needle, str(cm.exception))

    def test_cell_formulas_obey_the_allow_list_on_the_xml_path_too(self):
        data, spec = make()
        for f in ('=HYPERLINK("http://x","y")', '=WEBSERVICE("http://x")', "=INDIRECT(\"A1\")"):
            with self.assertRaises(Exception) as cm:
                D.edit(data, "b.xlsx", [{"op": "set_formula", "addr": "Costs!D2", "formula": f}], source_spec=spec)
            self.assertIn("not allowed", str(cm.exception))
        ok = D.edit(data, "b.xlsx", [{"op": "set_formula", "addr": "Costs!D2", "formula": "=SUM(B2:B4)"}], source_spec=spec)
        self.assertEqual(vals(ok.data, "Costs")[1][3], "=SUM(B2:B4)")

    def test_a_refused_formula_in_a_range_write_is_kept_as_text_and_reported(self):
        data, spec = make()
        out = D.edit(data, "b.xlsx", [{"op": "set_range", "addr": "Costs!D2", "formulas": True,
                                       "values": [["=SUM(B2:B4)"], ['=INDIRECT("A1")'], ["=B3*2"]]}], source_spec=spec)
        col = [r[3] for r in vals(out.data, "Costs")[1:4]]
        self.assertEqual(col[0], "=SUM(B2:B4)")
        self.assertEqual(col[2], "=B3*2")                              # the good formulas around it were still written
        self.assertEqual(col[1], '=INDIRECT("A1")')                    # text, not a formula
        self.assertTrue(any("skipped" in n and "not allowed" in n for n in out.notes))
        import zipfile
        xml = zipfile.ZipFile(io.BytesIO(out.data)).read("xl/worksheets/sheet1.xml").decode()
        self.assertNotIn("INDIRECT</f>", xml)


class FollowUp(unittest.TestCase):
    FILES = ["Hercules_TAP-3d384188.md", "Hercules_TAP_Bangladesh-a9978806.xlsx"]

    def test_sheet_talk_with_a_workbook_in_the_chat_is_an_edit_of_it(self):
        for q in ("the break-even sheet needs a monthly column", "add a Total row to the cost tab", "change column C to percent",
                  "update the formulas in the summary"):
            self.assertEqual(fi.file_followup_intent(q, True, self.FILES), "edit", q)

    def test_answering_the_assistants_own_question_continues_the_file(self):
        self.assertEqual(fi.file_followup_intent("Q7: Break-even Point: Month 8-10", True, self.FILES, asked=True), "edit")
        self.assertIsNone(fi.file_followup_intent("Q7: Break-even Point: Month 8-10", True, self.FILES, asked=False))

    def test_a_request_for_a_new_workbook_is_not_an_edit(self):
        for q in ("make a new spreadsheet for the Dhaka team", "create another workbook for hiring", "I need a separate excel for budget"):
            self.assertNotEqual(fi.file_followup_intent(q, True, self.FILES), "edit", q)

    def test_without_a_workbook_nothing_changes(self):
        self.assertIsNone(fi.file_followup_intent("the cost column looks wrong", True, ["a.md"]))
        self.assertEqual(fi.file_followup_intent("where is the file", True, self.FILES), "where")


class NoSecondCopy(unittest.TestCase):
    def test_a_second_workbook_is_refused_unless_a_new_one_was_asked_for(self):
        files = ["Hercules_TAP_Bangladesh-a9978806.xlsx"]
        self.assertTrue(dv._duplicate_workbook(files, {"path": "Hercules_TAP_breakeven.xlsx"}, "add the break-even point"))
        self.assertTrue(dv._duplicate_workbook(files, {"file": "x.xlsx"}, "Q7: Break-even Point: Month 8-10"))
        self.assertFalse(dv._duplicate_workbook(files, {"path": "x.xlsx"}, "make a new spreadsheet for hiring"))
        self.assertFalse(dv._duplicate_workbook(files, {"path": "notes.md"}, "add the break-even point"))
        self.assertFalse(dv._duplicate_workbook([], {"path": "x.xlsx"}, "make a spreadsheet"))
        self.assertIn("doc_edit", dv._DUPLICATE_WORKBOOK)

    def test_the_route_uses_it_and_the_manual_forbids_questions(self):
        from pathlib import Path
        from core import prompt_scope as ps
        src = (Path(__file__).resolve().parents[1] / "routes" / "chat" / "run.py").read_text(encoding="utf-8")
        self.assertIn("_duplicate_workbook(", src)
        self.assertIn("file_followup_intent(own_q, bool(prior_files), prior_files, _asked)", src)
        self.assertIn("NEVER ask the user questions", ps.XLSX_MANUAL)

    def test_sandbox_paths_are_scrubbed(self):
        self.assertEqual(dv.scrub_server_paths("(sandbox:/mnt/data/Hercules_TAP-9f0a18af.xlsx)"), "(Hercules_TAP-9f0a18af.xlsx)")
        self.assertEqual(dv.scrub_server_paths("[file](sandbox:/mnt/data/a.xlsx)"), "[DOWNLOAD: a.xlsx]")


if __name__ == "__main__":
    unittest.main()
