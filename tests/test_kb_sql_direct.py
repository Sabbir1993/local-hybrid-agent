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

    def test_select_star_only_adds_the_row_column(self):
        new, old = self.both({"sql": "SELECT * FROM Staff_list_Staff"})
        self.assertEqual([ln.rsplit("|", 2)[0] + "|" for ln in new.splitlines()[:-1]], [ln for ln in old.splitlines()[:-1]])
        self.assertIn("| Alice | Manager | Dhaka | 90,000 | 2 |", new)

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
        self.assertEqual(new.replace(', "Row" REAL', ""), old)

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
        self.assertTrue(all(r[2] == "main" and r[3] == "Staff_list_Staff" for r in base), base)      # view name, base in main
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
