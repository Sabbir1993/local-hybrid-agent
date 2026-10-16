"""Quality benchmark: a fixed set of PDF outlines (guide, report, handbook, Bengali, checklist) must render, open, reach a
sensible page count and pass the app's own PDF check (no blank / almost-empty pages, no stranded heading, footer page number).

Offline counterpart of the live run in scripts/bench_chat_files.py.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import core.agent_tools  # noqa: F401
from core import doc_ops
from file_bench_checks import check_pdf
from file_bench_prompts import PDF_PROMPTS


class PdfBenchmark(unittest.TestCase):
    def test_every_outline_renders_clean(self):
        bad, total = [], 0
        for name, spec in PDF_PROMPTS.items():
            data = doc_ops.render_pdf(spec["outline"], title=name)
            total += 1
            bad += [f"{name}: {f}" for f in check_pdf(data, spec["outline"])]
        self.assertEqual(bad, [], f"{len(bad)} findings over {total} documents")


if __name__ == "__main__":
    unittest.main()
