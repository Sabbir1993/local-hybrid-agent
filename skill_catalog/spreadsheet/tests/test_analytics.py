import csv
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from analytics import as_number, as_date, NumberStats, analyze
from report import write_report


class NumericTests(unittest.TestCase):
    def test_currency_negative_percent_and_bad_numbers(self):
        self.assertEqual(as_number("৳1,200.50"), 1200.5)
        self.assertEqual(as_number("(1,200)"), -1200.0)
        self.assertEqual(as_number("5%"), .05)
        self.assertIsNone(as_number("oops"))
        self.assertIsNone(as_number("NaN"))

    def test_dates(self):
        self.assertEqual(as_date("2026-01-11").isoformat(), "2026-01-11")
        self.assertEqual(as_date(46023, allow_serial=True).isoformat(), "2026-01-01")

    def test_online_statistics(self):
        s = NumberStats(sample_size=20)
        for number in [1, 3, 5, 7, 9]:
            s.add(number)
        r = s.output()
        self.assertEqual(r["sum"], 25)
        self.assertEqual(r["mean"], 5)
        self.assertEqual(r["median"], 5)
        self.assertEqual(r["q1"], 3)
        self.assertFalse(r["quantile_is_sampled"])


class IntegrationTests(unittest.TestCase):
    def _source(self, folder):
        src = Path(folder) / "transactions.csv"
        with src.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["payment_id", "transaction_date", "payment_method", "amount", "status", "note"])
            w.writerow(["ID-1", "2026-01-01", "Card", "100", "SUCCESS", "=2+2"])
            w.writerow(["ID-2", "2026-01-05", "Wallet", "200", "FAILED", "okay"])
            w.writerow(["ID-2", "2026-01-05", "Wallet", "200", "FAILED", "okay"])
            w.writerow(["ID-4", "2026-02-01", "Card", "50", "PENDING", "test"])
            w.writerow(["ID-5", "2026-02-02", "Card", "", "SUCCESS", "test"])
            w.writerow(["ID-6", "2026-02-03", "Wallet", "wrong", "SUCCESS", "test"])
        return src

    def test_exact_aggregates_and_duplicates(self):
        with tempfile.TemporaryDirectory() as temp:
            res = analyze(self._source(temp), preview_rows=30)
            self.assertEqual(res["row_count"], 6)
            self.assertEqual(res["quality"]["exact_duplicate_rows"], 1)
            self.assertEqual(res["roles"]["value"], "amount")
            self.assertEqual(res["value_sum"], 550)
            self.assertEqual(res["value_valid_count"], 4)
            self.assertEqual(res["status"]["success"], 3)
            self.assertEqual(res["status"]["failure"], 2)
            self.assertEqual(res["status"]["unknown"], 1)
            self.assertTrue(all(res["checks"].values()))

    def test_workbook_charts_and_safe_text(self):
        with tempfile.TemporaryDirectory() as temp:
            source = self._source(temp)
            res = analyze(source, preview_rows=30)
            output = Path(temp) / "report.xlsx"
            meta = write_report(res, output)
            self.assertTrue(meta["validation"]["passed"])
            self.assertGreaterEqual(meta["charts"], 2)
            with zipfile.ZipFile(output) as f:
                sheets = [f.read(n).decode("utf-8") for n in f.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")]
                shared = f.read("xl/sharedStrings.xml").decode("utf-8") if "xl/sharedStrings.xml" in f.namelist() else ""
                self.assertTrue("=2+2" in shared or any("=2+2" in xml for xml in sheets))
                self.assertFalse(any("<f>2+2</f>" in xml for xml in sheets))
                self.assertTrue(any("Source_Metadata!B7" in xml for xml in sheets))

    def test_native_xlsx_source(self):
        source = Path(__file__).resolve().parents[1] / "examples" / "sample_transactions.xlsx"
        if not source.is_file():
            self.skipTest("Optional sample XLSX not present")
        res = analyze(source, sheet="Transactions", preview_rows=10)
        self.assertEqual(res["row_count"], 2402)
        self.assertEqual(res["quality"]["exact_duplicate_rows"], 2)
        self.assertTrue(all(res["checks"].values()))
        with self.assertRaises(ValueError):
            write_report(res, source)

    def test_no_overwrite_source(self):
        with tempfile.TemporaryDirectory() as temp:
            src = self._source(temp)
            res = analyze(src)
            # A new report path distinct from the source works.
            output = Path(temp) / "out.xlsx"
            self.assertTrue(write_report(res, output)["validation"]["passed"])


if __name__ == "__main__":
    unittest.main()
