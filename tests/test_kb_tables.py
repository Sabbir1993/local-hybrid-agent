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


class KbSqlTests(TableTestBase):
    """kb_sql: totals, joins and medians over the same rows kb_query sees, with the same access and cloud rules."""

    def run_sql(self, args, scope=None):
        import asyncio
        from core.agent_tools import search_tools
        scope = scope or (True, object(), False, TITLES)
        with mock.patch.object(search_tools, "_kb_scope", return_value=scope), \
                mock.patch("core.audit.audit_log"):
            return asyncio.run(search_tools.tool_kb_sql(args))

    def test_describe_lists_the_sheets(self):
        out = self.run_sql({"describe": True})
        self.assertIn("Staff_list_Staff", out)
        self.assertIn('"Salary"', out)

    def test_the_table_name_is_the_title_without_the_file_extension(self):
        titles = {**TITLES, 3: "BFSI activity nov 2026.xlsx"}
        kb_tables.replace_source(3, kb_structure.rows_to_text([["Kam Name", "Proposal Value", "Client Name"],
                                                              ["Rahim", "100", "Acme"]], "Sheet1"))
        out = self.run_sql({"describe": True}, scope=(True, object(), False, titles))
        self.assertIn("BFSI_activity_nov_2026_Sheet1", out)     # what the model has to write in FROM
        self.assertNotIn("_xlsx", out)                           # the extension is not part of the name
        self.assertIn('sheet "Sheet1"', out)                     # the tab, not the table name
        q = self.run_sql({"sql": 'SELECT "Kam Name", COUNT(*) FROM BFSI_activity_nov_2026_Sheet1 GROUP BY 1'},
                         scope=(True, object(), False, titles))
        self.assertIn("| Rahim | 1 |", q)

    def test_a_wrong_table_name_says_which_tables_exist(self):
        out = self.run_sql({"sql": "SELECT * FROM payroll_totals_2026"})
        self.assertIn("no such table", out)
        self.assertIn("Staff_list_Staff", out)                  # the tables that do exist
        self.assertIn("describe=true", out)

    def test_a_table_name_the_model_guesses_still_works(self):
        """The file name, the document title and the identifier all name the same table: a model that writes the name it
        knows gets the rows, instead of 'no such table'."""
        titles = {**TITLES, 3: "BFSI activity nov 20,2026_Sheet1.xlsx"}
        kb_tables.replace_source(3, kb_structure.rows_to_text([["Kam Name", "Proposal Value"],
                                                              ["Rahim", "100"]], "Sheet1"))
        scope = (True, object(), False, titles)
        for name in ('BFSI_activity_nov_20_2026_Sheet1_Sheet1',      # the identifier
                     '"BFSI activity nov 20,2026_Sheet1"',           # the document title
                     '"BFSI_activity_nov_20,2026_Sheet1"',           # the file name, comma and all
                     'BFSI_activity_nov_20,2026_Sheet1'):            # the file name, unquoted: sqlite cannot read it
            out = self.run_sql({"sql": f'SELECT "Kam Name" FROM {name}'}, scope=scope)
            self.assertIn("| Rahim |", out, f"failed for {name}")

    def test_sum_avg_median_group_by(self):
        out = self.run_sql({"sql": 'SELECT Dept, COUNT(*) n, SUM(Salary) total, MEDIAN(Salary) med FROM Staff_list_Staff GROUP BY Dept ORDER BY Dept'})
        self.assertIn("| Dhaka | 2 | 150,000 | 75,000 |", out)         # "90,000" and 60000 are both numbers
        self.assertIn("| Khulna | 2 | 125,000 | 62,500 |", out)

    def test_join_between_two_documents(self):
        out = self.run_sql({"sql": 'SELECT s.Name, v.Vendor FROM Staff_list_Staff s JOIN Vendors_V v ON v.Role = s.Role'})
        self.assertIn("0 row", out)

    def test_only_select_and_no_internal_tables(self):
        self.assertTrue(self.run_sql({"sql": "DELETE FROM Staff_list_Staff"}).startswith("error:"))
        self.assertTrue(self.run_sql({"sql": "SELECT * FROM sqlite_master"}).startswith("error:"))

    def test_a_cloud_answer_cannot_use_it(self):
        out = self.run_sql({"sql": "SELECT 1"}, scope=(True, object(), True, TITLES))
        self.assertTrue(out.startswith("error:"))
        self.assertIn("cloud", out)

    def test_nothing_readable_returns_the_message(self):
        self.assertEqual(self.run_sql({"sql": "SELECT 1"}, scope=(False, None, None, "(no accessible company knowledge base sources)")),
                         "(no accessible company knowledge base sources)")

    def test_registered_with_the_other_kb_tools(self):
        from core.agent_tools import AGENT_TOOLS, TOOL_IMPLS
        self.assertIn("kb_sql", TOOL_IMPLS)
        self.assertTrue(any(t["function"]["name"] == "kb_sql" for t in AGENT_TOOLS))


class FuzzyColumnTests(unittest.TestCase):
    COLS = ["Employee ID", "Name", "Department", "Dept Code", "Home Phone", "Mobile Phone", "Salary (BDT)"]

    def test_exact_and_unique_partial_still_win(self):
        self.assertEqual(kb_tables.resolve_column("name", self.COLS), 1)
        self.assertEqual(kb_tables.resolve_column("salary", self.COLS), 6)

    def test_word_beginnings_resolve_to_the_one_column(self):
        self.assertEqual(kb_tables.resolve_column("emp id", self.COLS), 0)
        self.assertEqual(kb_tables.resolve_column("Emp. ID", self.COLS), 0)
        self.assertEqual(kb_tables.resolve_column("mob phone", self.COLS), 5)

    def test_whole_word_beats_a_longer_word(self):
        self.assertEqual(kb_tables.resolve_column("dept", self.COLS), 3)         # the word "Dept" in "Dept Code"
        self.assertEqual(kb_tables.resolve_column("dept", ["Department", "Name"]), 0)

    def test_ambiguous_is_refused(self):
        self.assertIsNone(kb_tables.resolve_column("phone", self.COLS))          # two phone columns
        self.assertIsNone(kb_tables.resolve_column("ph no", self.COLS))
        self.assertIsNone(kb_tables.resolve_column("zzz", self.COLS))
        self.assertIsNone(kb_tables.resolve_column("emp dept id x", self.COLS))

    def test_closest_columns(self):
        self.assertIn("Employee ID", kb_tables.closest_columns("emp id", self.COLS))
        self.assertIn("Salary (BDT)", kb_tables.closest_columns("salry", self.COLS))


class HelpfulFailureTests(TableTestBase):
    def test_all_tables_skipped_lists_columns_and_closest(self):
        kb_tables.replace_source(3, kb_structure.rows_to_text([["Employee ID", "Name"], ["E1", "Zed"]], "Big"))
        titles = {3: "Payroll register"}
        spec = {"filters": [{"column": "emp number", "op": "=", "value": "E1"}]}
        out = kb_tables.render(spec, kb_tables.run_query(spec, {3}, titles))
        self.assertIn("No column 'emp number' in \"Payroll register / Big\". Its columns: Employee ID | Name.", out)
        self.assertIn("Retry with", out)
        self.assertNotIn("(no spreadsheet", out)
        spec = {"filters": [{"column": "empid", "op": "=", "value": "E1"}]}
        out = kb_tables.render(spec, kb_tables.run_query(spec, {3}, titles))
        self.assertIn("Closest: 'Employee ID'", out)

    def test_column_list_is_capped(self):
        cols = [f"C{i}" for i in range(55)]
        kb_tables.replace_source(3, kb_structure.rows_to_text([cols, ["x"] * 55], "Wide"))
        spec = {"filters": [{"column": "nothing", "op": "=", "value": "1"}]}
        out = kb_tables.render(spec, kb_tables.run_query(spec, {3}, {3: "W"}))
        self.assertIn("... 15 more", out)
        self.assertNotIn("C54", out)

    def test_partly_skipped_keeps_results_and_adds_the_tail(self):
        spec = {"filters": [{"column": "Salary", "op": ">", "value": 1}], "count": True}
        out = kb_tables.render(spec, self.q(**spec))
        self.assertIn("4 matching row(s).", out)
        self.assertIn("Not searched (no such column):", out)
        self.assertIn('No column \'Salary\' in "Vendors / V". Its columns: Vendor | Role.', out)

    def test_source_not_found_lists_what_exists(self):
        spec = {"source": "nope", "count": True}
        out = kb_tables.render(spec, self.q(**spec))
        self.assertIn("No spreadsheet matches 'nope'.", out)
        self.assertIn("Staff list / Staff", out)
        self.assertIn("Vendors / V", out)

    def test_equals_hint_only_when_nothing_matched_and_contains_would(self):
        spec = {"filters": [{"column": "Dept", "op": "=", "value": "Dha"}], "source": "staff", "count": True}
        out = kb_tables.render(spec, self.q(**spec))
        self.assertIn("0 rows equal 'Dha', but 2 rows contain it", out)
        self.assertIn('"contains"', out)
        spec["filters"][0]["value"] = "Dhaka"                       # rows found: no hint, no extra query
        res = self.q(**spec)
        self.assertEqual(res["hint"], "")
        self.assertNotIn("contain it", kb_tables.render(spec, res))
        spec["filters"][0]["value"] = "Mars"                        # contains finds nothing either: no hint
        self.assertEqual(self.q(**spec)["hint"], "")

    def test_no_extra_query_when_rows_were_found(self):
        calls = []
        orig = kb_tables._equals_hint
        with mock.patch.object(kb_tables, "_equals_hint", side_effect=lambda *a: calls.append(1) or orig(*a)):
            self.q(source="staff", filters=[{"column": "Dept", "op": "=", "value": "Dhaka"}], count=True)
        self.assertEqual(calls, [])

    def test_fuzzy_name_now_finds_rows(self):
        r = self.q(source="staff", filters=[{"column": "dept", "op": "=", "value": "Khulna"}], count=True)
        self.assertEqual(r["total"], 2)


# ---------------------------------------------------------------------------------------------------------------------
# audit 3: the column that answers must be the column that was asked for
class ColumnMatchingTests(unittest.TestCase):
    R = staticmethod(kb_tables.resolve_column)

    def name(self, q, cols):
        i = self.R(q, cols)
        return None if i is None else cols[i]

    def test_a_part_of_a_word_is_never_a_match(self):
        self.assertIsNone(self.R("id", ["Name", "Paid Amount"]))                       # not "Paid"
        self.assertIsNone(self.R("dt", ["Amount BDT", "Name"]))
        self.assertIsNone(self.R("total", ["Subtotal", "Tax"]))
        self.assertEqual(self.name("date", ["Due Date", "Name"]), "Due Date")           # a whole word: fine, and echoed by the tool
        self.assertIsNone(self.R("am", ["Amount"]))
        self.assertIsNone(self.R("amt", ["Amount"]))                                   # a 3-letter short form is too loose
        self.assertIsNone(self.R("mount", ["Amount"]))

    def test_the_exact_name_always_wins(self):
        cols = ["ID", "Customer ID", "Cust Name", "Amount", "Amount USD", "Amount BDT", "Date", "Due Date"]
        for q in ("id", "ID", "Amount", "amount", "Date", "Amount USD", "amount (usd)", "AmountUSD", "date"):
            self.assertEqual(self.name(q, cols), next(c for c in cols if kb_tables._norm(c) == kb_tables._norm(q)))
        self.assertEqual(self.name("Amount", ["Amount", "Amount USD"]), "Amount")
        self.assertIsNone(self.R("name", ["Name", "name"]))                            # two columns of the same name: ambiguous

    def test_whole_words_resolve_when_exactly_one_column_has_them(self):
        cols = ["Customer ID", "Cust Name", "Amount USD", "Amount BDT", "Due Date", "Invoice Date"]
        self.assertEqual(self.name("usd", cols), "Amount USD")
        self.assertEqual(self.name("bdt", cols), "Amount BDT")
        self.assertEqual(self.name("id", cols), "Customer ID")
        self.assertEqual(self.name("due", cols), "Due Date")
        self.assertEqual(self.name("Cust ID", cols), "Customer ID")
        self.assertIsNone(self.R("amount", cols))                                      # two amounts
        self.assertIsNone(self.R("date", cols))                                        # two dates

    def test_a_longer_form_of_another_columns_word_is_not_answered(self):
        self.assertIsNone(self.R("customer", ["Customer ID", "Cust Name"]))            # could mean "Cust Name"
        self.assertEqual(self.name("customer", ["Customer Name", "Amount"]), "Customer Name")

    def test_ids_do_not_land_on_paid_or_total_on_subtotal(self):
        self.assertIsNone(self.R("id", ["Name", "Amount", "Paid Amount"]))
        self.assertEqual(self.name("total", ["Name", "Total Amount", "Subtotal"]), "Total Amount")
        self.assertEqual(self.name("paid", ["Name", "Paid Amount"]), "Paid Amount")
        self.assertIsNone(self.R("total", ["Subtotal", "Tax", "Grand Amount"]))

    def test_short_forms_need_four_letters_the_same_first_letter_and_one_candidate(self):
        self.assertEqual(self.name("dept", ["Department", "Name"]), "Department")
        self.assertIsNone(self.R("dept", ["Department", "Departure"]))                 # two candidates
        self.assertIsNone(self.R("tmnt", ["Department"]))                              # first letter differs
        self.assertIsNone(self.R("dpt", ["Department"]))                               # under 4 letters

    def test_word_starts_still_work(self):
        cols = ["Employee ID", "Name", "Department", "Dept Code", "Home Phone", "Mobile Phone", "Salary (BDT)"]
        self.assertEqual(self.name("emp id", cols), "Employee ID")
        self.assertEqual(self.name("mob phone", cols), "Mobile Phone")
        self.assertEqual(self.name("dept", cols), "Dept Code")


class ColumnEchoTests(TableTestBase):
    def test_the_matched_column_is_named_when_the_name_was_not_exact(self):
        kb_tables.replace_source(3, kb_structure.rows_to_text([["Customer ID", "Amount"], ["C1", "5"], ["C2", "6"]], "T"))
        spec = {"source": "pay", "filters": [{"column": "id", "op": "=", "value": "C1"}], "select": ["cust id", "Amount"]}
        out = kb_tables.render(spec, kb_tables.run_query(spec, {3}, {3: "Payments"}))
        self.assertIn("(matched 'id' -> 'Customer ID'; matched 'cust id' -> 'Customer ID')", out)
        spec = {"source": "pay", "filters": [{"column": "Customer ID", "op": "=", "value": "C1"}], "select": ["amount"]}
        out = kb_tables.render(spec, kb_tables.run_query(spec, {3}, {3: "Payments"}))
        self.assertNotIn("matched", out)                                              # exact up to case: nothing to say

    def test_a_wrong_column_is_an_error_with_the_listing_not_the_nearest_substring(self):
        kb_tables.replace_source(3, kb_structure.rows_to_text([["Name", "Paid Amount"], ["a", "1"]], "T"))
        spec = {"source": "pay", "filters": [{"column": "id", "op": "=", "value": "1"}], "count": True}
        out = kb_tables.render(spec, kb_tables.run_query(spec, {3}, {3: "Payments"}))
        self.assertIn("No column 'id'", out)
        self.assertIn("Name | Paid Amount", out)
        self.assertNotIn("matching row", out)


# numbers in filters: one grammar, a cell that is not a number is never 0
AMOUNTS = [["Name", "Amount", "Paid Amount"], ["a", "50", "1"], ["b", "1,200", "2"], ["c", "", "3"], ["d", "N/A", "4"],
           ["e", "$1,500", "5"], ["f", "(300)", "6"], ["g", "Tk 90", "7"], ["h", "৳2,000", "8"]]


class NumberFilterTests(TableTestBase):
    def setUp(self):
        super().setUp()
        kb_tables.replace_source(3, kb_structure.rows_to_text(AMOUNTS, "S"))

    def names(self, op, value):
        r = kb_tables.run_query({"source": "book", "filters": [{"column": "Amount", "op": op, "value": value}], "select": ["Name"]},
                                {3}, {3: "Book"})
        return sorted(row[1][0] for t in r["tables"] for row in t["rows"])

    def test_less_than(self):
        self.assertEqual(self.names("<", 100), ["a", "f", "g"])                         # 50, (300) = -300, Tk 90; not "", N/A
        self.assertEqual(self.names("<=", 50), ["a", "f"])

    def test_greater_than(self):
        self.assertEqual(self.names(">", 1000), ["b", "e", "h"])                       # 1,200  $1,500  ৳2,000
        self.assertEqual(self.names(">=", 1500), ["e", "h"])
        self.assertEqual(self.names(">", "1,000"), ["b", "e", "h"])                    # the filter value may be written like a cell

    def test_blank_and_text_cells_are_never_in_a_range(self):
        for op, v in (("<", 1), ("<", 1e9), (">", -1e9), (">=", 0), ("<=", 0)):
            self.assertNotIn("c", self.names(op, v))
            self.assertNotIn("d", self.names(op, v))

    def test_equals_a_number_finds_the_formatted_cell(self):
        self.assertEqual(self.names("=", 1200), ["b"])
        self.assertEqual(self.names("=", "1200"), ["b"])
        self.assertEqual(self.names("=", 1500), ["e"])
        self.assertEqual(self.names("=", -300), ["f"])
        self.assertEqual(self.names("=", 90), ["g"])
        self.assertEqual(self.names("=", 0), [])                                       # nothing is 0: blanks are not zeros

    def test_not_equals_a_number(self):
        got = self.names("!=", 1200)
        self.assertNotIn("b", got)
        self.assertIn("c", got)                                                         # a blank is not 1,200
        self.assertIn("d", got)                                                         # neither is "N/A"
        self.assertEqual(len(got), 7)

    def test_a_filter_value_that_is_not_a_number_is_refused_for_ranges(self):
        with self.assertRaises(ValueError):
            self.names(">", "lots")
        with self.assertRaises(ValueError):
            self.names("<", "12-34")

    def test_text_equality_is_unchanged(self):
        r = kb_tables.run_query({"source": "book", "filters": [{"column": "Amount", "op": "=", "value": "n/a"}], "count": True},
                                {3}, {3: "Book"})
        self.assertEqual(r["total"], 1)

    def test_an_identifier_with_leading_zeros_is_not_the_number(self):
        kb_tables.replace_source(4, kb_structure.rows_to_text([["Code", "N"], ["007", "a"], ["7", "b"]], "C"))
        r = kb_tables.run_query({"source": "codes", "filters": [{"column": "Code", "op": "=", "value": "007"}], "select": ["N"]},
                                {4}, {4: "Codes"})
        self.assertEqual([row[1][0] for row in r["tables"][0]["rows"]], ["a"])


# card numbers in kb_query: not in the output, not through a filter, not through a group-by
class CardNumberQueryTests(TableTestBase):
    def setUp(self):
        super().setUp()
        kb_tables.replace_source(3, kb_structure.rows_to_text(
            [["Merchant", "Card No", "Ref"], ["Acme", "4111 1111 1111 1111", "r1"], ["Bob", "5500005555555559", "r2"],
             ["Cy", "4111111111111112", "r3"]], "Txn"))

    def ask(self, **spec):
        spec.setdefault("source", "txn")
        return kb_tables.render(spec, kb_tables.run_query(spec, {3}, {3: "Cards"}))

    def test_the_rows_come_back_masked(self):
        out = self.ask(select=["Merchant", "Card No"])
        self.assertIn("[card ****1111]", out)
        self.assertIn("[card ****5559]", out)
        self.assertIn("[card ****1112]", out)                                           # fails the checksum, but the column is a card column
        for digits in ("4111 1111", "5500005555", "41111111"):
            self.assertNotIn(digits, out)

    def test_a_filter_on_the_digits_finds_nothing(self):
        for op, v in (("starts_with", "4111"), ("starts_with", "5"), ("contains", "1111 1111"), ("contains", "5500"),
                      ("=", "5500005555555559"), ("in", ["5500005555555559"]), ("starts_with", "41111")):
            out = self.ask(filters=[{"column": "Card No", "op": op, "value": v}], count=True)
            self.assertIn("0 matching row(s)", out, (op, v))

    def test_the_masked_text_is_what_a_filter_sees(self):
        out = self.ask(filters=[{"column": "Card No", "op": "contains", "value": "****1111"}], count=True)
        self.assertIn("1 matching row(s)", out)                                         # the last four are public anyway

    def test_numeric_operators_cannot_probe_a_card_number(self):
        for op in ("<", ">", ">=", "<="):
            with self.assertRaises(ValueError):                                         # 13+ digits are an identifier, not an amount
                self.ask(filters=[{"column": "Card No", "op": op, "value": 4111111111111110}], count=True)
        out = self.ask(filters=[{"column": "Card No", "op": "=", "value": 4111111111111111}], count=True)
        self.assertIn("0 matching row(s)", out)
        for op in ("<", ">", ">=", "<="):                                               # and a short number matches no card cell
            out = self.ask(filters=[{"column": "Card No", "op": op, "value": 5}], count=True)
            self.assertIn("0 matching row(s)", out, op)

    def test_group_by_a_card_column_shows_masked_values(self):
        out = self.ask(group_by="Card No")
        self.assertNotIn("4111 1111", out)
        self.assertNotIn("5500005555", out)
        self.assertIn("[card ****1111]", out)

    def test_ordinary_filters_are_unchanged(self):
        out = self.ask(filters=[{"column": "Merchant", "op": "starts_with", "value": "ac"}], select=["Merchant"])
        self.assertIn("| R2 | Acme |", out)


# a cloud model gets cleared sources only, and every value the answer shows is screened, not only the rows
class CloudQueryTests(TableTestBase):
    NOTES = [["Name", "Salary", "Note"], ["a", "50", "password: hunter2"], ["b", "60", "ok"], ["c", "70", "ok"]]

    def setUp(self):
        super().setUp()
        kb_tables.replace_source(3, kb_structure.rows_to_text(self.NOTES, "S"))

    def run_cloud(self, spec, cleared=frozenset({3}), rules=None):
        import asyncio
        from core import knowledge_access as ka, knowledge_rules
        from core.agent_tools import search_tools
        rules = rules if rules is not None else [{"kind": "keywords", "pattern": "hunter2", "name": "password"}]
        scope = (True, object(), True, {3: "Book"})
        with mock.patch.object(search_tools, "_kb_scope", return_value=scope), mock.patch("core.audit.audit_log"), \
                mock.patch.object(ka, "kb_local_only", return_value=True), \
                mock.patch("core.auth_db.cloud_ok_source_ids", return_value=set(cleared)), \
                mock.patch.object(knowledge_rules, "_rules", return_value=rules):
            return asyncio.run(search_tools.tool_kb_query(spec))

    def test_a_row_the_rules_hold_back_is_not_listed(self):
        out = self.run_cloud({"source": "Book", "select": ["Name", "Note"]})
        self.assertNotIn("hunter2", out)
        self.assertIn("| b | ok |", out)
        self.assertIn("not shown", out)

    def test_a_group_by_does_not_print_the_cell_values_of_a_held_back_row(self):
        out = self.run_cloud({"source": "Book", "group_by": "Note"})
        self.assertNotIn("hunter2", out)
        self.assertNotIn("password", out)
        self.assertIn("| ok | 2 |", out)                                                # the clear group stays, with its count
        self.assertIn("not shown", out)

    def test_rows_are_checked_against_their_own_source_not_source_zero(self):
        out = self.run_cloud({"source": "Book", "select": ["Name", "Note"]}, rules=[])
        self.assertIn("| b | ok |", out)                                                # with no rule, cleared rows are shown
        self.assertIn("| c | ok |", out)

    def test_a_source_that_is_not_cleared_shows_nothing(self):
        out = self.run_cloud({"source": "Book", "select": ["Name"]}, cleared=frozenset())
        self.assertNotIn("| b |", out)

    def test_the_hint_is_screened_too(self):
        from core.agent_tools import search_tools
        res = {"tables": [{"title": "Book", "sheet": "S", "source_id": 3, "count": 0, "columns": [], "rows": []}],
               "groups": None, "total": 0, "hint": "0 rows equal 'hunter2', but 1 rows contain it: use op \"contains\".",
               "group_sources": {}}
        from core import knowledge_access as ka, knowledge_rules
        with mock.patch.object(ka, "kb_local_only", return_value=True), mock.patch("core.auth_db.cloud_ok_source_ids", return_value={3}), \
                mock.patch.object(knowledge_rules, "_rules", return_value=[{"kind": "keywords", "pattern": "hunter2", "name": "pw"}]):
            search_tools._kb_query_cloud_screen(res)
        self.assertEqual(res["hint"], "")

    def test_group_keys_are_checked_against_every_source_they_came_from(self):
        from core.agent_tools import search_tools
        res = {"tables": [], "groups": {"ok": 2, "bad": 1}, "total": 3, "hint": "", "group_sources": {"ok": {3}, "bad": {3, 9}}}
        from core import knowledge_access as ka, knowledge_rules
        with mock.patch.object(ka, "kb_local_only", return_value=True), mock.patch("core.auth_db.cloud_ok_source_ids", return_value={3}), \
                mock.patch.object(knowledge_rules, "_rules", return_value=[]):
            search_tools._kb_query_cloud_screen(res)
        self.assertEqual(res["groups"], {"ok": 2})                                      # source 9 was not cleared: "bad" is dropped
