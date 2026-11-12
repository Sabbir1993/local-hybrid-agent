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


if __name__ == "__main__":
    unittest.main()
