"""A workbook built in steps through the real doc tool path (doc_create, then doc_edit add_outline_sheet), the outline ops added for
restyling, and the auto-design note the tools give back."""
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import openpyxl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import agent_tools, doc_store, doc_tools  # noqa: E402
import core.small_model  # noqa: F401,E402
from core.request_context import set_current_user  # noqa: E402
from core.sheet_engine import build as B  # noqa: E402
from core.sheet_engine import calc  # noqa: E402
from core.sheet_engine import edit as E  # noqa: E402
from tests.test_doc_ops import doc_files_db  # noqa: E402


def _name(out: str) -> str:
    return out.split("[DOWNLOAD: ")[1].split("]")[0]


def _table(i: int) -> str:
    rows = "\n".join(f"| 2026-0{m}-05 | Dhaka | {10 * m + i} | {1000 * m + i} | {400 * m + i} |" for m in range(1, 5))
    return f"| Date | Region | Units | Revenue | Cost |\n|---|---|---|---|---|\n{rows}"


class StepBuild(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._patches = [mock.patch.object(agent_tools, "COMMON_ROOT", self.root),
                         mock.patch.object(doc_store, "_projects_db", doc_files_db()),
                         mock.patch.object(calc, "_prefer_external", lambda *a, **k: None)]   # never Excel / LibreOffice
        for p in self._patches:
            p.start()
        set_current_user(31)

    def tearDown(self):
        for p in self._patches:
            p.stop()
        set_current_user(None)
        self.tmp.cleanup()

    def _latest(self) -> Path:
        files = sorted((self.root / "user_31").glob("*.xlsx"), key=lambda p: p.stat().st_mtime)
        return files[-1]

    def test_two_sheets_then_six_more_in_steps(self):
        first = "# Regions\n## Sheet: Sheet A\n" + _table(0) + "\n## Sheet: Sheet B\n" + _table(1)
        out = doc_tools.tool_doc_create_common({"file": "regions.xlsx", "content": first})
        self.assertIn("Design added automatically", out, out)
        name = _name(out)
        for i in range(2, 8):
            res = doc_tools.tool_doc_edit_common({"file": name, "ops": [
                {"op": "add_outline_sheet", "name": f"Sheet {chr(65 + i)}", "content": _table(i)}]})
            self.assertIn("Design added automatically", res, res)
            self.assertEqual(res.count("Design added automatically"), 1)
            name = _name(res)
        wb = openpyxl.load_workbook(self._latest())
        data_sheets = [f"Sheet {c}" for c in "ABCDEFGH"]
        self.assertEqual([n for n in wb.sheetnames if n in data_sheets], data_sheets)
        self.assertIn("Contents", wb.sheetnames)
        self.assertIn("Dashboard", wb.sheetnames)
        contents = wb["Contents"]
        linked = {c.value: c.hyperlink.location for row in contents.iter_rows() for c in row if c.hyperlink and c.value}
        for n in data_sheets:
            self.assertIn(n, linked, f"{n} missing from Contents")
            self.assertIn(n, linked[n])                               # the link points at that sheet
        res = calc.calculate(self._latest().read_bytes())
        self.assertEqual(res["errors"], [])
        self.assertGreater(res["formulas"], 0)                        # the Dashboard KPIs are live formulas
        dash_formulas = [c.value for row in wb["Dashboard"].iter_rows() for c in row if isinstance(c.value, str) and c.value.startswith("=")]
        self.assertTrue(dash_formulas)

    def test_a_plain_table_doc_create_can_be_restyled_afterwards(self):
        out = doc_tools.tool_doc_create_common({"file": "plain.xlsx", "content":
                                                "| Item | Qty | Status |\n|---|---|---|\n| Pens | 10 | Done |\n| Ink | 4 | Pending |\n"})
        name = _name(out)
        row = doc_store.find(31, name)
        self.assertTrue(B.is_outline(row["source_spec"]), row["source_spec"])      # the outline, not the raw text
        res = doc_tools.tool_doc_edit_common({"file": name, "ops": [{"op": "set_banner", "sheet": "Sheet1", "text": "Stock list"}]})
        self.assertNotIn("error", res.lower(), res)
        ws = openpyxl.load_workbook(self._latest()).worksheets[0]
        self.assertIn("Stock list", [c.value for row in ws.iter_rows() for c in row])


class OutlineOps(unittest.TestCase):
    BASE = ("# T\n## Sheet: Sales\n| Region | Revenue |\n|---|---|\n| A | 10 |\n| B | 20 |\n"
            "## Dashboard: Overview\nkpi: Total = =SUM(Sales!B2:B3)\n"
            "## Sheet: Costs\n| Item | Cost |\n|---|---|\n| X | 5 |\n")

    def test_sections_know_dashboards(self):
        secs = E._sections(self.BASE.split("\n"))
        self.assertEqual([s[0] for s in secs], ["Sales", "Overview", "Costs"])
        out, _ = E.apply_outline_ops(self.BASE, [{"op": "add_note", "sheet": "Sales", "text": "hi"}])
        lines = out.split("\n")
        self.assertLess(lines.index("note: hi"), lines.index("## Dashboard: Overview"))

    def test_new_ops_write_their_directive(self):
        ops = [{"op": "set_banner", "sheet": "Sales", "text": "Sales 2026"}, {"op": "set_subtitle", "sheet": "Sales", "text": "BDT"},
               {"op": "set_banner", "sheet": "Sales", "text": "Sales 2027"},
               {"op": "set_input", "sheet": "Sales", "columns": ["Revenue"]}, {"op": "add_merge", "sheet": "Costs", "range": "a1:b1", "text": "Hi"},
               {"op": "set_color", "sheet": "Costs", "target": "Cost", "fill": "FFF2CC", "font": "0000FF"},
               {"op": "add_icons", "sheet": "Costs", "column": "Cost"}, {"op": "set_auto", "sheet": "Costs", "on": False},
               {"op": "set_style", "value": "plain"},
               {"op": "add_dashboard", "name": "KPIs", "content": "title: K\nkpi: Rev = =SUM(Sales!B2:B3)"}]
        out, changes = E.apply_outline_ops(self.BASE, ops)
        self.assertEqual(out.count("banner:"), 1)
        for needle in ("banner: Sales 2027", "subtitle: BDT", "input: Revenue", 'merge: "Hi"=A1:B1', "color: Cost fill=FFF2CC font=0000FF",
                       "icons: Cost", "auto: off", "style: plain", "## Dashboard: KPIs"):
            self.assertIn(needle, out)
        self.assertLess(out.index("style: plain"), out.index("## Sheet: Sales"))
        out2, _ = E.apply_outline_ops(out, [{"op": "set_auto", "sheet": "Costs", "on": True}, {"op": "set_style", "value": "auto"}])
        self.assertNotIn("auto: off", out2)
        self.assertNotIn("style: plain", out2)
        self.assertEqual(len(changes), len(ops))

    def test_errors_name_the_valid_choices(self):
        with self.assertRaises(E.EditError) as cm:
            E.apply_outline_ops(self.BASE, [{"op": "set_theme", "theme": "neon"}])
        self.assertIn("corporate", str(cm.exception))
        with self.assertRaises(E.EditError):
            E.apply_outline_ops(self.BASE, [{"op": "add_dashboard", "name": "D", "content": "note: nothing"}])
        with self.assertRaises(E.EditError):
            E.apply_outline_ops(self.BASE, [{"op": "add_dashboard", "name": "Sales", "content": "kpi: a = =1"}])

    def test_the_edited_outline_builds(self):
        data, outline, _ = E.edit(self.BASE, [{"op": "set_banner", "sheet": "Sales", "text": "Sales 2026"},
                                              {"op": "add_dashboard", "name": "More", "content": "kpi: Cost = =SUM(Costs!B2:B2)"}])
        names = openpyxl.load_workbook(io.BytesIO(data)).sheetnames
        self.assertIn("More", names)

    def test_is_outline_sees_dashboards_and_report_directives(self):
        self.assertTrue(B.is_outline("## Dashboard: X\nkpi: a = =1"))
        for d in ("banner: x", "subtitle: y", "kpi: a = =1", "input: Qty", "style: plain", "icons: Qty", "rowformat: A=0%", "link: Item"):
            self.assertTrue(B.is_outline("| A | B |\n|---|---|\n| 1 | 2 |\n" + d), d)
        self.assertFalse(B.is_outline("| A | B |\n|---|---|\n| 1 | 2 |\n"))


class CutOffAdvice(unittest.TestCase):
    def test_documents_keep_their_format(self):
        from routes.chat.file_intent import chat_cut_call_error
        self.assertIn("add_outline_sheet", chat_cut_call_error("r.xlsx"))
        self.assertNotIn("index.html", chat_cut_call_error("r.xlsx"))
        self.assertIn("doc_append", chat_cut_call_error("r.docx"))
        self.assertIn("index.html", chat_cut_call_error("site.html"))
        self.assertIn("index.html", chat_cut_call_error(""))
        self.assertNotIn("index.html", chat_cut_call_error("tool.py"))

    def test_agent_notices_use_the_path(self):
        from core.agent_loop.truncation import CUT_CALL_NOTICE, cut_call_notice, invalid_args_error, INVALID_ARGS_ERROR
        calls = [{"function": {"name": "write_file", "arguments": '{"path": "plan.xlsx", "content": "| a'}}]
        self.assertIn("add_outline_sheet", cut_call_notice(calls))
        self.assertTrue(cut_call_notice(calls).startswith("[cut off]"))
        self.assertEqual(cut_call_notice([{"function": {"arguments": '{"path": "a.py", "content": "x'}}]), CUT_CALL_NOTICE)
        self.assertEqual(invalid_args_error("a.py"), INVALID_ARGS_ERROR)
        self.assertIn("doc_append", invalid_args_error("a.pptx"))


if __name__ == "__main__":
    unittest.main()


class SheetCap(unittest.TestCase):
    def test_contents_and_dashboard_never_push_past_the_sheet_limit(self):
        for n in (28, 29, 30):
            outline = "# T\n" + "".join(f"## Sheet: S{i}\n| A | B | C |\n|---|---|---|\n| x | {i} | {i + 1} |\n| y | 2 | 3 |\n" for i in range(n))
            data = B.build(outline, "t", True)
            self.assertLessEqual(len(openpyxl.load_workbook(io.BytesIO(data)).sheetnames), B.MAX_SHEETS, n)
