"""Opt-in: calculate a built workbook with the REAL Excel on this PC and compare with the built-in calculator.
Run with A770_LIVE_EXCEL=1 (it starts Excel hidden for a couple of seconds, on a temporary copy)."""
import base64
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

import core.agent_tools  # noqa: F401
import core.doc_ops as D
from core.sheet_engine import calc, excel, office

ROOT = Path(__file__).resolve().parents[1]
EXCEL = Path(r"C:\Program Files\Microsoft Office\root\Office16\EXCEL.EXE")
LIVE = os.environ.get("A770_LIVE_EXCEL") == "1" and os.name == "nt" and EXCEL.is_file() and shutil.which("node")

OUTLINE = """## Sheet: Rates
| Currency | BDT |
|---|---|
| USD | 120 |
| EUR | 130 |
## Sheet: Sales
| Item | Cur | Amount | BDT | Note |
|---|---|---|---|---|
| A | USD | 100 | =C2*VLOOKUP(B2,Rates!A2:B3,2,FALSE) | =TEXT(C2,"#,##0.00") |
| B | EUR | 50 | =C3*VLOOKUP(B3,Rates!A2:B3,2,FALSE) | =IFERROR(1/0,"n/a") |
| C | XXX | 10 | =C4*VLOOKUP(B4,Rates!A2:B3,2,FALSE) | =XLOOKUP("USD",Rates!A2:A3,Rates!B2:B3) |
"""


@unittest.skipUnless(LIVE, "set A770_LIVE_EXCEL=1 on a Windows PC with Excel to run")
class LiveExcel(unittest.TestCase):
    def test_excel_and_the_builtin_calculator_agree(self):
        from unittest import mock
        with mock.patch.object(excel, "recalculate", return_value=None), mock.patch.object(office, "find_soffice", return_value=None):
            data, _ = D.create("t.xlsx", OUTLINE)
            builtin = calc.calculate(data)
        js = ("const ex=require(process.argv[1]);ex.recalc({data_b64:process.argv[2],name:'t.xlsx'})"
              ".then(r=>console.log(JSON.stringify(r)))")
        out = subprocess.run(["node", "-e", js, str(ROOT / "companion" / "excelops.js"), base64.b64encode(data).decode()],
                             capture_output=True, text=True, timeout=90)
        self.assertEqual(out.returncode, 0, out.stderr)
        res = json.loads(out.stdout.strip().splitlines()[-1])
        cells = res["cells"]
        self.assertEqual(cells["Sales!D2"], 12000)
        self.assertEqual(cells["Sales!D3"], 6500)
        self.assertEqual(cells["Sales!D4"], "#N/A")            # an unknown currency: a real lookup error, reported as Excel shows it
        self.assertEqual(cells["Sales!E4"], 120)               # XLOOKUP works (the _xlfn. prefix is written)
        for (sheet, ref), v in builtin["values"].items():
            if isinstance(v, (int, float)) and f"{sheet}!{ref}" in cells:
                self.assertAlmostEqual(float(cells[f"{sheet}!{ref}"]), float(v), places=6, msg=f"{sheet}!{ref}")


if __name__ == "__main__":
    unittest.main()
