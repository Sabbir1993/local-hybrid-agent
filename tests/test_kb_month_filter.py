"""A plan for one month must be filtered by that month: the model leaves the month out of the query."""
import asyncio
import unittest
from unittest import mock

from core import kb_structure, kb_tables, month_arg
from core.sqlite_util import ThreadLocalDB

ROWS = [["Kam Name", "Stage", "Proposal Value", "Planned Date", "Date"],
        ["Rahim", "Proposal", "150000", "2026-11-03", "2026-11-05"],
        ["Karim", "Negotiation", "220000", "2026-11-20", "2026-12-01"],
        ["Sadia", "Proposal", "90000", "2026-10-02", "2026-11-11"],
        ["Rina", "Proposal", "310000", "2026-12-05", "2026-12-07"]]


class MonthArgTests(unittest.TestCase):
    def test_the_month_of_a_request(self):
        self.assertEqual(month_arg.from_request("Share me BFSI nov 2026 activity plan"), "2026-11")
        self.assertEqual(month_arg.from_request("the November plan"), "2026-11")
        self.assertEqual(month_arg.from_request("what is the pipeline for february 2027?"), "2027-02")
        self.assertEqual(month_arg.from_request("November"), "2026-11")

    def test_a_month_word_that_is_english_is_not_a_month(self):
        self.assertEqual(month_arg.from_request("you may ask me anything"), "")
        self.assertEqual(month_arg.from_request("we march on"), "")
        self.assertEqual(month_arg.from_request("we may need a plan in March"), "")

    def test_two_months_are_left_to_the_model(self):
        self.assertEqual(month_arg.from_request("compare November and December"), "")
        self.assertEqual(month_arg.from_request("show me 2026 by month"), "")

    def test_the_forms_of_a_month(self):
        self.assertEqual(month_arg.parse("2026-11"), "2026-11")
        self.assertEqual(month_arg.parse("11/2026"), "2026-11")
        self.assertEqual(month_arg.parse("dec 2025"), "2025-12")
        self.assertEqual(month_arg.parse("not a month"), "")


class MonthFilterTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self._tmp = tempfile.TemporaryDirectory()
        self.db = ThreadLocalDB(Path(self._tmp.name) / "m.db")
        kb_tables._ready = False
        p = mock.patch("core.memory.store._db", return_value=self.db)
        p.start()
        self.addCleanup(p.stop)
        kb_tables.replace_source(1, kb_structure.rows_to_text(ROWS, "Sheet1"))
        self.titles = {1: "BFSI activity nov 2026.xlsx"}

    def tearDown(self):
        self.db.close() if hasattr(self.db, "close") else None
        try:
            self._tmp.cleanup()
        except OSError:
            pass

    def run_sql(self, args):
        from core.agent_tools import search_tools
        scope = (True, object(), False, self.titles)
        with mock.patch.object(search_tools, "_kb_scope", return_value=scope), \
                mock.patch("core.audit.audit_log"):
            return asyncio.run(search_tools.tool_kb_sql(args))

    def test_without_a_month_the_whole_sheet_is_read(self):
        out = self.run_sql({"sql": 'SELECT "Stage", COUNT(*) n FROM "BFSI_activity_nov_2026_Sheet1" GROUP BY 1'})
        self.assertIn("| Proposal | 3 |", out)
        self.assertNotIn("filtered to", out)

    def test_a_month_filters_the_rows_before_the_grouping(self):
        out = self.run_sql({"sql": 'SELECT "Stage", COUNT(*) n FROM "BFSI_activity_nov_2026_Sheet1" GROUP BY 1',
                            "month": "2026-11"})
        self.assertIn("| Proposal | 2 |", out)          # the 3rd row has its Date in November
        self.assertNotIn("| Proposal | 3 |", out)
        self.assertIn("filtered to 2026-11", out)

    def test_the_named_date_column_is_the_one_that_counts(self):
        out = self.run_sql({"sql": 'SELECT "Stage", COUNT(*) n FROM "BFSI_activity_nov_2026_Sheet1" GROUP BY 1',
                            "month": "2026-11", "date_column": "Planned Date"})
        self.assertIn("| Proposal | 1 |", out)
        self.assertIn("filtered to 2026-11 on Planned Date", out)

    def test_a_date_column_that_is_not_there_is_an_error(self):
        out = self.run_sql({"sql": "SELECT 1 FROM x", "month": "2026-11", "date_column": "Closing Date"})
        self.assertIn("no date column named", out)
        self.assertIn("Planned Date", out)              # it says which ones there are

    def test_a_month_that_is_not_a_month_is_reported(self):
        out = self.run_sql({"sql": 'SELECT COUNT(*) FROM "BFSI_activity_nov_2026_Sheet1"', "month": "sometime"})
        self.assertIn("is not a month", out)

    def run_month(self, sql_text, month="2026-11", **extra):
        return self.run_sql({"sql": sql_text, "month": month, **extra})

    def test_a_month_with_a_where_clause_returns_rows(self):
        out = self.run_month('SELECT "Stage", COUNT(*) n FROM "BFSI_activity_nov_2026_Sheet1" WHERE "Kam Name" <> \'Nobody\' GROUP BY 1')
        self.assertNotIn("syntax error", out)
        self.assertIn("| Proposal | 2 |", out)
        self.assertIn("| Negotiation | 1 |", out)

    def test_an_or_in_the_where_clause_is_still_restricted_by_the_month(self):
        out = self.run_month('SELECT COUNT(*) FROM "BFSI_activity_nov_2026_Sheet1" WHERE "Stage" = \'Proposal\' OR "Stage" = \'Negotiation\'')
        self.assertIn("| 3 |", out)
        self.assertNotIn("| 4 |", out)

    def test_where_group_having_order_and_limit_together(self):
        out = self.run_month('SELECT "Stage", COUNT(*) n FROM "BFSI_activity_nov_2026_Sheet1" WHERE "Proposal Value" > 0 '
                             'GROUP BY "Stage" HAVING COUNT(*) > 0 ORDER BY n DESC LIMIT 5')
        self.assertIn("| Proposal | 2 |", out)
        self.assertIn("| Negotiation | 1 |", out)

    def test_keywords_inside_a_string_literal_do_not_move_the_filter(self):
        out = self.run_month('SELECT "Stage", COUNT(*) n FROM "BFSI_activity_nov_2026_Sheet1" '
                             'WHERE "Kam Name" <> \' where x limit 1 \' GROUP BY 1')
        self.assertIn("| Proposal | 2 |", out)

    def test_the_statements_own_cte_and_a_recursive_cte(self):
        out = self.run_month('WITH x AS (SELECT "Stage" FROM "BFSI_activity_nov_2026_Sheet1" WHERE "Stage" <> \'z\') '
                             'SELECT "Stage", COUNT(*) n FROM x GROUP BY 1')
        self.assertIn("| Proposal | 2 |", out)
        out = self.run_month('WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 2) '
                             'SELECT COUNT(*) FROM "BFSI_activity_nov_2026_Sheet1", r')
        self.assertIn("| 6 |", out)                  # 3 November rows x 2

    def test_both_halves_of_a_union_are_filtered(self):
        out = self.run_month('SELECT "Stage" FROM "BFSI_activity_nov_2026_Sheet1" WHERE "Stage" = \'Proposal\' UNION ALL '
                             'SELECT "Stage" FROM "BFSI_activity_nov_2026_Sheet1" WHERE "Stage" = \'Negotiation\'')
        self.assertIn("(3 rows)", out)

    def test_a_subquery_and_a_self_join_read_the_filtered_rows(self):
        out = self.run_month('SELECT COUNT(*) FROM (SELECT "Stage" FROM "BFSI_activity_nov_2026_Sheet1" WHERE "Stage" <> \'z\')')
        self.assertIn("| 3 |", out)
        out = self.run_month('SELECT COUNT(*) FROM "BFSI_activity_nov_2026_Sheet1" a JOIN "BFSI_activity_nov_2026_Sheet1" b '
                             'ON a."Kam Name" = b."Kam Name"')
        self.assertIn("| 3 |", out)

    def test_a_table_without_a_date_column_joins_without_an_error(self):
        from core.agent_tools import search_tools
        kb_tables.replace_source(2, kb_structure.rows_to_text([["Kam Name", "Target"], ["Rahim", "500000"], ["Karim", "700000"]], "Sheet1"))
        titles = {1: "BFSI activity nov 2026.xlsx", 2: "Targets.xlsx"}
        other = search_tools._kb_table_name("Targets.xlsx", "Sheet1")
        scope = (True, object(), False, titles)
        with mock.patch.object(search_tools, "_kb_scope", return_value=scope), mock.patch("core.audit.audit_log"):
            out = asyncio.run(search_tools.tool_kb_sql({
                "sql": f'SELECT a."Kam Name", b."Target" FROM "BFSI_activity_nov_2026_Sheet1" a '
                       f'JOIN "{other}" b ON a."Kam Name" = b."Kam Name" WHERE a."Stage" = \'Proposal\'',
                "month": "2026-11"}))
        self.assertNotIn("no such column", out)
        self.assertIn("(1 row)", out)                # Rahim only: a November Proposal with a target

    def test_a_handle_and_a_loosely_spelled_name_are_filtered_too(self):
        self.assertIn("| 3 |", self.run_month("SELECT COUNT(*) FROM K1"))
        self.assertIn("| 3 |", self.run_month('SELECT COUNT(*) FROM "BFSI activity nov 2026.xlsx"'))


class MonthFilterFunctionTests(unittest.TestCase):
    ROWS = [["2026-11-05", "Dhaka", "100"], ["2026-12-01", "Dhaka", "50"], ["2026-11-20", "Khulna", "70"]]

    def test_the_data_sql_path_filters_by_the_month(self):
        from core.agent_tools import data_tools
        from core.sheet_engine import sql as sheet_sql
        wb = sheet_sql.from_tables([("sales", "sales.xlsx", ["Date", "Region", "Revenue"], self.ROWS)])
        stmt, note = data_tools._month_filter(wb, {"sql": 'SELECT "Region", SUM("Revenue") s FROM "sales" WHERE "Region" <> \'x\' '
                                                          'GROUP BY 1 ORDER BY 1', "month": "2026-11"})
        out = sheet_sql.query(wb, stmt)
        self.assertIn("| Dhaka | 100 |", out)
        self.assertIn("| Khulna | 70 |", out)
        self.assertNotIn("| Dhaka | 150 |", out)
        self.assertTrue(note.startswith("(filtered to 2026-11 on Date)"), note)

    def test_a_file_without_a_date_column_says_so(self):
        from core.sheet_engine import sql as sheet_sql
        wb = sheet_sql.from_tables([("sales", "sales.xlsx", ["Region"], [["Dhaka"]])])
        stmt, note = sheet_sql.month_filtered(wb, 'SELECT COUNT(*) FROM "sales"', "2026-11", noun="file")
        self.assertIn("this file has no date column", note)
        self.assertEqual(stmt, 'SELECT COUNT(*) FROM "sales"')

    def test_an_invalid_month_is_rejected_by_the_shared_function(self):
        from core.sheet_engine import sql as sheet_sql
        wb = sheet_sql.from_tables([("sales", "sales.xlsx", ["Date", "Region"], self.ROWS)])
        for bad in ["2026-11' OR 1=1 --", "2026-1", "11/2026", "2026-11\n"]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                sheet_sql.month_filtered(wb, 'SELECT 1 FROM "sales"', bad)


DAY_ROWS = [["Kam Name", "Stage", "Proposal Value", "Planned Date", "Date"],
            ["Rahim", "Proposal", "150000", "2026-11-03", "2026-11-04"],
            ["Karim", "Negotiation", "220000", "2026-11-20", "2026-11-04 10:30:00"],
            ["Sadia", "Proposal", "90000", "2026-11-04", "2026-11-05"],
            ["Rina", "Proposal", "310000", "2026-12-05", "2026-12-07"],
            ["Tania", "Proposal", "10000", "2026-11-02", "2026-11-02"]]


class DayArgTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(month_arg, "_this_year", return_value=2026)
        p.start()
        self.addCleanup(p.stop)

    def test_the_day_of_a_request(self):
        for text, want in [("I need the detailed report of 4th november of BFSI team", "2026-11-04"),
                           ("report for 04/11/2026", "2026-11-04"), ("4-11-2026 sales", "2026-11-04"),
                           ("activity on 2026-11-04", "2026-11-04"), ("Nov 4, 2026 plan", "2026-11-04"),
                           ("nov 20,2026", "2026-11-20"), ("4th of nov", "2026-11-04"), ("4 nov 2026", "2026-11-04"),
                           ("november 4", "2026-11-04"), ("nov 4th", "2026-11-04"), ("the 4 november report", "2026-11-04")]:
            with self.subTest(text=text):
                self.assertEqual(month_arg.day_from_request(text), want)

    def test_what_is_not_a_day_in_a_request(self):
        for text in ["BFSI nov 2026 activity plan", "the November plan", "4 nov to 10 nov", "from 4th november",
                     "top 5 november deals", "31 february 2026", "11/2026", "we may need 4 people", "4/11",
                     "compare November and December 4", "week of 4th november", "between 4 and 10 nov"]:
            with self.subTest(text=text):
                self.assertEqual(month_arg.day_from_request(text), "")

    def test_parse_day_reads_a_tool_argument(self):
        self.assertEqual(month_arg.parse_day("2026-11-04"), "2026-11-04")
        self.assertEqual(month_arg.parse_day("4 November 2026"), "2026-11-04")
        self.assertEqual(month_arg.parse_day("04/11/2026"), "2026-11-04")
        self.assertEqual(month_arg.parse_day("nov 20,2026"), "2026-11-20")
        self.assertEqual(month_arg.parse_day("4 november", default_year=2025), "2025-11-04")
        self.assertEqual(month_arg.parse_day("2026-11-04 10:30:00"), "2026-11-04")
        for bad in ["", "not a date", "November 2026", "11/2026", "31 february 2026", "2026-02-30", "4 nov to 10 nov"]:
            with self.subTest(bad=bad):
                self.assertEqual(month_arg.parse_day(bad), "")

    def test_month_readings_are_unchanged(self):
        self.assertEqual(month_arg.from_request("I need the detailed report of 4th november of BFSI team"), "2026-11")
        self.assertEqual(month_arg.from_request("the November plan"), "2026-11")


class DayGuardTests(unittest.TestCase):
    """A wrongly read day empties an answer without a word, so anything doubtful is not a day."""

    def test_doubtful_text_is_not_a_day(self):
        for q in ("you may 5 times retry", "in march 3 teams joined", "4th november to 6th", "ticket 12-11-2026-7",
                  "week of 4 november", "compare 4 nov and 5 nov"):
            self.assertEqual(month_arg.day_from_request(q), "", q)

    def test_a_clear_day_in_may_or_march_still_counts(self):
        with mock.patch.object(month_arg, "_this_year", return_value=2026):
            self.assertEqual(month_arg.day_from_request("may 4 report"), "2026-05-04")
            self.assertEqual(month_arg.day_from_request("what was done on 3rd march"), "2026-03-03")
            self.assertEqual(month_arg.day_from_request("sales on 4 May 2026"), "2026-05-04")


class ApplyRequestPeriodTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(month_arg, "_this_year", return_value=2026)
        p.start()
        self.addCleanup(p.stop)

    def test_a_day_in_the_request_is_injected(self):
        args = {"sql": "SELECT 1"}
        month_arg.apply_request_period(args, "the detailed report of 4th november")
        self.assertEqual(args["day"], "2026-11-04")
        self.assertNotIn("month", args)

    def test_a_model_day_is_not_overwritten(self):
        args = {"sql": "SELECT 1", "day": "2026-11-02"}
        month_arg.apply_request_period(args, "the detailed report of 4th november")
        self.assertEqual(args["day"], "2026-11-02")

    def test_an_empty_day_opts_out_of_the_request_day(self):
        args = {"sql": "SELECT 1", "day": ""}
        month_arg.apply_request_period(args, "the detailed report of 4th november")
        self.assertEqual(args, {"sql": "SELECT 1", "day": "", "month": "2026-11"})   # no day, but the month

    def test_an_empty_day_still_takes_the_month_when_none_is_given(self):
        args = {"sql": "SELECT 1", "day": ""}
        month_arg.apply_request_period(args, "the November plan")
        self.assertEqual(args, {"sql": "SELECT 1", "day": "", "month": "2026-11"})

    def test_no_day_in_the_request_falls_back_to_the_month(self):
        args = {"sql": "SELECT 1"}
        month_arg.apply_request_period(args, "the November plan")
        self.assertEqual(args, {"sql": "SELECT 1", "month": "2026-11"})

    def test_a_month_the_model_set_is_kept(self):
        args = {"sql": "SELECT 1", "month": "2026-10"}
        month_arg.apply_request_period(args, "the November plan")
        self.assertEqual(args["month"], "2026-10")

    def test_a_request_with_no_period_changes_nothing(self):
        args = {"sql": "SELECT 1"}
        month_arg.apply_request_period(args, "top 5 november deals")
        self.assertEqual(args, {"sql": "SELECT 1"})


class DayFilterTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self._tmp = tempfile.TemporaryDirectory()
        self.db = ThreadLocalDB(Path(self._tmp.name) / "d.db")
        kb_tables._ready = False
        p = mock.patch("core.memory.store._db", return_value=self.db)
        p.start()
        self.addCleanup(p.stop)
        kb_tables.replace_source(1, kb_structure.rows_to_text(DAY_ROWS, "Sheet1"))
        self.titles = {1: "BFSI activity nov 2026.xlsx"}

    def tearDown(self):
        self.db.close() if hasattr(self.db, "close") else None
        try:
            self._tmp.cleanup()
        except OSError:
            pass

    def run_sql(self, args):
        from core.agent_tools import search_tools
        scope = (True, object(), False, self.titles)
        with mock.patch.object(search_tools, "_kb_scope", return_value=scope), \
                mock.patch("core.audit.audit_log"):
            return asyncio.run(search_tools.tool_kb_sql(args))

    def test_a_day_keeps_only_that_days_rows(self):
        out = self.run_sql({"sql": 'SELECT "Stage", COUNT(*) n FROM "BFSI_activity_nov_2026_Sheet1" GROUP BY 1',
                            "day": "2026-11-04"})
        self.assertIn("| Proposal | 2 |", out)          # Rahim (Date) and Sadia (Planned Date)
        self.assertIn("| Negotiation | 1 |", out)         # Karim, a datetime cell
        self.assertIn("filtered to 2026-11-04 on Planned Date, Date", out)
        self.assertIn('call again with day="" and month=2026-11', out)

    def test_a_datetime_cell_matches_its_day(self):
        out = self.run_sql({"sql": 'SELECT "Kam Name" FROM "BFSI_activity_nov_2026_Sheet1" WHERE "Stage" = \'Negotiation\'',
                            "day": "2026-11-04"})
        self.assertIn("Karim", out)

    def test_the_whole_month_without_a_day(self):
        out = self.run_sql({"sql": 'SELECT "Stage", COUNT(*) n FROM "BFSI_activity_nov_2026_Sheet1" GROUP BY 1',
                            "month": "2026-11"})
        self.assertIn("| Proposal | 3 |", out)

    def test_a_day_with_the_models_own_where(self):
        out = self.run_sql({"sql": 'SELECT COUNT(*) FROM "BFSI_activity_nov_2026_Sheet1" WHERE "Stage" = \'Proposal\'',
                            "day": "2026-11-04"})
        self.assertIn("| 2 |", out)

    def test_a_day_with_group_by_and_a_date_column(self):
        out = self.run_sql({"sql": 'SELECT "Stage", COUNT(*) n FROM "BFSI_activity_nov_2026_Sheet1" GROUP BY 1',
                            "day": "2026-11-04", "date_column": "Planned Date"})
        self.assertIn("| Proposal | 1 |", out)
        self.assertNotIn("| Negotiation |", out)
        self.assertIn("filtered to 2026-11-04 on Planned Date", out)

    def test_a_day_wins_over_a_different_month(self):
        out = self.run_sql({"sql": 'SELECT COUNT(*) FROM "BFSI_activity_nov_2026_Sheet1"',
                            "day": "2026-11-04", "month": "2026-12"})
        self.assertIn("| 3 |", out)
        self.assertIn("filtered to 2026-11-04", out)

    def test_an_unreadable_day_leaves_the_rows_unfiltered(self):
        out = self.run_sql({"sql": 'SELECT COUNT(*) FROM "BFSI_activity_nov_2026_Sheet1"', "day": "the fourth"})
        self.assertIn("| 5 |", out)
        self.assertIn("warning: 'the fourth' is not a date, so no day filter was applied.", out)

    def test_a_day_in_the_order_by_query(self):
        out = self.run_sql({"sql": 'SELECT "Kam Name" FROM "BFSI_activity_nov_2026_Sheet1" ORDER BY 1', "day": "2026-11-04",
                            "date_column": "Date"})
        self.assertIn("Karim", out)
        self.assertIn("Rahim", out)
        self.assertNotIn("Sadia", out)                     # her Date is the 5th; only her Planned Date is the 4th


class DataSqlDayTests(unittest.TestCase):
    ROWS = [["2026-11-05", "Dhaka", "100"], ["2026-12-01", "Dhaka", "50"], ["2026-11-20", "Khulna", "70"]]

    def test_the_data_sql_path_filters_by_a_day(self):
        from core.agent_tools import data_tools
        from core.sheet_engine import sql as sheet_sql
        wb = sheet_sql.from_tables([("sales", "sales.xlsx", ["Date", "Region", "Revenue"], self.ROWS)])
        stmt, note = data_tools._month_filter(wb, {"sql": 'SELECT "Region", SUM("Revenue") s FROM "sales" GROUP BY 1 ORDER BY 1',
                                                   "day": "5 November 2026"})
        out = sheet_sql.query(wb, stmt)
        self.assertIn("| Dhaka | 100 |", out)
        self.assertNotIn("Khulna", out)
        self.assertIn("(filtered to 2026-11-05 on Date)", note)
        self.assertIn('call again with day="" and month=2026-11', note)

    def test_an_unreadable_day_on_data_sql(self):
        from core.agent_tools import data_tools
        from core.sheet_engine import sql as sheet_sql
        wb = sheet_sql.from_tables([("sales", "sales.xlsx", ["Date", "Region", "Revenue"], self.ROWS)])
        stmt, note = data_tools._month_filter(wb, {"sql": 'SELECT COUNT(*) FROM "sales"', "day": "soon"})
        self.assertEqual(stmt, 'SELECT COUNT(*) FROM "sales"')
        self.assertEqual(note, "warning: 'soon' is not a date, so no day filter was applied.")


class PeriodFilteredTests(unittest.TestCase):
    def test_a_period_must_be_a_strict_month_or_day(self):
        from core.sheet_engine import sql as sheet_sql
        wb = sheet_sql.from_tables([("sales", "sales.xlsx", ["Date", "Region"], [["2026-11-04", "Dhaka"]])])
        for bad in ["2026-11-4", "2026-13", "2026-11-04; DROP", "2026-11-32"]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                sheet_sql.period_filtered(wb, 'SELECT 1 FROM "sales"', bad)

    def test_the_day_statement_text(self):
        from core.sheet_engine import sql as sheet_sql
        wb = sheet_sql.from_tables([("sales", "sales.xlsx", ["Date", "Region"], [["2026-11-04", "Dhaka"]])])
        stmt, note = sheet_sql.period_filtered(wb, 'SELECT "Region" FROM "sales" ORDER BY 1', "2026-11-04")
        self.assertTrue(note.startswith("(filtered to 2026-11-04 on Date)"), note)
        self.assertIn("strftime('%Y-%m-%d', \"Date\") = '2026-11-04'", stmt)


if __name__ == "__main__":
    unittest.main()
