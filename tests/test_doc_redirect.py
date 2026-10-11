"""tests/test_doc_redirect.py - an Office file is one doc_create call, not a run_python script (core/agent_loop/doc_redirect.py).

Run: python -m unittest tests.test_doc_redirect -v
"""

import json
import queue
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.agent_loop import doc_redirect as dr  # noqa: E402

OFFERED = {"run_python", "doc_create", "read_file"}
XLSX = "import os\ntry:\n    import openpyxl\nexcept ImportError:\n    pass\nwb = openpyxl.Workbook()"
DOCX = 'import importlib\nfor mod in ["docx", "zipfile"]:\n    importlib.import_module(mod)'


class Decision(unittest.TestCase):
    def test_scripts_that_import_office_libraries_are_redirected_once(self):
        seen = set()
        self.assertIn("doc_create", dr.note("run_python", {"code": XLSX}, OFFERED, seen))
        self.assertIsNone(dr.note("run_python", {"code": XLSX}, OFFERED, seen), "only once per run")

    def test_each_library_and_the_quoted_form(self):
        for code in ("import docx", "from pptx import Presentation", "  import xlsxwriter", DOCX,
                     "importlib.import_module('openpyxl')", "__import__('docx')"):
            with self.subTest(code=code):
                self.assertIsNotNone(dr.note("run_python", {"code": code}, OFFERED, set()))

    def test_other_code_and_other_tools_are_untouched(self):
        for code in ("print(137*29)", "import json, os", "import pandas as pd", "x = 'docx is a format'",
                     "# openpyxl would do this", "import os\nprint(os.listdir('.'))"):
            with self.subTest(code=code):
                self.assertIsNone(dr.note("run_python", {"code": code}, OFFERED, set()))
        self.assertIsNone(dr.note("run_shell", {"code": XLSX}, OFFERED, set()))
        self.assertIsNone(dr.note("run_python", {}, OFFERED, set()))

    def test_no_redirect_when_doc_create_is_not_offered(self):
        self.assertIsNone(dr.note("run_python", {"code": XLSX}, {"run_python"}, set()), "the tool is not there to point at")


class LoopBehaviour(unittest.TestCase):
    def run_script(self, prompt, script, answer=True):
        import eval_mock as em
        from routes.agent import permissions, stream
        cards, events = [], []

        async def fake_permission_stream(req_id, ev, *a, **k):
            try:
                yield "done", (answer, "", "once")
            finally:
                permissions._perm_pending.pop(req_id, None)

        with em.MockEvalEnv() as env, mock.patch.object(stream, "_permission_stream", fake_permission_stream):
            em._current["fifo"] = queue.Queue()
            for turn in script:
                em._current["fifo"].put(dict(turn))
            em._current["extra_calls"] = 0
            em._current["requests"] = 0
            payload = {"messages": [{"role": "user", "content": prompt}], "mode": "all-local", "max_steps": 8,
                       "temperature": 0, "session_id": env.sid}
            headers = {"User-Agent": "A770NativeApp/1.0", "X-Device-Id": em.DEVICE}
            with env.client.stream("POST", "/agent/run", json=payload, headers=headers, timeout=120) as resp:
                event = None
                for line in resp.iter_lines():
                    line = line.strip() if isinstance(line, str) else line.decode("utf-8", "replace").strip()
                    if line.startswith("event: "):
                        event = line[7:].strip()
                    elif line.startswith("data: ") and event:
                        try:
                            data = json.loads(line[6:])
                        except ValueError:
                            continue
                        events.append((event, data))
                        if event == "permission_request":
                            cards.append(data)
        return events, cards

    def test_the_first_office_script_is_redirected_without_a_card_and_a_second_one_is_not_blocked(self):
        import eval_mock as em
        events, cards = self.run_script(
            "Create an Excel file eval_tmp/sales.xlsx with a header row and two data rows.",
            [em.T(tool_calls=[em.TC("run_python", code=XLSX)]),
             em.T(tool_calls=[em.TC("run_python", code=XLSX)]),
             em.T(content="done")])
        results = [d for e, d in events if e == "tool_result"]
        self.assertGreaterEqual(len(results), 1)
        self.assertFalse(results[0]["ok"])
        self.assertIn("use doc_create", results[0]["result"])
        self.assertEqual(cards, [], "the redirected call never reaches an approval card")
        self.assertGreaterEqual(len(results), 2)
        self.assertNotIn("use doc_create", results[1]["result"], "the same script again is not blocked a second time")

    def test_a_script_without_office_libraries_is_never_redirected(self):
        import eval_mock as em
        events, _ = self.run_script(
            "Create an Excel file eval_tmp/sales.xlsx with two rows.",
            [em.T(tool_calls=[em.TC("run_python", code="print(137*29)")]), em.T(content="done")])
        res = [d for e, d in events if e == "tool_result"][0]
        self.assertNotIn("use doc_create", res["result"])


if __name__ == "__main__":
    unittest.main()
