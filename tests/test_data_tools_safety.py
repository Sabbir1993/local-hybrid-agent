"""Spreadsheet tools: card numbers never leave unmasked, generated workbooks never carry injected formulas, and csv / id
columns are read as written. The card number used is the public Visa test number."""
import csv
import io
import tempfile
import unittest
from pathlib import Path

import openpyxl

from core.file_tools import profile
from core.file_tools.data_query import run_data_query
from core.sheet_engine import build, clean, sql

TEST_CARD = "4111111111111111"          # public test number, not a real card


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def xlsx(self, name, rows):
        wb = openpyxl.Workbook()
        ws = wb.active
        for r in rows:
            ws.append(r)
        p = self.dir / name
        wb.save(p)
        return p

    def csv(self, name, data: bytes):
        p = self.dir / name
        p.write_bytes(data)
        return p


class CardMaskingTests(Base):
    def cards(self, as_number):
        card = int(TEST_CARD) if as_number else TEST_CARD
        rows = [["txn", "card", "amount"]] + [[f"T{i}", None, 100 + i] for i in range(6)] + [["T9", card, 500]]
        return self.xlsx("gw.xlsx", rows)

    def test_profile_never_shows_a_card_number(self):
        for as_number in (True, False):
            text, _ = profile.profile_file(self.cards(as_number))
            self.assertNotIn(TEST_CARD, text)
            self.assertNotIn("4,111,111,111,111,111", text)

    def test_data_query_masks_rows_and_describe(self):
        for as_number in (True, False):
            p = self.cards(as_number)
            out = run_data_query(p, {"select": ["txn", "card"], "filters": [{"column": "txn", "op": "=", "value": "T9"}]})
            self.assertIn("T9", out)
            self.assertNotIn(TEST_CARD, out)
            self.assertNotIn("4,111,111", out)
            self.assertNotIn(TEST_CARD, run_data_query(p, {"describe": True}))

    def test_data_sql_masks_a_card_stored_as_a_number(self):
        wb = sql._build([str(self.cards(True))])
        out = sql.query(wb, 'SELECT txn, card FROM "Sheet" WHERE card IS NOT NULL')
        self.assertIn("T9", out)
        self.assertNotIn(TEST_CARD, out)
        self.assertNotIn("4,111,111", out)

    def test_long_whole_numbers_are_printed_without_separators(self):
        self.assertEqual(profile.plain_long_int(4111111111111111.0), TEST_CARD)
        self.assertIsNone(profile.plain_long_int(1200000.0))
        self.assertIsNone(profile.plain_long_int(12.5))


class TypingTests(Base):
    def test_phone_and_long_ids_stay_text_and_currency_is_a_number(self):
        self.assertEqual(profile._kind("01712345678"), "text")
        self.assertEqual(profile._kind(TEST_CARD), "text")
        self.assertEqual(profile._kind("1,200.50"), "number")
        self.assertEqual(profile.parse_number("৳1,200"), 1200.0)
        self.assertEqual(profile.parse_number("Tk 500"), 500.0)
        self.assertEqual(profile.parse_number("1,200.50 BDT"), 1200.5)
        self.assertIsNone(profile.parse_number("Tk"))

    def test_a_header_with_a_double_quote_loads(self):
        p = self.xlsx("s.xlsx", [['Size 5"', "qty"], ["a", 1], ["b", 2]])
        wb = sql._build([str(p)])
        self.assertIn("(2 rows)", sql.query(wb, 'SELECT * FROM "Sheet"'))

    def test_memory_bombs_are_refused(self):
        wb = sql._build([str(self.xlsx("s.xlsx", [["a"], [1]]))])
        for q in ("SELECT randomblob(1000000000)", "SELECT zeroblob(1000000000)"):
            with self.assertRaises(sql.SqlError):
                sql.query(wb, q)


class CsvTests(Base):
    def test_windows_1252_file_is_not_read_as_utf16(self):
        t = profile.read_tables(self.csv("a.csv", "name,city\nJosé,Zürich\n".encode("cp1252")))[0]
        self.assertEqual(t.headers, ["name", "city"])
        self.assertEqual(t.rows[0], ["José", "Zürich"])

    def test_utf8_bangla_with_bom(self):
        t = profile.read_tables(self.csv("b.csv", "নাম,শহর\nরহিম,ঢাকা\n".encode("utf-8-sig")))[0]
        self.assertEqual(t.headers, ["নাম", "শহর"])

    def test_utf16_with_bom(self):
        t = profile.read_tables(self.csv("c.csv", "a,b\n1,2\n".encode("utf-16")))[0]
        self.assertEqual(t.headers, ["a", "b"])

    def test_line_break_inside_a_quoted_cell_is_kept(self):
        buf = io.StringIO()
        csv.writer(buf).writerows([["id", "note"], ["1", "line one\nline two"], ["2", "x"]])
        t = profile.read_tables(self.csv("d.csv", buf.getvalue().encode()))[0]
        self.assertEqual(len(t.rows), 2)
        self.assertEqual(t.rows[0][1], "line one\nline two")


class FormulaInjectionTests(Base):
    def formulas_in(self, data: bytes) -> list:
        wb = openpyxl.load_workbook(io.BytesIO(data))
        return [c.value for ws in wb.worksheets for row in ws.iter_rows() for c in row if c.data_type == "f"]

    def test_data_clean_keeps_formula_looking_text_as_text(self):
        p = self.csv("x.csv", b'name,link\nA,"=HYPERLINK(""http://evil.example/x"",""click"")"\nB,=1+1\n')
        data, _ = clean.run(p, [{"op": "trim"}])
        self.assertEqual(self.formulas_in(data), [])
        ws = openpyxl.load_workbook(io.BytesIO(data)).worksheets[0]
        self.assertTrue(str(ws["B2"].value).startswith("=HYPERLINK"))

    def test_data_clean_wrong_sheet_is_an_error(self):
        p = self.xlsx("s.xlsx", [["a"], [1]])
        with self.assertRaises(clean.CleanError):
            clean.run(p, [{"op": "trim"}], "Nope")

    def test_builder_headers_and_titles_are_text(self):
        outline = ('## Sheet: Data\n| =WEBSERVICE("http://evil.example") | Amount |\n|---|---|\n| a | 5 |\n'
                   'merge: "=1+1"=D1:E1\nhighlight: Amount > 3 red\n')
        data = build.build(outline, formulas=False)
        self.assertEqual(self.formulas_in(data), [])

    def test_conditional_format_text_cannot_leave_its_quotes(self):
        self.assertEqual(build._quoted('a"&WEBSERVICE("x")&"b'), '"a""&WEBSERVICE(""x"")&""b"')


if __name__ == "__main__":
    unittest.main()
