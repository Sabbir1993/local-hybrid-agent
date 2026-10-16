"""The pure parts of scripts/bench_chat_files.py: SSE folding, nudge detection, outcome codes, the report table."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from scripts import bench_chat_files as B


def sse(*evs):
    out = []
    for name, data in evs:
        out += [f"event: {name}", "data: " + json.dumps(data), ""]
    return out


class FoldTests(unittest.TestCase):
    def test_pdf_build_with_one_nudge(self):
        lines = sse(("delta", {"text": "Let me build it."}), ("delta_reset", {}),            # no tool call after: a nudge
                    ("delta", {"text": "x" * 350}), ("delta_reset", {}),
                    ("tool_call", {"id": "1", "name": "doc_create", "args": {"file": "a.pdf", "content": "# A"}}),
                    ("tool_result", {"id": "1", "name": "doc_create", "ok": True, "result": "created"}),
                    ("tool_call", {"id": "2", "name": "doc_append", "args": {"file": "a.pdf", "content": "## B", "done": True}}),
                    ("tool_result", {"id": "2", "name": "doc_append", "ok": True, "result": "PDF check found 1 issue(s): page 2 is blank."}),
                    ("delta", {"text": "Done [DOWNLOAD: a.pdf]"}),
                    ("done", {"prompt_tokens": 900, "completion_tokens": 400, "total_tokens": 1300}))
        rec = B.fold_chat_sse(lines)
        self.assertEqual(rec["nudges"], 1)
        self.assertEqual([t["name"] for t in rec["tools"]], ["doc_create", "doc_append"])
        self.assertEqual(B.downloads_in(rec["text"]), ["a.pdf"])
        self.assertEqual(B.classify_run("pdf", rec, ["x"]), "ok_findings")
        self.assertEqual(B.classify_run("pdf", rec, []), "ok_nudged")
        self.assertTrue(rec["turn_tokens_est"])

    def test_text_only_answer_is_no_progress(self):
        rec = B.fold_chat_sse(sse(("delta", {"text": "Here is a plan."}), ("done", {"prompt_tokens": 1, "completion_tokens": 1})))
        self.assertEqual(B.classify_run("docx", rec, ["no file delivered"]), "no_progress")

    def test_docx_write_file_ok(self):
        rec = B.fold_chat_sse(sse(("tool_call", {"id": "1", "name": "write_file", "args": {"path": "r.docx", "content": "# R"}}),
                                  ("tool_result", {"id": "1", "name": "write_file", "ok": True, "result": "saved"}),
                                  ("delta", {"text": "[DOWNLOAD: r.docx]"}), ("done", {"prompt_tokens": 5, "completion_tokens": 5})))
        self.assertEqual(B.classify_run("docx", rec, []), "ok")
        self.assertEqual(B.classify_run("docx", rec, [], "ReadTimeout: x"), "timeout")

    def test_report_renders(self):
        row = {"format": "docx", "prompt": "p", "label": "ctx32k", "ctx": 32768, "outcome": "ok", "tool_ok": 1, "tool_total": 1,
               "nudges": 0, "max_turn_tokens_est": 100, "turns_over_cap": 0, "prompt_tokens": 10, "completion_tokens": 5,
               "duration_s": 3.0, "tool_calls": 1, "findings": [], "inloop_findings": 0}
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "results.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
            out = B.report([td])
        self.assertIn("### DOCX", out)
        self.assertIn("| 32768 | 1 | 1/1 | 1/1 | 1/1 | 1/1 |", out)


if __name__ == "__main__":
    unittest.main()
