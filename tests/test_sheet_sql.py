"""Read-only SQL over sheets: joins, stats, dates, header detection, and the sandbox."""
import datetime
import tempfile
import unittest
from pathlib import Path

import core.agent_tools  # noqa: F401
import openpyxl

from core.file_tools import profile
from core.sheet_engine import sql


def build(d: Path, name: str = "sales.xlsx") -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sales"
    ws["A1"] = "ACME Ltd - Q3 Sales"
    ws.merge_cells("A1:D1")
    ws["A2"] = "prepared by finance"
    for i, h in enumerate(["Date", "Region", "Revenue", "Qty"], 1):
        ws.cell(4, i, h)
    rows = [(datetime.date(2026, 7, 5), "Dhaka", 100, 1), (datetime.date(2026, 7, 20), "Dhaka", 300, 3),
            (datetime.date(2026, 8, 2), "Khulna", 50, 2), (datetime.date(2026, 8, 9), "Dhaka", 200, 2),
            (datetime.date(2026, 9, 1), "Khulna", 150, 5)]
    for r, row in enumerate(rows, 5):
        for c, v in enumerate(row, 1):
            ws.cell(r, c, v)
    ws["C10"] = "=SUM(C5:C9)"
    t = wb.create_sheet("Targets")
    t.append(["Region", "Target"])
    t.append(["Dhaka", 500])
    t.append(["Khulna", 300])
    p = d / name
    wb.save(p)
    return p


class Loader(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = build(Path(self.tmp.name))
        sql.clear_cache()

    def test_the_header_is_found_below_title_rows(self):
        t = profile.read_tables(self.path)[0]
        self.assertEqual(t.headers, ["Date", "Region", "Revenue", "Qty"])
        self.assertIn("ACME", t.preamble[0])
        self.assertEqual(t.total_rows, 5)             # the uncached formula row below the data is blank in the data view

    def test_formula_columns_are_reported(self):
        t = profile.read_tables(self.path)[0]
        self.assertIn("Revenue", t.formulas)

    def test_a_plain_table_still_uses_its_first_row(self):
        t = profile.read_tables(self.path)[1]
        self.assertEqual(t.headers, ["Region", "Target"])
        self.assertEqual(t.preamble, [])


class Queries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = build(Path(self.tmp.name))
        sql.clear_cache()
        self.wb = sql.load(self.path)

    def q(self, text, **kw):
        return sql.query(self.wb, text, **kw)

    def test_describe_lists_tables_and_columns(self):
        d = sql.describe(self.wb)
        self.assertIn("Sales", d)
        self.assertIn('"Revenue" REAL', d)
        self.assertIn("MEDIAN", d)

    def test_group_by_with_a_join_to_another_sheet(self):
        out = self.q('SELECT s.Region, SUM(s.Revenue) AS rev, t.Target, ROUND(100.0*SUM(s.Revenue)/t.Target) AS pct '
                     'FROM Sales s JOIN Targets t ON t.Region = s.Region WHERE s.Region IS NOT NULL GROUP BY s.Region ORDER BY s.Region')
        self.assertIn("| Dhaka | 600 | 500 | 120 |", out)
        self.assertIn("| Khulna | 200 | 300 | 67 |", out)

    def test_month_buckets_and_pivots(self):
        out = self.q("SELECT strftime('%Y-%m', Date) AS m, SUM(CASE WHEN Region='Dhaka' THEN Revenue END) AS dhaka, "
                     "SUM(CASE WHEN Region='Khulna' THEN Revenue END) AS khulna FROM Sales WHERE Date IS NOT NULL GROUP BY m ORDER BY m")
        self.assertIn("| 2026-07 | 400 |", out)
        self.assertIn("| 2026-08 | 200 | 50 |", out)

    def test_median_percentile_stdev_mode(self):
        out = self.q("SELECT MEDIAN(Revenue), PERCENTILE(Revenue, 0.5), ROUND(STDEV(Revenue), 2), MODE(Region) FROM Sales WHERE Region IS NOT NULL")
        self.assertIn("| 150 | 150 |", out)
        self.assertIn("Dhaka", out)

    def test_window_functions(self):
        out = self.q("SELECT Region, Revenue, RANK() OVER (PARTITION BY Region ORDER BY Revenue DESC) AS r FROM Sales WHERE Region IS NOT NULL ORDER BY Region, r")
        self.assertIn("| Dhaka | 300 | 1 |", out)

    def test_row_cap_and_note(self):
        out = self.q("SELECT * FROM Sales", limit=2)
        self.assertIn("more rows exist", out)


class BadNames(unittest.TestCase):
    """A model that guesses a table or column name gets told what does exist, and a mistyped column is reported:
    SQLite reads an unknown double-quoted name as a text constant, so it would otherwise answer with a plausible lie."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = build(Path(self.tmp.name))
        sql.clear_cache()
        self.wb = sql.load(self.path)

    def q(self, text, **kw):
        return sql.query(self.wb, text, **kw)

    def test_a_wrong_table_name_lists_the_tables_that_exist(self):
        with self.assertRaises(sql.SqlError) as cm:
            self.q("SELECT * FROM Sale")
        msg = str(cm.exception)
        self.assertIn("no such table", msg)
        self.assertIn("Sales", msg)                 # what does exist
        self.assertIn("Did you mean", msg)           # the name that was probably meant
        self.assertIn("describe=true", msg)

    def test_a_wrong_column_name_lists_the_columns_that_exist(self):
        with self.assertRaises(sql.SqlError) as cm:
            self.q("SELECT Regoin FROM Sales")          # unquoted: sqlite reports it
        msg = str(cm.exception)
        self.assertIn("no such column", msg)
        self.assertIn('"Region"', msg)

    def test_a_mistyped_column_is_reported_in_the_result(self):
        out = self.q('SELECT "Regoin", COUNT(*) FROM Sales')
        self.assertIn("warning", out)
        self.assertIn("Regoin", out)

    def test_a_text_value_is_not_a_mistyped_column(self):
        out = self.q('SELECT COUNT(*) FROM Sales WHERE Region = "Dhaka"')
        self.assertNotIn("warning", out)

    def test_a_quoted_alias_is_not_a_mistyped_column(self):
        out = self.q('SELECT Region AS "the region", COUNT(*) FROM Sales GROUP BY 1')
        self.assertNotIn("warning", out)

    def test_a_quoted_alias_of_an_unquoted_one_is_not_a_mistyped_column(self):
        out = self.q('SELECT Region, COUNT(*) AS n FROM Sales GROUP BY Region ORDER BY "n"')
        self.assertNotIn("warning", out)

    def test_a_real_column_is_not_reported(self):
        self.assertNotIn("warning", self.q("SELECT Region, COUNT(*) FROM Sales GROUP BY Region"))


class TableNames(unittest.TestCase):
    """A model writes the name it knows - the file name, with the space still a space - not the identifier the table
    really has. The reference is resolved to the table it names, but only when exactly one table fits."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = build(Path(self.tmp.name))
        sql.clear_cache()
        self.wb = sql.load(self.path)

    def q(self, text, **kw):
        return sql.query(self.wb, text, **kw)

    def test_the_file_name_names_the_table(self):
        out = self.q('SELECT Region, SUM(Revenue) FROM "sales.xlsx_Sales" GROUP BY Region')
        self.assertIn("| Dhaka | 600 |", out)

    def test_an_unquoted_file_name_names_the_table(self):
        out = self.q('SELECT Region, SUM(Revenue) FROM sales.xlsx_Sales GROUP BY Region')
        self.assertIn("| Dhaka | 600 |", out)

    def test_a_name_with_a_comma_in_it_names_the_table(self):
        p = build(Path(self.tmp.name), "sales,2026.xlsx")
        sql.clear_cache()
        wb = sql.load(p)
        out = sql.query(wb, 'SELECT Region, SUM(Revenue) FROM "sales,2026.xlsx_Sales" GROUP BY Region')
        self.assertIn("| Dhaka | 600 |", out)

    def test_a_stray_comma_does_not_break_the_name(self):
        p = build(Path(self.tmp.name), "sales,2026.xlsx")
        sql.clear_cache()
        wb = sql.load(p)
        out = sql.query(wb, 'SELECT Region, SUM(Revenue) FROM sales,2026_sales GROUP BY Region')
        self.assertIn("| Dhaka | 600 |", out)

    def test_an_alias_is_kept(self):
        out = self.q('SELECT s.Region FROM Sales s JOIN Targets t ON s.Region = t.Region')
        self.assertIn("| Dhaka |", out)

    def test_a_join_of_two_tables_still_is_one(self):
        out = self.q('SELECT s.Region, t.Target FROM Sales s JOIN Targets t ON s.Region = t.Region')
        self.assertIn("| Dhaka | 500 |", out)

    def test_an_ambiguous_name_is_not_guessed(self):
        """Two tables with the same tab: the short name fits both, so nothing is rewritten and sqlite reports it."""
        wb = sql.from_tables([("Sales_Sheet1", "Sales", ["Region"], [["Dhaka"]]),
                               ("Targets_Sheet1", "Targets", ["Region"], [["Dhaka"]])])
        with self.assertRaises(sql.SqlError) as cm:
            sql.query(wb, "SELECT * FROM Sheet1")
        self.assertIn("no such table", str(cm.exception))

    def test_a_name_the_statement_defines_itself_is_left_alone(self):
        """A common table expression named like a table keeps its own rows: the reference is not pointed at the table."""
        out = self.q('WITH Targets AS (SELECT "Region" FROM Sales) SELECT COUNT(*) FROM Targets')
        self.assertIn("| 5 |", out)                     # the CTE, not the Targets sheet (2 rows)

    def test_a_digit_typed_wrong_finds_the_table(self):
        """'2026' typed as '2_6' is the same name with a wrong digit: the letters are the same and the digits fit."""
        wb = sql.from_tables([("BFSI_activity_nov_2026_Sheet1", "BFSI activity nov 2026.xlsx",
                               ["Kam Name", "Proposal Value"], [["Rahim", 100]], "Sheet1", "K7")])
        out = sql.query(wb, 'SELECT "Kam Name", SUM("Proposal Value") FROM BFSI_activity_nov_2_6_Sheet1 GROUP BY 1')
        self.assertIn("| Rahim | 100 |", out)

    def test_the_table_that_was_read_is_reported(self):
        wb = sql.from_tables([("BFSI_activity_nov_2026_Sheet1", "BFSI activity nov 2026.xlsx",
                               ["Kam Name", "Proposal Value"], [["Rahim", 100]], "Sheet1", "K7")])
        out = sql.query(wb, 'SELECT COUNT(*) FROM "BFSI_activity_nov_2_6_Sheet1"')
        self.assertIn("note: the table name was read as", out)

    def test_a_wrong_year_is_not_read_as_another_year(self):
        """The digits of a wrong year do not fit the year of the table, so nothing is read."""
        wb = sql.from_tables([("BFSI_activity_nov_2026_Sheet1", "BFSI activity nov 2026.xlsx",
                               ["Kam Name"], [["Rahim"]], "Sheet1", "K7")])
        with self.assertRaises(sql.SqlError) as cm:
            sql.query(wb, 'SELECT * FROM "BFSI_activity_nov_2027_Sheet1"')
        self.assertIn("no such table", str(cm.exception))

    def test_a_handle_names_the_table(self):
        out = self.q("SELECT COUNT(*) FROM T1")
        self.assertIn("| 5 |", out)

    def test_a_handle_that_does_not_exist_is_an_error(self):
        with self.assertRaises(sql.SqlError) as cm:
            self.q("SELECT * FROM T9")
        self.assertIn("no such table", str(cm.exception))

    def test_the_query_itself_is_never_changed_beyond_the_table_name(self):
        out = self.q('SELECT Region, COUNT(*) n FROM Sales GROUP BY Region ORDER BY Region')
        self.assertIn("| Dhaka | 3 |", out)


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        sql.clear_cache()
        self.wb = sql.load(build(Path(self.tmp.name)))

    def refused(self, text):
        with self.assertRaises(sql.SqlError):
            sql.query(self.wb, text)

    def test_only_select_is_allowed(self):
        for bad in ("DELETE FROM Sales", "UPDATE Sales SET Revenue = 0", "DROP TABLE Sales", "INSERT INTO Sales VALUES (1,2,3,4)",
                    "PRAGMA table_info(Sales)", "ATTACH DATABASE 'x.db' AS x", "CREATE TABLE z (a)", "", "   "):
            self.refused(bad)

    def test_hidden_tricks_inside_a_select(self):
        self.refused("SELECT load_extension('x')")
        self.refused("SELECT * FROM sqlite_master")
        self.refused("WITH x AS (SELECT 1) DELETE FROM Sales")
        self.refused("SELECT 1; DROP TABLE Sales")

    def test_data_survives_the_attempts(self):
        for bad in ("DELETE FROM Sales", "DROP TABLE Sales"):
            try:
                sql.query(self.wb, bad)
            except sql.SqlError:
                pass
        self.assertIn("| 5 |", sql.query(self.wb, "SELECT COUNT(*) FROM Sales WHERE Region IS NOT NULL"))

    def test_a_runaway_query_is_stopped(self):
        old = sql.QUERY_TIMEOUT_S
        sql.QUERY_TIMEOUT_S = 0.3
        try:
            with self.assertRaises(sql.SqlError) as cm:
                sql.query(self.wb, "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM r) SELECT COUNT(*) FROM r")
        finally:
            sql.QUERY_TIMEOUT_S = old
        self.assertIn("longer", str(cm.exception))

    def test_card_numbers_in_cells_are_masked(self):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Cards"
        ws.append(["name", "card"])
        ws.append(["a", "4111 1111 1111 1111"])
        p = Path(self.tmp.name) / "cards.xlsx"
        wb.save(p)
        out = sql.query(sql.load(p), "SELECT * FROM Cards")
        self.assertNotIn("4111 1111 1111 1111", out)


class AnswerGroundingNotes(unittest.TestCase):
    """Notes that keep a small model from listing one person twice in a "top 5" and from inventing the year."""

    def setUp(self):
        from core.sheet_engine import sql as s
        self.s = s
        self.wb = s.from_tables([("T", "doc", ["Kam Name", "Dept", "Value", "Date"],
                                  [["Sayed", "BFSI", 3000000, "04/11/2026"], ["Ranada", "BFSI", 500000, "08/11/2026"],
                                   ["Jhantu", "BFSI", 300000, "09/11/2026"], ["Jhantu", "BFSI", 300000, "10/11/2026"]])])

    def test_a_top_n_over_single_rows_says_the_name_repeats(self):
        out = self.s.query(self.wb, 'SELECT "Kam Name", "Value" FROM T ORDER BY "Value" DESC LIMIT 5')
        self.assertIn('"Kam Name" repeats (Jhantu is on 2 rows)', out)
        self.assertIn("GROUP BY", out)

    def test_a_column_with_one_value_on_every_row_is_not_the_name(self):
        out = self.s.query(self.wb, 'SELECT "Dept", "Kam Name", "Value" FROM T ORDER BY "Value" DESC LIMIT 5')
        self.assertIn('"Kam Name" repeats', out)

    def test_a_grouped_ranking_and_a_plain_listing_get_no_note(self):
        grouped = self.s.query(self.wb, 'SELECT "Kam Name", SUM("Value") v FROM T GROUP BY 1 ORDER BY v DESC LIMIT 5')
        listing = self.s.query(self.wb, 'SELECT "Kam Name", "Value" FROM T')
        distinct = self.s.query(self.wb, "SELECT \"Kam Name\", \"Value\" FROM T WHERE \"Kam Name\" <> 'Jhantu' ORDER BY \"Value\" DESC")
        for out in (grouped, listing, distinct):
            self.assertNotIn("repeats", out)

    def test_the_date_range_of_an_unfiltered_query(self):
        note = self.s.date_span_note(self.wb, "SELECT * FROM T")
        self.assertIn('"Date" 2026-11-04 to 2026-11-10', note)
        self.assertEqual(self.s.date_span_note(self.wb, "SELECT 1"), "")

    def test_a_filtered_query_names_its_period_in_words(self):
        self.assertIn("These rows are November 2026", self.s.period_filtered(self.wb, "SELECT * FROM T", "2026-11")[1])
        self.assertIn("These rows are 4 November 2026", self.s.period_filtered(self.wb, "SELECT * FROM T", "2026-11-04")[1])


class ParserDepthTests(unittest.TestCase):
    def test_the_number_expression_leaves_room_on_a_fixed_parser_stack(self):
        """SQLite up to 3.45 has a parser stack of 100: the expression must compile with room for what wraps it (a view
        column, COALESCE(... = ?, 0) in a filter), or kb_sql silently falls back to copying every row."""
        import sqlite3
        from core.sheet_engine import sql as s
        c = sqlite3.connect(":memory:")
        c.execute("CREATE TABLE r(cells)")
        c.execute("SELECT " + "(" * 10 + s.num_expr("json_extract(cells, '$[0]')") + ")" * 10 + " FROM r")


class MonthFilterSchemaTests(unittest.TestCase):
    def test_a_schema_qualified_table_is_filtered_too(self):
        from core.sheet_engine import sql as s
        wb = s.from_tables([("T", "doc", ["N", "Date"], [[1, "04/11/2026"], [2, "05/10/2026"], [3, "09/11/2026"]])])
        for q in ('SELECT COUNT(*) FROM main."T"', "SELECT COUNT(*) FROM main.T", 'SELECT COUNT(*) FROM MAIN . "T" x'):
            stmt, _note = s.month_filtered(wb, q, "2026-11")
            self.assertIn("| 2 |", s.query(wb, stmt), q)


class MonthFilteredTests(unittest.TestCase):
    def test_a_temp_view_workbook_reads_its_view_through_the_schema(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.execute('CREATE TABLE "base" ("Kam Name" TEXT, "Date" TEXT)')
        conn.executemany('INSERT INTO "base" VALUES (?, ?)',
                         [("Rahim", "2026-11-05"), ("Karim", "2026-12-01"), ("Sadia", "2026-11-11")])
        conn.execute('CREATE TEMP VIEW "T" AS SELECT "Kam Name", "Date" FROM main."base"')
        tables = [{"name": "T", "sheet": "Sheet1", "file": "x", "cols": [("Kam Name", "TEXT"), ("Date", "TEXT")], "dates": ["Date"],
                   "rows": 3, "truncated": False, "formulas": {}, "preamble": [], "handle": "K1", "aliases": ["T"]}]
        wb = sql.Workbook(conn, tables, [], schema="temp")
        stmt, _note = sql.month_filtered(wb, 'SELECT COUNT(*) FROM "T"', "2026-11")
        self.assertIn('temp."T"', stmt)
        self.assertIn("| 2 |", sql.query(wb, stmt))       # the two November rows of the view, not the base table's 3 rows or 1

    def test_a_statement_that_does_not_parse_gives_no_guessed_cause(self):
        wb = sql.from_tables([("Sales", "sales.xlsx", ["Region"], [["Dhaka"]])])
        with self.assertRaises(sql.SqlError) as cm:
            sql.query(wb, 'SELECT "Region" FROM Sales sheet WHERE')
        self.assertIn("does not parse", str(cm.exception))
        self.assertNotIn("usual cause", str(cm.exception))


class Tool(unittest.TestCase):
    def test_the_tool_is_registered_and_rejects_other_files(self):
        from core.agent_tools import TOOL_IMPLS, AGENT_TOOLS
        self.assertIn("data_sql", TOOL_IMPLS)
        self.assertTrue(any(t["function"]["name"] == "data_sql" for t in AGENT_TOOLS))
        self.assertTrue(TOOL_IMPLS["data_sql"]({}).startswith("error:"))
        self.assertTrue(TOOL_IMPLS["data_sql"]({"file": "nope.xlsx"}).startswith("error:"))


if __name__ == "__main__":
    unittest.main()
