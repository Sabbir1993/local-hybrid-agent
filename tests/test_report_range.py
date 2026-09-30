"""db_report honours an explicit local date range (start/end inclusive) for every section."""
import time
import unittest
from unittest import mock

from core import db as D
from core.db import usage


class RangeTests(unittest.TestCase):
    def test_day_bounds(self):
        a, b = usage._day_bounds("2026-09-29", "2026-09-30")
        self.assertEqual(b - a, 2 * 86400)                 # the end day is included
        self.assertEqual(usage._day_bounds(None, None), (None, None))
        self.assertEqual(usage._day_bounds("garbage", "x"), (None, None))
        self.assertIsNone(usage._day_bounds("2026-09-30", None)[1])

    def test_report_range_limits_rows_and_days(self):
        rep = D.db_report(365, None, "2000-01-01", "2000-01-02")      # long before any usage
        self.assertEqual(rep["requests"], 0)
        self.assertEqual(rep["by_day"], [])
        self.assertEqual(rep["by_model"], [])

    def test_bad_dates_fall_back_to_the_rolling_window(self):
        with mock.patch.object(usage.time, "time", wraps=time.time):
            self.assertEqual(D.db_report(365, None, "nope", "nope")["days"], 365)


if __name__ == "__main__":
    unittest.main()
