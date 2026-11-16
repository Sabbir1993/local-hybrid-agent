import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.agent_tools import data_tools
from core.sheet_engine import sql as sheet_sql


class DataSqlErrorDetail(unittest.TestCase):
    def test_unexpected_error_reports_its_message(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.csv"
            p.write_text("A,B\n1,2\n", encoding="utf-8")
            with mock.patch("core.doc_tools.location.resolve_common", return_value=p), \
                 mock.patch.object(sheet_sql, "query", side_effect=RuntimeError("boom detail")):
                out = data_tools.tool_data_sql({"file": "a.csv", "sql": 'SELECT "A" FROM a'})
        self.assertTrue(out.startswith("error: could not read the data file (RuntimeError: boom detail"), out)


if __name__ == "__main__":
    unittest.main()
