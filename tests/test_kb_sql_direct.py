"""kb_sql queries the stored rows in place (read-only second connection + temp views): same answers as the old copy path,
and nothing but the views of the readable tables can be reached."""
import asyncio
import re
import sqlite3
import time
import unittest
from unittest import mock

from core import kb_structure, kb_tables
from core.agent_tools import search_tools
from core.sheet_engine import sql as sheet_sql
from tests.test_kb_tables import TableTestBase, TITLES

ORDERS = [["Order ID", "Staff", "Amount", "Date", "Share"],
          ["O1", "Alice", "1,200", "2026-01-15", "12%"],
          ["O2", "Bob", "800", "2026-01-20", "8%"],
          ["O3", "Alice", "2,000.50", "2026-03-02", ""],
          ["O4", "Zed", "n/a", "2026-03-09", "5%"]]


class DirectBase(TableTestBase):
    def setUp(self):
        super().setUp()
        kb_tables.replace_source(3, kb_structure.rows_to_text(ORDERS, "Orders"))
        self.titles = {**TITLES, 3: "Orders book"}

    def run_sql(self, args, titles=None, copy=False):
        scope = (True, object(), False, titles or self.titles)
        patches = [mock.patch.object(search_tools, "_kb_scope", return_value=scope), mock.patch("core.audit.audit_log")]
        if copy:
            patches.append(mock.patch.object(sheet_sql, "open_readonly", side_effect=OSError("forced copy path")))
        for p in patches:
            p.start()
        try:
            return asyncio.run(search_tools.tool_kb_sql(args))
        finally:
            for p in patches:
                p.stop()


class EquivalenceTests(DirectBase):
    def both(self, args):
        new, old = self.run_sql(args), self.run_sql(args, copy=True)
        return new, old

    def same(self, sql, **kw):
        new, old = self.both({"sql": sql, **kw})
        self.assertEqual(new, old, sql)
        self.assertFalse(new.startswith("error:"), new)
        return new

    def test_select_star_is_the_same_on_both_paths(self):
        """No extra row-number column any more (it made NATURAL JOIN join on it): SELECT * is exactly the copy path's."""
        new, old = self.both({"sql": "SELECT * FROM Staff_list_Staff"})
        self.assertEqual(new, old)
        self.assertIn("| Alice | Manager | Dhaka | 90,000 |", new)

    def test_aggregates_group_by_limit_and_dates(self):
        self.same("SELECT SUM(Salary), AVG(Salary), MEDIAN(Salary) FROM Staff_list_Staff")
        self.same("SELECT Dept, COUNT(*) n FROM Staff_list_Staff GROUP BY Dept ORDER BY Dept")
        self.same("SELECT Name, Salary FROM Staff_list_Staff ORDER BY Name", limit=2)
        self.same("SELECT strftime('%Y-%m', Date) m, SUM(Amount) FROM Orders_book_Orders GROUP BY m ORDER BY m")
        self.same("SELECT SUM(Amount), COUNT(Amount), SUM(Share) FROM Orders_book_Orders")

    def test_numbers_with_separators_are_real_and_text_is_null(self):
        out = self.run_sql({"sql": "SELECT SUM(Salary) s, COUNT(Salary) c FROM Staff_list_Staff"})
        self.assertIn("| 275,000 | 4 |", out)                       # 90,000 counted as a number
        out = self.run_sql({"sql": "SELECT COUNT(Amount) FROM Orders_book_Orders"})
        self.assertIn("| 3 |", out)                                 # "n/a" is NULL, not 0

    def test_join_across_two_sources(self):
        self.same("SELECT s.Name, SUM(o.Amount) FROM Staff_list_Staff s JOIN Orders_book_Orders o ON o.Staff = s.Name "
                  "GROUP BY s.Name ORDER BY s.Name")

    def test_describe(self):
        new, old = self.both({"describe": True})
        self.assertEqual(new, old)

    def test_the_view_path_is_really_used(self):
        with mock.patch.object(search_tools, "_kb_copy_workbook", side_effect=AssertionError("copied")):
            self.assertIn("| 4 |", self.run_sql({"sql": "SELECT COUNT(*) FROM Staff_list_Staff"}))

    def test_fallback_when_the_file_cannot_be_opened(self):
        err = mock.patch("sys.stderr")
        with err:
            out = self.run_sql({"sql": "SELECT COUNT(*) FROM Staff_list_Staff"}, copy=True)
        self.assertIn("| 4 |", out)


class SecurityTests(DirectBase):
    def refused(self, sql, secret="Acme"):
        out = self.run_sql({"sql": sql})
        self.assertTrue(out.startswith("error:"), f"{sql!r} -> {out}")
        self.assertNotIn(secret, out)
        self.assertNotIn("Alice", out)
        return out

    def test_direct_reads_of_the_real_tables_fail(self):
        for sql in ["SELECT * FROM kb_rows", "SELECT * FROM main.kb_rows", "SELECT * FROM kb_tables",
                    "SELECT * FROM sqlite_master", "SELECT * FROM sqlite_temp_master", "SELECT * FROM temp.sqlite_temp_master",
                    "SELECT name FROM pragma_table_info('kb_rows')", "SELECT * FROM pragma_table_list",
                    "WITH a AS (SELECT * FROM main.kb_rows) SELECT * FROM a",
                    "SELECT * FROM Staff_list_Staff WHERE Name IN (SELECT cells FROM kb_rows)",
                    "SELECT (SELECT cells FROM kb_rows LIMIT 1)", "SELECT * FROM chunks", "SELECT * FROM chunks_fts"]:
            self.refused(sql)

    def test_everything_but_select_is_refused(self):
        for sql in ["ATTACH DATABASE ':memory:' AS x", "ATTACH DATABASE '/etc/passwd' AS x", "DETACH DATABASE x",
                    "PRAGMA table_info(kb_rows)", "PRAGMA query_only = OFF", "CREATE TEMP VIEW v AS SELECT 1",
                    "CREATE TABLE t(x)", "DROP VIEW Staff_list_Staff", "ALTER TABLE kb_rows ADD x",
                    "INSERT INTO kb_rows VALUES (1,1,'x')", "UPDATE kb_rows SET cells='x'", "DELETE FROM kb_rows",
                    "INSERT INTO Staff_list_Staff VALUES (1)", "DELETE FROM Staff_list_Staff", "BEGIN", "SAVEPOINT a",
                    "VACUUM", "SELECT load_extension('x')", "SELECT readfile('/etc/passwd')", "SELECT randomblob(1000000000)",
                    "SELECT 1; SELECT * FROM kb_rows", "SELECT 1; DELETE FROM kb_rows", "SELECT zeroblob(2000000000)"]:
            self.refused(sql)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM kb_rows").fetchone()[0], 4 + 1 + 4)   # nothing was written

    def test_a_source_the_user_cannot_read_is_unreachable(self):
        titles = {1: "Staff list"}                                  # Vendors (2) and Orders (3) are not allowed
        out = self.run_sql({"sql": "SELECT * FROM Vendors_V"}, titles=titles)
        self.assertTrue(out.startswith("error:"))
        self.assertNotIn("Acme", out)
        for sql in ["SELECT * FROM kb_rows WHERE table_id = 2", "SELECT * FROM main.kb_rows", "SELECT * FROM Orders_book_Orders"]:
            out = self.run_sql({"sql": sql}, titles=titles)
            self.assertTrue(out.startswith("error:"), out)
            self.assertNotIn("Acme", out)
            self.assertNotIn("O1", out)
        self.assertNotIn("Vendors", self.run_sql({"describe": True}, titles=titles))

    def test_hostile_titles_and_headers_cannot_break_the_views(self):
        evil = 'x"; DROP TABLE kb_rows; -- select'
        kb_tables.replace_source(4, kb_structure.rows_to_text([['a"b', "Select", 'a"b'], ["1", "2", "3"]], "Sheet\"1'"))
        out = self.run_sql({"describe": True}, titles={4: evil})
        self.assertIn("a'b", out)
        out = self.run_sql({"sql": 'SELECT "a\'b", "Select" FROM x_DROP_TABLE_kb_rows_select_Sheet_1'}, titles={4: evil})
        self.assertIn("| 1 | 2 |", out)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM kb_rows").fetchone()[0], 4 + 1 + 4 + 1)

    def test_titles_that_look_like_internal_tables_do_not_shadow_them(self):
        kb_tables.replace_source(4, kb_structure.rows_to_text([["A"], ["1"]], "S"))
        for t in ("kb_rows", "sqlite_master", "chunks"):
            out = self.run_sql({"describe": True}, titles={4: t})
            self.assertNotIn(f"- {t} ", out)
            self.assertIn(f"{t}_S" if t != "sqlite_master" else "t_sqlite_master_S", out)
        out = self.run_sql({"sql": "SELECT * FROM kb_rows"}, titles={1: "Staff list", 4: "kb_rows"})
        self.assertNotIn("Alice", out)

    def test_authorizer_sees_the_view_as_the_reason_for_a_base_read(self):
        """The rule 'a base table may be read only on behalf of one of our views' relies on SQLite passing the view's name as
        the 5th authorizer argument; record what this SQLite does."""
        conn = sheet_sql.open_readonly(self.db.path)
        try:
            sheet_sql.from_kb_views(conn, kb_tables.list_tables({1}, TITLES))
            seen = []
            conn.set_authorizer(lambda a, x, y, db, src: seen.append((a, x, db, src)) or sqlite3.SQLITE_OK)
            conn.execute("SELECT Name FROM Staff_list_Staff").fetchall()
            conn.set_authorizer(None)
        finally:
            conn.close()
        base = [r for r in seen if r[0] == sqlite3.SQLITE_READ and r[1] == "kb_rows"]
        self.assertTrue(base)
        self.assertTrue(all(r[2] == "main" and str(r[3]).startswith("__kb_") for r in base), base)   # the hidden inner view, base in main
        top = [r for r in seen if r[0] == sqlite3.SQLITE_READ and r[3] is None]
        self.assertEqual({(r[1], r[2]) for r in top}, {("Staff_list_Staff", "temp")})               # the view itself: temp, no source

    def test_the_apps_own_connection_is_untouched(self):
        self.run_sql({"sql": "SELECT COUNT(*) FROM Staff_list_Staff"})
        self.db.execute("INSERT INTO kb_rows VALUES (1, 99, '[\"Late\",\"\",\"\",\"1\"]')")      # still writable
        self.db.commit()
        self.assertIn("| 5 |", self.run_sql({"sql": "SELECT COUNT(*) FROM Staff_list_Staff"}))


class HintTests(DirectBase):
    def test_wrong_column_gets_the_real_names(self):
        out = self.run_sql({"sql": "SELECT Salry FROM Staff_list_Staff"})
        self.assertTrue(out.startswith("error:"))
        self.assertIn("no such column", out)
        self.assertIn('Columns of Staff_list_Staff: "Name", "Role", "Dept", "Salary"', out)
        self.assertNotIn("Alice", out)
        self.assertNotIn("Vendors_V", out)                          # only the table the statement named

    def test_wrong_table_lists_all_tables(self):
        out = self.run_sql({"sql": "SELECT * FROM zzz_unknown_table"})
        self.assertIn("no such table", out)
        for name in ("Staff_list_Staff", "Vendors_V", "Orders_book_Orders"):
            self.assertIn(name, out)

    def test_unquoted_name_with_a_space(self):
        kb_tables.replace_source(4, kb_structure.rows_to_text([["Employee ID", "Name"], ["E1", "Zed"]], "Big"))
        out = self.run_sql({"sql": "SELECT Employee ID FROM Big_x"}, titles={4: "Big"})
        self.assertTrue(out.startswith("error:"))
        self.assertIn("Big_Big", out)                                # the real table name is offered

    def test_other_errors_get_no_reminder(self):
        out = self.run_sql({"sql": "DELETE FROM kb_rows"})
        self.assertNotIn("double quotes", out)


class LargeDataTests(DirectBase):
    def fill(self, n, sid=9):
        cur = self.db.execute("INSERT INTO kb_tables (source_id, sheet, columns, nrows) VALUES (?, 'Big', ?, ?)",
                              (sid, '["Id", "Amount"]', n))
        tid = cur.lastrowid
        self.db.executemany("INSERT INTO kb_rows VALUES (?, ?, ?)", ((tid, i, f'["E{i:06d}", "{i:,}"]') for i in range(n)))
        self.db.commit()
        return {sid: "Large"}

    def test_lookup_and_describe_are_fast_and_do_not_copy(self):
        titles = self.fill(60000)
        with mock.patch.object(search_tools, "_kb_copy_workbook", side_effect=AssertionError("copied")):
            t0 = time.time()
            out = self.run_sql({"sql": "SELECT * FROM Large_Big WHERE Id = 'E043210'"}, titles=titles)
            self.assertLess(time.time() - t0, 1.5)
            self.assertIn("| E043210 |", out)
            t0 = time.time()
            out = self.run_sql({"describe": True}, titles=titles)
            self.assertLess(time.time() - t0, 1.5)
            self.assertIn("60,000 rows", out)

    def test_more_than_200000_rows_are_all_queryable(self):
        titles = self.fill(210000)
        out = self.run_sql({"sql": "SELECT COUNT(*), MAX(Amount) FROM Large_Big"}, titles=titles)
        self.assertIn("| 210000 | 209,999 |", out)


class ConcurrencyTests(DirectBase):
    def test_works_while_the_main_connection_has_an_open_write(self):
        self.db.execute("INSERT INTO kb_rows VALUES (1, 77, '[\"Pending\",\"\",\"\",\"5\"]')")      # not committed
        try:
            out = self.run_sql({"sql": "SELECT COUNT(*) FROM Staff_list_Staff"})
            self.assertIn("| 4 |", out)                           # the reader sees the last commit only
        finally:
            self.db.rollback()

    def test_works_while_another_thread_writes(self):
        import threading
        stop = threading.Event()

        def writer():
            while not stop.is_set():
                self.db.execute("INSERT INTO kb_rows VALUES (3, 500, '[\"x\",\"x\",\"1\",\"x\",\"x\"]')")
                self.db.commit()
                self.db.execute("DELETE FROM kb_rows WHERE rownum = 500")
                self.db.commit()
        th = threading.Thread(target=writer)
        th.start()
        try:
            for _ in range(5):
                self.assertNotIn("locked", self.run_sql({"sql": "SELECT COUNT(*) FROM Staff_list_Staff"}))
        finally:
            stop.set()
            th.join()


if __name__ == "__main__":
    unittest.main()


class DayFirstDateTests(DirectBase):
    """d/m/yyyy text becomes ISO in the view, like the copy path, so month filters and strftime work on it."""
    def test_day_first_dates_match_the_copy_path(self):
        kb_tables.replace_source(5, kb_structure.rows_to_text(
            [["Date", "Amount"], ["05/11/2026", "10"], ["7-11-26", "20"], ["01/12/2026", "30"], ["2026-11-09", "40"]], "Plan"))
        t = {5: "Plan"}
        out = self.run_sql({"sql": "SELECT Date FROM Plan_Plan ORDER BY Amount"}, titles=t)
        self.assertIn("2026-11-05", out)
        self.assertIn("2026-11-07", out)
        self.assertEqual(out.count("2026-"), 4)
        self.assertIn("70", self.run_sql({"sql": "SELECT SUM(Amount) FROM Plan_Plan", "month": "2026-11"}, titles=t))


class CteTests(DirectBase):
    """A CTE can carry the name of an allowed view: it must neither open the stored rows nor break ordinary CTE use."""
    def test_cte_named_like_a_view_cannot_read_the_stored_rows(self):
        for sql in ["WITH Staff_list_Staff AS (SELECT table_id, cells FROM kb_rows) SELECT * FROM Staff_list_Staff",
                    'WITH "Staff_list_Staff" AS (SELECT cells FROM kb_rows WHERE table_id = 2) SELECT cells FROM "Staff_list_Staff"',
                    "WITH Staff_list_Staff AS (SELECT * FROM kb_tables) SELECT * FROM Staff_list_Staff",
                    "WITH Staff_list_Staff(x) AS (SELECT group_concat(text) FROM chunks) SELECT x FROM Staff_list_Staff"]:
            out = self.run_sql({"sql": sql})
            self.assertTrue(out.startswith("error:"), f"{sql!r} -> {out}")
            self.assertNotIn("Acme", out)
            self.assertNotIn("table_id", out)

    def test_ordinary_ctes_over_a_view_work(self):
        out = self.run_sql({"sql": "WITH x AS (SELECT Name, Salary FROM Staff_list_Staff) SELECT COUNT(*) AS n FROM x"})
        self.assertFalse(out.startswith("error:"), out)
        self.assertIn("4", out)
        out = self.run_sql({"sql": "WITH a AS (SELECT Name FROM Staff_list_Staff), b AS (SELECT Name FROM a) SELECT * FROM b"})
        self.assertFalse(out.startswith("error:"), out)
        self.assertIn("Alice", out)

    def test_the_hidden_inner_views_are_not_readable_by_name(self):
        wb_names = self.run_sql({"describe": True})
        self.assertNotIn("__kb_", wb_names)
        out = self.run_sql({"sql": "SELECT name FROM temp.sqlite_master"})
        self.assertTrue(out.startswith("error:"), out)


# ---------------------------------------------------------------------------------------------------------------------
# card numbers never reach the model's SQL (audit 3, item 1)
CARDS = [["Merchant", "Card No", "Amt"],
         ["Acme", "4111 1111 1111 1111", "10"],
         ["Bob", "5500005555555559", "20"],
         ["Cy", "378282246310005", "5"],
         ["Dee", "4111-1111-1111-1111", "7"]]
PAN_PIECES = ("4111", "5500", "0000", "5555", "3782", "8224", "6310")


class CardNumberTests(DirectBase):
    ATTACKS = [
        'SELECT Merchant, "Card No" FROM Book_Txn',
        'SELECT * FROM Book_Txn',
        'SELECT substr("Card No",1,8) a, substr("Card No",9) b FROM Book_Txn',
        'SELECT substr("Card No",1,4)||\'x\'||substr("Card No",5,4)||\'x\'||substr("Card No",9,4)||\'x\'||substr("Card No",13) FROM Book_Txn',
        'SELECT replace("Card No",\' \',\'\') FROM Book_Txn',
        'SELECT "Card No"+0, CAST("Card No" AS INTEGER), CAST("Card No" AS REAL) FROM Book_Txn',
        'SELECT group_concat(substr("Card No",1,4),\'/\') g1, group_concat(substr("Card No",5,4),\'/\') g2 FROM Book_Txn',
        'SELECT "Card No" AS "x" FROM Book_Txn ORDER BY "Card No"',
        'SELECT printf(\'%s\', "Card No"), lower("Card No"), upper("Card No"), trim("Card No", \'[card*]\') FROM Book_Txn',
        'SELECT json_array("Card No"), json_object(\'c\', "Card No") FROM Book_Txn',
        'WITH c AS (SELECT "Card No" AS n FROM Book_Txn) SELECT substr(n, 1, 6), substr(n, 7, 6) FROM c',
        'SELECT MIN("Card No"), MAX("Card No"), MODE("Card No") FROM Book_Txn',
    ]

    def check(self, copy):
        for q in self.ATTACKS:
            out = self.run_sql({"sql": q}, titles={4: "Book"}, copy=copy)
            for piece in PAN_PIECES:
                self.assertNotIn(piece, out, f"{q!r} leaked {piece}: {out}")
            self.assertIsNone(re.search(r"\d{5}", out.replace("(4 rows)", "")), f"{q!r} -> {out}")

    def setUp(self):
        super().setUp()
        kb_tables.replace_source(4, kb_structure.rows_to_text(CARDS, "Txn"))

    def test_no_query_can_rebuild_a_card_number_in_place(self):
        self.check(copy=False)

    def test_no_query_can_rebuild_a_card_number_on_the_copy_path(self):
        self.check(copy=True)

    def test_the_masked_value_keeps_the_last_four_like_everywhere_else(self):
        for copy in (False, True):
            out = self.run_sql({"sql": 'SELECT "Card No" FROM Book_Txn ORDER BY Merchant'}, titles={4: "Book"}, copy=copy)
            self.assertEqual(out.count("[card ****1111]"), 2, out)
            self.assertIn("[card ****5559]", out)
            self.assertIn("[card ****0005]", out)

    def test_the_functions_that_read_a_value_character_by_character_are_refused(self):
        for q in ['SELECT hex("Card No") FROM Book_Txn', 'SELECT unicode(substr("Card No",1,1)) FROM Book_Txn',
                  "SELECT char(52)", "SELECT quote(\"Card No\") FROM Book_Txn", "SELECT HEX(Amt) FROM Book_Txn",
                  "SELECT zeroblob(10)", "SELECT randomblob(10)"]:
            for copy in (False, True):
                out = self.run_sql({"sql": q}, titles={4: "Book"}, copy=copy)
                self.assertTrue(out.startswith("error:"), f"{q!r} -> {out}")

    def test_a_filter_on_the_digits_finds_nothing(self):
        for q in ['SELECT * FROM Book_Txn WHERE "Card No" LIKE \'4111%\'',
                  "SELECT Merchant FROM Book_Txn WHERE substr(\"Card No\",1,1)='4' AND substr(\"Card No\",2,1)='1'",
                  'SELECT Merchant FROM Book_Txn WHERE "Card No" GLOB \'5500*\'',
                  'SELECT Merchant FROM Book_Txn WHERE instr("Card No", \'1111\') = 1',
                  'SELECT Merchant FROM Book_Txn WHERE "Card No" = \'5500005555555559\'']:
            for copy in (False, True):
                out = self.run_sql({"sql": q}, titles={4: "Book"}, copy=copy)
                self.assertIn("(0 rows)", out, f"{q!r} -> {out}")

    def test_a_table_read_that_needs_no_column_does_not_count_the_stored_rows(self):
        for q in ("SELECT COUNT(*) FROM kb_rows", "SELECT COUNT(*) FROM kb_tables", "SELECT 1 FROM kb_rows LIMIT 1",
                  "SELECT COUNT(*) FROM Staff_list_Staff a, kb_rows b", "SELECT (SELECT COUNT(*) FROM chunks)"):
            out = self.run_sql({"sql": q})
            self.assertTrue(out.startswith("error:"), f"{q} -> {out}")

    def test_a_recursive_cte_still_works_and_cannot_reach_the_stored_rows(self):
        out = self.run_sql({"sql": "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM r WHERE n < 5) SELECT SUM(n) FROM r"})
        self.assertIn("| 15 |", out)
        out = self.run_sql({"sql": "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM r WHERE n < (SELECT COUNT(*) FROM kb_rows)) "
                                   "SELECT COUNT(*) FROM r"})
        self.assertTrue(out.startswith("error:"), out)

    def test_the_function_can_only_hide(self):
        out = self.run_sql({"sql": "SELECT kb_mask('4111 1111 1111 1111'), kb_mask('plain text, nothing to hide')"}, titles={4: "Book"})
        self.assertIn("[card ****1111]", out)
        self.assertIn("plain text, nothing to hide", out)
        self.assertNotIn("4111", out)

    def test_a_card_number_stored_as_a_spreadsheet_number_is_masked_too(self):
        wb = sheet_sql.from_tables([("T", "T", ["Card", "Amt"], [[4111111111111111, 5], [4012888888881881, 6]])])
        for q in ("SELECT Card FROM T", "SELECT CAST(Card AS TEXT), Card + 0, Card * 1 FROM T", "SELECT substr(Card, 1, 6) FROM T"):
            out = sheet_sql.query(wb, q)
            self.assertNotIn("4111", out)
            self.assertNotIn("401288", out)
            self.assertIsNone(re.search(r"\d{5}", out.replace("(2 rows)", "")), out)

    def test_a_mistyped_card_number_in_a_card_column_is_masked_but_an_id_elsewhere_is_not(self):
        kb_tables.replace_source(6, kb_structure.rows_to_text([["Card No", "Who"], ["4111111111111112", "x"]], "Odd"))     # fails the checksum
        kb_tables.replace_source(5, kb_structure.rows_to_text([["Account No", "Who"], ["1234567890123456", "x"]], "Acc"))
        out = self.run_sql({"sql": "SELECT * FROM Book_Odd"}, titles={6: "Book"})
        self.assertNotIn("4111111111111112", out)
        self.assertIn("[card ****1112]", out)
        out = self.run_sql({"sql": "SELECT * FROM Book_Acc WHERE \"Account No\" = '1234567890123456'"}, titles={5: "Book"})
        self.assertIn("(1 row)", out)                               # an account number is not a card number: it can be looked up

    def test_ordinary_long_text_and_short_cells_are_untouched(self):
        kb_tables.replace_source(6, kb_structure.rows_to_text(
            [["Note", "Qty"], ["delivered on 2026-01-15 at the office", "12"]], "N"))
        out = self.run_sql({"sql": "SELECT Note, Qty FROM Book_N"}, titles={6: "Book"})
        self.assertIn("delivered on 2026-01-15 at the office", out)

    def test_the_mask_is_only_applied_to_long_cells_with_a_digit(self):
        self.assertIn("kb_mask(", sheet_sql.masked_text("x"))
        self.assertIn("length(x) >= 13", sheet_sql.masked_text("x"))


# ---------------------------------------------------------------------------------------------------------------------
# one number grammar for both paths (items 3 and 4)
CELLS = ["50", "1,200", "", "N/A", "$1,500", "(300)", "Tk 90", "৳2,000", "Tk 500", "500 BDT", "USD 20", "12%", ".5", "5.", "+5", "-5",
         "12,34,567", "1,200.50", "12-34", "5-", "--5", "1.5.2", "2026-01-15", "1,5", "1 000", "1e5", "007", "0042", "0", "0.5",
         "4111111111111111", "1234567890123", "(5)%", "(-5)", "-(5)", "Tk", "$", "tk. 7", "TAKA 3", "8/-", "٣٠٠", "∞"]


class NumberGrammarTests(DirectBase):
    def test_in_place_and_copy_path_agree_on_every_cell(self):
        from core.sheet_engine.sql import parse_cell_number
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t (x)")
        conn.executemany("INSERT INTO t VALUES (?)", [(c,) for c in CELLS])
        for x, got in conn.execute(f"SELECT x, {sheet_sql.num_expr('x')} FROM t"):
            want = parse_cell_number(x)
            self.assertEqual(got is None, want is None, f"{x!r}: sql {got} python {want}")
            if want is not None:
                self.assertAlmostEqual(got, want, msg=repr(x))

    def test_the_audit_cells(self):
        want = {"50": 50, "1,200": 1200, "": None, "N/A": None, "$1,500": 1500, "(300)": -300, "Tk 90": 90, "৳2,000": 2000,
                "Tk 500": 500, "500 BDT": 500, "USD 20": 20, ".5": 0.5, "5.": 5, "12-34": None, "5-": None, "--5": None,
                "1.5.2": None, "2026-01-15": None, "007": None, "4111111111111111": None, "12,34,567": 1234567, "1,5": None,
                "12%": 12, "(5)%": -5, "(-5)": None, "8/-": 8, "tk. 7": 7}
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t (x)")
        conn.executemany("INSERT INTO t VALUES (?)", [(c,) for c in want])
        for x, got in conn.execute(f"SELECT x, {sheet_sql.num_expr('x')} FROM t"):
            if want[x] is None:
                self.assertIsNone(got, repr(x))
            else:
                self.assertAlmostEqual(got, want[x], msg=repr(x))

    def test_python_and_sql_agree_on_generated_cells(self):
        import itertools
        import random
        random.seed(11)
        nums = ["5", "05", "1200", "1,200", "1,2,3", "12,34,567", "1,234,567.8", "0.5", ".5", "5.", "12345678901", "1234567890123",
                "1.2.3", "1,2", "-", "", "+"]
        pre = ["", "$", "৳", "Tk ", "tk.", "BDT ", "usd ", "-", "+", "(", "- ", "~", "x"]
        post = ["", "%", " BDT", "Tk", "/-", ")", " USD", "x", "%%", " %", "~"]
        cells = ["".join(p) for p in itertools.product(pre, nums, post)]
        cells += ["".join(random.choice("0123456789,.+-()% $tkBDTUSDx৳") for _ in range(random.randint(1, 8))) for _ in range(3000)]
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t (x)")
        conn.executemany("INSERT INTO t VALUES (?)", [(c,) for c in cells])
        from core.sheet_engine.sql import parse_cell_number
        bad = []
        for x, got in conn.execute(f"SELECT x, {sheet_sql.num_expr('x')} FROM t"):
            want = parse_cell_number(x)
            if (got is None) != (want is None) or (want is not None and abs(got - want) > 1e-9):
                bad.append((x, got, want))
        self.assertEqual(bad[:10], [])

    def test_a_numeric_column_reads_the_same_on_both_paths(self):
        rows = [["Name", "V"]] + [["n", v] for v in ["100", "1,200", "(300)", "Tk 500", "500 BDT", "USD 20", ".5", "N/A", "", "12-34",
                                                      "5-", "--5", "1.5.2", "2026-01-15", "$1,500", "৳2,000"]] + [["n", "1"]] * 30
        kb_tables.replace_source(7, kb_structure.rows_to_text(rows, "S"))
        t = {7: "Book"}
        q = {"sql": "SELECT V, typeof(V) FROM Book_S", "limit": 200}
        a, b = self.run_sql(q, titles=t), self.run_sql(q, titles=t, copy=True)
        self.assertEqual(a, b)
        self.assertIn("| -300 | real |", a)
        self.assertIn("| 500 | real |", a)
        self.assertIn("| 0.50 | real |", a)
        self.assertGreaterEqual(a.count("| null |"), 7)             # blanks and text are NULL, not 0
        self.assertNotIn("| 0 | real |", a)
        for q2 in ("SELECT SUM(V), COUNT(V), MIN(V), MAX(V) FROM Book_S",):
            self.assertEqual(self.run_sql({"sql": q2}, titles=t), self.run_sql({"sql": q2}, titles=t, copy=True))

    def test_identifiers_stay_text_in_a_column_of_numbers(self):
        rows = [["Name", "Ref"]] + [["n", str(i)] for i in range(1, 60)] + [["n", "007"], ["n", "4111111111111112"], ["n", "123456789012345"]]
        kb_tables.replace_source(7, kb_structure.rows_to_text(rows, "S"))
        t = {7: "Book"}
        for copy in (False, True):
            self.assertIn('"Ref" TEXT', self.run_sql({"describe": True}, titles=t, copy=copy))
            out = self.run_sql({"sql": "SELECT Ref FROM Book_S WHERE Ref LIKE '0%' OR Ref LIKE '%456789012345'"}, titles=t, copy=copy)
            self.assertIn("| 007 |", out)                       # the leading zeros are kept
            self.assertIn("123456789012345", out)               # 15 digits are not rounded through a float

    def test_a_column_that_is_numeric_only_after_the_first_rows_is_still_numbers(self):
        rows = [["Name", "Amt"]] + [["n", ""] for _ in range(1200)] + [["n", "1,000"] for _ in range(5)]
        kb_tables.replace_source(7, kb_structure.rows_to_text(rows, "S"))
        t = {7: "Book"}
        q = {"sql": "SELECT SUM(Amt), COUNT(Amt), typeof(MAX(Amt)) FROM Book_S"}
        a = self.run_sql(q, titles=t)
        self.assertIn("| 5,000 | 5 | real |", a)
        self.assertIn("| 5,000 | 5 |", self.run_sql({"sql": "SELECT SUM(Amt), COUNT(Amt) FROM Book_S"}, titles=t, copy=True))

    def test_numbers_that_start_late_in_a_long_table_are_found_by_the_spread_sample(self):
        rows = [["Name", "Amt"]] + [["n", "text"] for _ in range(2500)] + [["n", str(i)] for i in range(1, 3001)]
        kb_tables.replace_source(7, kb_structure.rows_to_text(rows, "S"))
        out = self.run_sql({"sql": "SELECT SUM(Amt) FROM Book_S"}, titles={7: "Book"})
        self.assertIn("4,501,500", out)


# ---------------------------------------------------------------------------------------------------------------------
# NATURAL JOIN, joins that silently lose a table, dates, the race, the cap, the worker thread (items 6-8)
JOIN_A = [["Cust", "Name"], ["c1", "Acme"], ["c2", "Bob"], ["c3", "Cy"]]
JOIN_B = [["Cust", "Amt"], ["c3", "10"], ["c1", "20"], ["c2", "30"]]


class JoinTests(DirectBase):
    def setUp(self):
        super().setUp()
        kb_tables.replace_source(8, kb_structure.rows_to_text(JOIN_A, "A"))
        kb_tables.replace_source(9, kb_structure.rows_to_text(JOIN_B, "B"))
        self.t = {8: "Alpha", 9: "Beta"}

    def test_natural_join_gives_the_same_rows_on_both_paths(self):
        q = {"sql": "SELECT * FROM Alpha_A NATURAL JOIN Beta_B ORDER BY Cust"}
        a, b = self.run_sql(q, titles=self.t), self.run_sql(q, titles=self.t, copy=True)
        self.assertEqual(a, b)
        self.assertIn("(3 rows)", a)
        self.assertEqual(a.splitlines()[0], "| Cust | Name | Amt |")

    def test_no_extra_column_in_select_star_or_describe(self):
        for q in ({"sql": "SELECT * FROM Alpha_A"}, {"describe": True}):
            self.assertEqual(self.run_sql(q, titles=self.t), self.run_sql(q, titles=self.t, copy=True))
        self.assertNotIn("Row", self.run_sql({"describe": True}, titles=self.t))

    def test_a_table_typo_in_a_join_is_an_error_not_a_silent_swap(self):
        for copy in (False, True):
            out = self.run_sql({"sql": "SELECT COUNT(*) FROM Alpha_A a, Beta_Typo b"}, titles=self.t, copy=copy)
            self.assertTrue(out.startswith("error:"), out)
            self.assertIn("Beta_Typo", out)
            self.assertIn("Beta_B", out)                         # the candidate is listed
            self.assertNotIn("| 3 |", out)
            out = self.run_sql({"sql": "SELECT * FROM Alpha_A a JOIN Beta_Typo b ON a.Cust = b.Cust"}, titles=self.t, copy=copy)
            self.assertTrue(out.startswith("error:"), out)

    def test_two_names_are_never_collapsed_into_one_table(self):
        kb_tables.replace_source(10, kb_structure.rows_to_text([["X"], ["1"]], "Pub"))
        t = {**self.t, 10: "Public_book"}
        for q in ("SELECT COUNT(*) FROM Public_book_Pub a, kb_rows b", "SELECT COUNT(*) FROM Alpha_A a, Nope b"):
            out = self.run_sql({"sql": q}, titles=t)
            self.assertTrue(out.startswith("error:"), f"{q} -> {out}")
        out = self.run_sql({"sql": "SELECT COUNT(*) FROM Alpha_A a, Beta_B b WHERE a.Cust = b.Cust"}, titles=t)
        self.assertIn("| 3 |", out)                                 # a real comma join still works

    def test_handles_and_exact_names_still_work(self):
        h = {t["source_id"]: f"K{t['id']}" for t in kb_tables.list_tables({8, 9}, self.t)}
        out = self.run_sql({"sql": f"SELECT COUNT(*) FROM {h[8]} a, {h[9]} b WHERE a.Cust = b.Cust"}, titles=self.t)
        self.assertIn("| 3 |", out)
        out = self.run_sql({"sql": 'SELECT COUNT(*) FROM "Alpha_A" a JOIN Beta_B b USING (Cust)'}, titles=self.t)
        self.assertIn("| 3 |", out)


class DateAndLimitTests(DirectBase):
    def test_a_day_the_month_does_not_have_stays_text(self):
        kb_tables.replace_source(5, kb_structure.rows_to_text(
            [["Date", "Amount"], ["31/02/2026", "1"], ["29/02/2026", "2"], ["29/02/2024", "3"], ["31/04/2026", "4"], ["30/04/2026", "5"],
             ["29/02/2100", "6"], ["29/02/2000", "7"], ["05/11/2026", "8"]], "Plan"))
        for copy in (False, True):
            out = self.run_sql({"sql": "SELECT Date FROM Plan_Plan ORDER BY Amount"}, titles={5: "Plan"}, copy=copy)
            rows = [ln.strip("| ") for ln in out.splitlines()[2:-1]]
            self.assertEqual(rows, ["31/02/2026", "29/02/2026", "2024-02-29", "31/04/2026", "2026-04-30", "29/02/2100",
                                    "2000-02-29", "2026-11-05"], (copy, out))

    def test_describe_says_dates_are_day_first(self):
        kb_tables.replace_source(5, kb_structure.rows_to_text([["Date", "Amount"], ["05/11/2026", "10"], ["07/11/2026", "20"]], "Plan"))
        for copy in (False, True):
            out = self.run_sql({"describe": True}, titles={5: "Plan"}, copy=copy)
            self.assertIn('date columns: "Date"', out)
            self.assertIn("day-first", out)

    def test_a_table_replaced_while_the_views_were_built_is_refused(self):
        tables = kb_tables.list_tables({1}, TITLES)
        tables[0]["nrows"] += 1                                       # what list_tables said no longer matches the stored table
        conn = sheet_sql.open_readonly(self.db.path)
        try:
            with self.assertRaises(RuntimeError):
                sheet_sql.from_kb_views(conn, tables)
        finally:
            conn.close()

    def test_the_tool_copies_the_current_rows_when_that_happens(self):
        real = sheet_sql.from_kb_views

        def stale(conn, metas):
            metas = [{**m, "nrows": m["nrows"] + 1} for m in metas]
            return real(conn, metas)
        with mock.patch.object(sheet_sql, "from_kb_views", stale), mock.patch("sys.stderr"):
            out = self.run_sql({"sql": "SELECT COUNT(*) FROM Staff_list_Staff"})
        self.assertIn("| 4 |", out)

    def test_more_tables_than_the_cap_are_mentioned_in_a_no_such_table_error(self):
        titles = dict(self.titles)
        for i in range(10, 72):
            kb_tables.replace_source(i, kb_structure.rows_to_text([["A"], ["1"]], "S"))
            titles[i] = f"Doc {i:02d}"
        out = self.run_sql({"sql": "SELECT * FROM zzz_unknown_table"}, titles=titles)
        self.assertIn("no such table", out)
        self.assertIn("more table(s) exist but are not loaded", out)
        self.assertIn("source=", out)


class WorkerThreadTests(DirectBase):
    def test_the_sql_runs_off_the_event_loop_with_the_callers_context(self):
        import contextvars
        import threading
        var = contextvars.ContextVar("kb_test_user", default=None)
        seen = {}
        real = search_tools._kb_sql_blocking

        def spy(args, titles):
            seen["thread"] = threading.current_thread()
            seen["ctx"] = var.get()
            return real(args, titles)

        async def main():
            var.set("user-7")
            return await search_tools.tool_kb_sql({"sql": "SELECT COUNT(*) FROM Staff_list_Staff"})

        scope = (True, object(), False, self.titles)
        with mock.patch.object(search_tools, "_kb_scope", return_value=scope), mock.patch("core.audit.audit_log"), \
                mock.patch.object(search_tools, "_kb_sql_blocking", spy):
            out = asyncio.run(main())
        self.assertIn("| 4 |", out)
        self.assertIsNot(seen["thread"], threading.main_thread())
        self.assertEqual(seen["ctx"], "user-7")

    def test_a_slow_query_does_not_stop_the_event_loop(self):
        import time as _t
        ticks = []

        def slow(args, titles):
            _t.sleep(0.4)
            return None, "| x |\n|---|\n| 1 |\n(1 row)", "", 1

        async def main():
            async def ticker():
                for _ in range(8):
                    ticks.append(1)
                    await asyncio.sleep(0.04)
            t = asyncio.create_task(ticker())
            out = await search_tools.tool_kb_sql({"sql": "SELECT 1"})
            await t
            return out

        scope = (True, object(), False, self.titles)
        with mock.patch.object(search_tools, "_kb_scope", return_value=scope), mock.patch("core.audit.audit_log"), \
                mock.patch.object(search_tools, "_kb_sql_blocking", slow):
            out = asyncio.run(main())
        self.assertIn("| 1 |", out)
        self.assertGreaterEqual(len(ticks), 8)

    def test_the_eight_second_cap_is_still_there(self):
        self.assertEqual(sheet_sql.QUERY_TIMEOUT_S, 8.0)
        with mock.patch.object(sheet_sql, "QUERY_TIMEOUT_S", 0.3):
            out = self.run_sql({"sql": "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM r) SELECT COUNT(*) FROM r"})
        self.assertIn("longer than", out)


SPLIT_ROWS = [["Merchant", "P1", "P2", "P3", "P4", "Qty", "Amt"],
              ["two", "4111 1111", "1111 1111", "", "", "2", "10"],
              ["three", "5500 0055", "5555", "5559", "", "3", "20"],
              ["four", "3782", "8224", "6310", "005", "4", "30"],            # Amex 4-4-4-3
              ["ten-six", "4111111111", "111111", "", "", "5", "40"],
              ["plain", "1200", "3400", "5600", "7800", "6", "50"],           # adjacent numbers, not a card: must stay readable
              ["luhn", "4012", "8888", "8888", "1882", "7", "60"]]            # right prefix, wrong checksum: stays


class SplitCardNumberTests(DirectBase):
    """A card number split over neighbouring cells cannot be rebuilt with `a || b`, substr or a join, on either kb_sql path."""
    ATTACKS = [
        'SELECT Merchant, P1 || P2 || P3 || P4 AS n FROM Book_Split',
        'SELECT Merchant, P1 || P2 AS n FROM Book_Split',
        'SELECT Merchant, replace(P1 || P2 || P3 || P4, \' \', \'\') AS n FROM Book_Split',
        'SELECT Merchant, substr(P1, 1, 4) || substr(P2, 1, 4) AS n FROM Book_Split',
        'SELECT P1, P2, P3, P4 FROM Book_Split',
        'SELECT * FROM Book_Split',
        'SELECT group_concat(P1 || P2, \'/\') FROM Book_Split',
        'SELECT P1 || P2 FROM Book_Split WHERE P1 LIKE \'4111%\'',
    ]
    # the last four digits of a card are public (a masked cell shows them), so only the other pieces count as a leak. The final
    # text of a query is masked once more, and a row next to "[card ...]" text counts as card context there: the two rows that are not
    # cards are therefore only read on their own (below)
    SECRET = ("4111", "5500", "0055", "5555", "3782", "8224", "6310", "1111 1111", "5600", "1200")

    def setUp(self):
        super().setUp()
        kb_tables.replace_source(5, kb_structure.rows_to_text(SPLIT_ROWS, "Split"))
        self.titles5 = {5: "Book"}

    def go(self, q, copy):
        return self.run_sql({"sql": q}, titles=self.titles5, copy=copy)

    def check(self, copy):
        for q in self.ATTACKS:
            out = self.go(q, copy)
            for piece in self.SECRET:
                if piece not in ("5600", "1200"):
                    self.assertNotIn(piece, out, f"{q!r} leaked {piece}: {out}")

    def test_no_query_can_rebuild_a_split_card_number_in_place(self):
        self.check(copy=False)

    def test_no_query_can_rebuild_a_split_card_number_on_the_copy_path(self):
        self.check(copy=True)

    def test_both_paths_give_the_same_answer(self):
        for q in ('SELECT * FROM Book_Split ORDER BY Merchant', 'SELECT Merchant, P1 || P2 AS n FROM Book_Split ORDER BY Merchant'):
            self.assertEqual(self.go(q, False), self.go(q, True), q)

    def test_the_masked_pieces_keep_the_last_four(self):
        for copy in (False, True):
            out = self.go('SELECT Merchant, P1, P2, P3, P4 FROM Book_Split ORDER BY Merchant', copy)
            self.assertIn("| two | [card part] | [card ****1111] |", out)
            self.assertIn("| three | [card part] | [card part] | [card ****5559] |", out)
            self.assertIn("| four | [card part] | [card part] | [card part] | [card ****0005] |", out)

    def test_numbers_that_are_not_a_card_are_still_queryable(self):
        for copy in (False, True):
            out = self.go("SELECT P1 || P2 || P3 || P4 AS n FROM Book_Split WHERE Merchant = 'plain'", copy)
            self.assertIn("1200340056007800", out)
            out = self.go("SELECT P1, P4, Qty, Amt FROM Book_Split WHERE Merchant = 'luhn'", copy)
            self.assertIn("| 4012 | 1882 |", out)
            out = self.go("SELECT SUM(Qty), SUM(Amt) FROM Book_Split", copy)
            self.assertIn("| 27 | 210 |", out)

    def test_kb_query_masks_the_pieces_too(self):
        spec = {"source": "split", "select": ["Merchant", "P1", "P2", "P3", "P4"]}
        out = kb_tables.render(spec, kb_tables.run_query(spec, {5}, {5: "Book"}))
        for piece in self.SECRET:
            if piece not in ("5600", "1200"):
                self.assertNotIn(piece, out, piece)
        self.assertIn("[card ****1111]", out)
        self.assertIn("1200 | 3400 | 5600 | 7800", out)
        cnt = {"source": "split", "filters": [{"column": "P1", "op": "starts_with", "value": "4012"}], "count": True,
               "select": None}
        self.assertIn("1 matching row(s)", kb_tables.render(cnt, kb_tables.run_query(cnt, {5}, {5: "Book"})))   # the row that is not a card


class SplitCardOldDataTests(DirectBase):
    """Rows stored by an older version (raw pieces): the copy path and kb_query still hide them (the in-place views need
    kb_tables.remask_all() or a re-index, see docs/knowledge-pan.md)."""

    def setUp(self):
        super().setUp()
        import json
        kb_tables.replace_source(6, kb_structure.rows_to_text([["Merchant", "P1", "P2"], ["a", "x", "y"]], "Old"))
        self.db.execute("UPDATE kb_rows SET cells = ? WHERE table_id = (SELECT id FROM kb_tables WHERE source_id = 6)",
                        (json.dumps(["a", "4111 1111", "1111 1111"]),))
        self.db.commit()

    def test_copy_path_hides_old_pieces(self):
        out = self.run_sql({"sql": "SELECT P1 || P2 FROM Book_Old"}, titles={6: "Book"}, copy=True)
        self.assertNotIn("4111", out)
        self.assertIn("[card ****1111]", out)

    def test_remask_all_fixes_the_in_place_path(self):
        kb_tables.remask_all()
        out = self.run_sql({"sql": "SELECT P1 || P2 FROM Book_Old"}, titles={6: "Book"})
        self.assertNotIn("4111", out)
