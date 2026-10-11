import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import kb_structure, kb_tables
from core.sqlite_util import ThreadLocalDB

STAFF = [["Name", "Role", "Dept", "Salary"],
         ["Alice", "Manager", "Dhaka", "90,000"],
         ["Bob", "Engineer", "Dhaka", "60000"],
         ["Carol", "Manager", "Khulna", "85000"],
         ["Dan", "", "Khulna", "40000"]]
TITLES = {1: "Staff list", 2: "Vendors"}


class TableTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = ThreadLocalDB(Path(self._tmp.name) / "m.db")
        kb_tables._ready = False
        p = mock.patch("core.memory.store._db", return_value=self.db)
        p.start()
        self.addCleanup(p.stop)
        kb_tables.replace_source(1, kb_structure.rows_to_text(STAFF, "Staff"))
        kb_tables.replace_source(2, kb_structure.rows_to_text([["Vendor", "Role"], ["Acme", "Supplier"]], "V"))

    def tearDown(self):
        self.db.close() if hasattr(self.db, "close") else None
        try:
            self._tmp.cleanup()
        except OSError:
            pass                                    # Windows keeps the sqlite file open a moment

    def q(self, **spec):
        return kb_tables.run_query(spec, {1, 2}, TITLES)


class ParseTests(unittest.TestCase):
    def test_parse_reads_sheets_columns_and_row_numbers(self):
        text = kb_structure.rows_to_text(STAFF, "Staff") + "\n" + kb_structure.rows_to_text([["X", "Y"], ["1", "2"]], "Other")
        t = kb_tables.parse_tables(text)
        self.assertEqual([x["sheet"] for x in t], ["Staff", "Other"])
        self.assertEqual(t[0]["columns"], ["Name", "Role", "Dept", "Salary"])
        self.assertEqual(t[0]["rows"][0], (2, ["Alice", "Manager", "Dhaka", "90,000"]))

    def test_repeated_header_does_not_split_a_table(self):
        rows = [["A", "B"]] + [[f"v{i}", "x"] for i in range(40)]
        t = kb_tables.parse_tables(kb_structure.rows_to_text(rows, "S"))
        self.assertEqual(len(t), 1)
        self.assertEqual(len(t[0]["rows"]), 40)

    def test_headerless_sheet_gets_column_names(self):
        t = kb_tables.parse_tables(kb_structure.rows_to_text([[1, 2], [3, 4]], "S"))
        self.assertEqual(t[0]["columns"], ["Col1", "Col2"])

    def test_plain_text_has_no_tables(self):
        self.assertEqual(kb_tables.parse_tables("just a paragraph\n[Page 1]\ntext"), [])


class QueryTests(TableTestBase):
    def test_count_with_filter(self):
        r = self.q(filters=[{"column": "role", "op": "=", "value": "manager"}], count=True)
        self.assertEqual(r["total"], 2)

    def test_group_by_merges_across_tables_that_have_the_column(self):
        r = self.q(group_by="Role")
        self.assertEqual(r["groups"], {"Manager": 2, "Engineer": 1, "(empty)": 1, "Supplier": 1})

    def test_list_rows_with_select_and_row_numbers(self):
        r = self.q(source="staff", filters=[{"column": "Dept", "op": "=", "value": "Khulna"}], select=["Name"])
        out = kb_tables.render({}, r)
        self.assertIn("| R4 | Carol |", out)
        self.assertIn("| R5 | Dan |", out)

    def test_numeric_comparison_handles_commas(self):
        r = self.q(source="staff", filters=[{"column": "Salary", "op": ">", "value": 80000}], count=True)
        self.assertEqual(r["total"], 2)                           # 90,000 and 85000

    def test_in_empty_and_contains(self):
        self.assertEqual(self.q(source="staff", filters=[{"column": "Role", "op": "is_empty"}], count=True)["total"], 1)
        self.assertEqual(self.q(source="staff", filters=[{"column": "Dept", "op": "in", "value": ["dhaka", "khulna"]}],
                                count=True)["total"], 4)
        self.assertEqual(self.q(source="staff", filters=[{"column": "Name", "op": "contains", "value": "aro"}],
                                count=True)["total"], 1)

    def test_unknown_column_skips_tables_and_says_so(self):
        r = self.q(filters=[{"column": "Salary", "op": ">", "value": 1}], count=True)
        self.assertEqual(r["total"], 4)
        self.assertTrue(r["skipped"])                             # Vendors has no Salary column

    def test_only_allowed_sources_are_searched(self):
        r = kb_tables.run_query({"count": True, "filters": [{"column": "Role", "op": "not_empty"}]}, {2}, TITLES)
        self.assertEqual(r["total"], 1)

    def test_injection_attempts_are_just_values(self):
        r = self.q(source="staff", filters=[{"column": "Name", "op": "=", "value": "x' OR '1'='1"}], count=True)
        self.assertEqual(r["total"], 0)
        with self.assertRaises(ValueError):
            self.q(filters=[{"column": "Name", "op": "; DROP TABLE kb_rows"}])

    def test_wildcards_in_contains_are_literal(self):
        self.assertEqual(self.q(source="staff", filters=[{"column": "Name", "op": "contains", "value": "%"}],
                                count=True)["total"], 0)

    def test_limit_is_capped_and_reported(self):
        out = kb_tables.render({}, self.q(source="staff", filters=[{"column": "Name", "op": "not_empty"}], limit=2))
        self.assertIn("4 matching row(s); showing up to 2", out)
        self.assertIn("more rows match", out)

    def test_replace_and_delete_source(self):
        kb_tables.replace_source(1, kb_structure.rows_to_text([["Name", "Role"], ["Zed", "CEO"]], "Staff"))
        self.assertEqual(self.q(source="staff", filters=[{"column": "Name", "op": "not_empty"}], count=True)["total"], 1)
        kb_tables.delete_source(1)
        self.assertEqual(self.q(source="staff", count=True)["tables"], [])

    def test_describe_lists_columns(self):
        out = kb_tables.describe({1, 2}, TITLES)
        self.assertIn("Name | Role | Dept | Salary", out)


if __name__ == "__main__":
    unittest.main()
